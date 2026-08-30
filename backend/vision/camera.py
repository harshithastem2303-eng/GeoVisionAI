"""Frame sources: Intel RealSense, and a hardware-free mock.

pyrealsense2 is imported lazily and only inside :class:`RealSenseSource`.
Importing this module with no camera attached, or with the SDK not installed
at all, must succeed -- the API has to start and the tests have to run.

RealSense startup sequence
--------------------------
The order below is not stylistic. It follows a startup sequence proven
against a physical D455, where two seemingly harmless additions made an
otherwise working configuration fail:

* ``config.can_resolve()`` before ``start()`` can turn a startable profile
  into a failure. It is never called on the startup path.
* Enumerating sensors / stream profiles before ``start()`` can do the same,
  and any device or sensor handle held open across ``start()`` can too.
  Discovery therefore reads identity fields only and releases every handle
  before the pipeline starts.
* ``enable_device(serial)`` before ``start(config)`` is the *good* half:
  it binds one specific camera so startup never guesses between two.

Stream introspection (intrinsics, depth scale, negotiated profile) happens
strictly **after** a successful ``start()``, reading the profile object that
``start()`` returned.
"""

from __future__ import annotations

import gc
import logging
from typing import Optional, Tuple

from .depth_position import CameraIntrinsics

logger = logging.getLogger(__name__)


class CameraUnavailable(RuntimeError):
    """Raised when no usable camera can be opened."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_device(rs, configured_serial: str = "") -> dict:
    """Identify the RealSense to bind, touching no sensor and no profile.

    Reads name / serial / firmware / USB type, then releases the context and
    the device list before returning. Refuses to substitute: an absent
    configured serial is an error, and so is more than one camera attached
    with no serial configured.
    """

    ctx = rs.context()
    devices = ctx.query_devices()

    found = []
    for device in devices:
        try:
            info = {
                "name": device.get_info(rs.camera_info.name),
                "serial": device.get_info(rs.camera_info.serial_number),
            }
        except Exception:  # pragma: no cover - depends on SDK build
            continue
        for key, attr in (
            ("firmware", "firmware_version"),
            ("usb_type", "usb_type_descriptor"),
        ):
            try:
                info[key] = device.get_info(getattr(rs.camera_info, attr))
            except Exception:
                info[key] = "unknown"
        found.append(info)

    # Release every handle before the caller starts a pipeline.
    del devices
    del ctx
    gc.collect()

    if not found:
        raise CameraUnavailable("No Intel RealSense device detected.")

    if configured_serial:
        for info in found:
            if info["serial"] == configured_serial:
                return info
        raise CameraUnavailable(
            f"RealSense serial {configured_serial} is not attached. "
            f"Found: {', '.join(i['serial'] for i in found)}"
        )

    if len(found) > 1:
        raise CameraUnavailable(
            "Multiple RealSense devices attached and GEOVISION_VISION_SERIAL "
            "is not set; refusing to guess. Found: "
            + ", ".join(f"{i['name']} ({i['serial']})" for i in found)
        )

    return found[0]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class FrameSource:
    """Interface every frame source implements."""

    backend = "abstract"

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Tuple[Optional[object], Optional[object]]:
        """Return ``(color, depth)``. Either may be ``None``."""

        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def is_open(self) -> bool:
        return False

    @property
    def intrinsics(self) -> Optional[CameraIntrinsics]:
        return None

    @property
    def depth_scale(self) -> Optional[float]:
        return None

    def describe(self) -> dict:
        return {"backend": self.backend, "open": self.is_open}


class RealSenseSource(FrameSource):
    """Intel RealSense colour + depth."""

    backend = "realsense"

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        serial: str = "",
        enable_color: bool = True,
        enable_depth: bool = True,
        frame_timeout_ms: int = 5000,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.serial = serial
        self.enable_color = enable_color
        self.enable_depth = enable_depth
        self.frame_timeout_ms = frame_timeout_ms

        self._rs = None
        self._pipeline = None
        self._profile = None
        self._open = False
        self._intrinsics: Optional[CameraIntrinsics] = None
        self._depth_scale: Optional[float] = None
        self._device_info: dict = {}
        self._negotiated: dict = {}

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def intrinsics(self) -> Optional[CameraIntrinsics]:
        return self._intrinsics

    @property
    def depth_scale(self) -> Optional[float]:
        return self._depth_scale

    def open(self) -> None:
        if self._open:
            return

        try:
            import pyrealsense2 as rs  # imported lazily on purpose
        except Exception as exc:
            raise CameraUnavailable(f"pyrealsense2 is not available: {exc}") from exc

        self._rs = rs

        # 1. Identify the device and release every handle before starting.
        self._device_info = discover_device(rs, self.serial)
        serial = self._device_info["serial"]
        logger.info(
            "Binding RealSense %s (%s, USB %s)",
            self._device_info.get("name"),
            serial,
            self._device_info.get("usb_type"),
        )

        # 2. Build the config: explicit device, explicit streams. Nothing else.
        #    No can_resolve(). No profile enumeration. Both are known to break
        #    an otherwise startable configuration.
        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(serial)

        if self.enable_depth:
            config.enable_stream(
                rs.stream.depth,
                self.width,
                self.height,
                rs.format.z16,
                self.fps,
            )
        if self.enable_color:
            config.enable_stream(
                rs.stream.color,
                self.width,
                self.height,
                rs.format.bgr8,
                self.fps,
            )

        # 3. Start.
        try:
            profile = pipeline.start(config)
        except Exception as exc:
            self._pipeline = None
            raise CameraUnavailable(
                f"pipeline.start() failed for {serial}: {exc}"
            ) from exc

        self._pipeline = pipeline
        self._profile = profile
        self._open = True

        # 4. Introspect only now, from what start() actually returned.
        self._read_negotiated_profile(rs, profile)
        logger.info("RealSense pipeline started: %s", self._negotiated)

    def _read_negotiated_profile(self, rs, profile) -> None:
        """Post-start introspection. Cannot contend for the device."""

        try:
            device = profile.get_device()
            for sensor in device.query_sensors():
                if sensor.is_depth_sensor():
                    self._depth_scale = float(
                        sensor.as_depth_sensor().get_depth_scale()
                    )
                    break
        except Exception as exc:
            logger.warning("Could not read depth scale: %s", exc)

        for stream, label in (
            (rs.stream.color, "color"),
            (rs.stream.depth, "depth"),
        ):
            try:
                sp = profile.get_stream(stream).as_video_stream_profile()
            except Exception:
                continue
            self._negotiated[label] = (
                f"{sp.width()}x{sp.height()} @ {sp.fps()}"
            )
            # Intrinsics come from the colour stream when it exists, because
            # detections are made in colour pixel coordinates.
            if label == "color" or self._intrinsics is None:
                try:
                    self._intrinsics = CameraIntrinsics.from_realsense(
                        sp.get_intrinsics()
                    )
                except Exception as exc:
                    logger.warning("Could not read %s intrinsics: %s", label, exc)

    def read(self):
        if not self._open or self._pipeline is None:
            return None, None

        try:
            frames = self._pipeline.wait_for_frames(self.frame_timeout_ms)
        except Exception as exc:
            logger.warning("wait_for_frames failed: %s", exc)
            return None, None

        color = depth = None
        try:
            import numpy as np

            if self.enable_color:
                color_frame = frames.get_color_frame()
                if color_frame:
                    color = np.asanyarray(color_frame.get_data())
            if self.enable_depth:
                depth_frame = frames.get_depth_frame()
                if depth_frame:
                    depth = np.asanyarray(depth_frame.get_data())
        except Exception as exc:
            logger.warning("Frame conversion failed: %s", exc)

        return color, depth

    def close(self) -> None:
        self._open = False
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
                logger.info("RealSense pipeline stopped")
            except Exception as exc:
                logger.warning("Pipeline stop error: %s", exc)
        self._pipeline = None
        self._profile = None

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "open": self._open,
            "device": self._device_info,
            "negotiated_profile": self._negotiated,
            "depth_scale": self._depth_scale,
            "intrinsics": self._intrinsics.to_dict() if self._intrinsics else None,
            "color_stream_active": self._open and self.enable_color,
            "depth_stream_active": self._open and self.enable_depth,
        }


class MockSource(FrameSource):
    """Synthetic frames so the API, the dashboard and the tests run dry.

    Produces a plain gradient image and a constant-depth plane. Nothing in
    it pretends to be a detection -- YOLO simply finds no people, which is
    the honest answer for a synthetic frame.
    """

    backend = "mock"

    def __init__(self, width: int = 640, height: int = 480) -> None:
        self.width = width
        self.height = height
        self._open = False
        self._frame_index = 0

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def intrinsics(self) -> Optional[CameraIntrinsics]:
        # Plausible 640x480 pinhole values; enough to exercise deprojection.
        return CameraIntrinsics(
            width=self.width,
            height=self.height,
            fx=615.0,
            fy=615.0,
            ppx=self.width / 2.0,
            ppy=self.height / 2.0,
        )

    @property
    def depth_scale(self) -> Optional[float]:
        return 0.001  # millimetres, as on a RealSense

    def open(self) -> None:
        self._open = True
        logger.info("Mock camera source open (%dx%d)", self.width, self.height)

    def read(self):
        if not self._open:
            return None, None
        try:
            import numpy as np
        except Exception:  # pragma: no cover
            return None, None

        self._frame_index += 1
        shade = (self._frame_index % 60) + 40
        color = np.full((self.height, self.width, 3), shade, dtype=np.uint8)
        depth = np.full((self.height, self.width), 3000, dtype=np.uint16)
        return color, depth

    def close(self) -> None:
        self._open = False

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "open": self._open,
            "device": {"name": "mock", "serial": "MOCK-0000"},
            "negotiated_profile": {
                "color": f"{self.width}x{self.height} @ 30",
                "depth": f"{self.width}x{self.height} @ 30",
            },
            "depth_scale": self.depth_scale,
            "intrinsics": self.intrinsics.to_dict() if self.intrinsics else None,
            "color_stream_active": self._open,
            "depth_stream_active": self._open,
        }


def build_source(
    backend: str,
    width: int,
    height: int,
    fps: int,
    serial: str = "",
    enable_color: bool = True,
    enable_depth: bool = True,
    frame_timeout_ms: int = 5000,
) -> FrameSource:
    """Factory driven by ``GEOVISION_CAMERA_BACKEND``."""

    if backend == "mock":
        return MockSource(width=width, height=height)
    return RealSenseSource(
        width=width,
        height=height,
        fps=fps,
        serial=serial,
        enable_color=enable_color,
        enable_depth=enable_depth,
        frame_timeout_ms=frame_timeout_ms,
    )
