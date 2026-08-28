"""The RealSense -> person detection -> tracking -> camera-local X/Z pipeline.

One background thread owns the camera. FastAPI request handlers only ever
read a snapshot out of the TrackStore or grab the latest encoded JPEG, so a
slow browser can never stall the camera loop and the camera can never block
an HTTP worker.

Every heavy or platform-specific import (pyrealsense2, ultralytics, cv2,
numpy) happens INSIDE this module's functions, not at import time. That is
deliberate: `backend/app/vision/api.py` must import cleanly on a machine
where none of them are installed, because the property system - which is
frozen, working infrastructure - is served by the same process. A missing
pyrealsense2 wheel degrades /vision/status to camera_connected=false; it does
not take the operations dashboard down with it.

Phase boundary: this produces CAMERA-LOCAL metres and nothing else. There is
no GNSS, no vehicle pose, no world transform and no property association
here, by design.
"""

from __future__ import annotations

import atexit
import gc
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from . import geometry as geo
from .tracking import TrackStore, utcnow

log = logging.getLogger("wastraq.vision")

STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
NO_CAMERA = "NO_CAMERA"
ERROR = "ERROR"
DEGRADED = "DEGRADED"


# ---------------------------------------------------------------------------
# Rolling video evidence (optional).
#
# Imported defensively: a problem in the evidence module must never be able
# to stop the camera thread. See app/evidence_buffer.py.
# ---------------------------------------------------------------------------
try:
    from ..evidence_buffer import evidence_recorder
except Exception as _evidence_error:  # noqa: BLE001
    evidence_recorder = None
    print("!! Evidence buffer unavailable:", repr(_evidence_error))

EVIDENCE_CAMERA_ID = os.environ.get("EVIDENCE_CAMERA_ID", "vision-pipeline")


class DependencyMissing(RuntimeError):
    def __init__(self, name: str, hint: str = "") -> None:
        self.name = name
        self.hint = hint
        super().__init__(f"{name} is not installed. {hint}".strip())


# ------------------------------------------------------- stream profiles --
#
# Asking a RealSense for a profile it does not actually publish is the classic
# way to get a pipeline that starts without complaint and then never delivers
# a frame. `rs-capture` never hits this because it starts with no config at
# all and takes whatever the device recommends. So: enumerate what the device
# really offers, pick the closest supported match, and log exactly what was
# chosen. Never hand librealsense a guess.

# Preference order. bgr8/rgb8 are synthesised by librealsense from the
# sensor's native yuyv, so all three are usable; we take whichever the device
# lists, cheapest conversion first.
COLOR_FORMAT_PREFERENCE = ("bgr8", "rgb8", "yuyv", "uyvy")
DEPTH_FORMAT_PREFERENCE = ("z16",)


def enumerate_stream_profiles(rs, device) -> list[dict[str, Any]]:
    """Every video stream profile the device publishes, as plain dicts.

    Plain dicts on purpose: the selection logic below is then pure Python and
    is unit-tested with no camera and no SDK (see scripts/test_vision_logic.py).
    """
    out: list[dict[str, Any]] = []
    for sensor in device.query_sensors():
        try:
            sensor_name = sensor.get_info(rs.camera_info.name)
        except Exception:  # noqa: BLE001
            sensor_name = "?"
        for sp in sensor.get_stream_profiles():
            try:
                if not sp.is_video_stream_profile():
                    continue
                vsp = sp.as_video_stream_profile()
                out.append({
                    "sensor": sensor_name,
                    "stream": str(sp.stream_type()).rsplit(".", 1)[-1].lower(),
                    "format": str(sp.format()).rsplit(".", 1)[-1].lower(),
                    "width": int(vsp.width()),
                    "height": int(vsp.height()),
                    "fps": int(sp.fps()),
                    "index": int(sp.stream_index()),
                })
            except Exception:  # noqa: BLE001
                continue
    return out


def profile_from_stream(rs, pipeline_profile, stream) -> dict[str, Any] | None:
    """The profile the pipeline ACTUALLY negotiated, read after start().

    Post-start introspection only. It reads the profile object start() already
    returned, so it cannot contend for the device the way a pre-start sensor
    sweep does.
    """
    try:
        sp = pipeline_profile.get_stream(stream)
        vsp = sp.as_video_stream_profile()
        return {
            "sensor": "(negotiated)",
            "stream": str(sp.stream_type()).rsplit(".", 1)[-1].lower(),
            "format": str(sp.format()).rsplit(".", 1)[-1].lower(),
            "width": int(vsp.width()),
            "height": int(vsp.height()),
            "fps": int(sp.fps()),
            "index": int(sp.stream_index()),
        }
    except Exception:  # noqa: BLE001
        return None


def discover_device(rs, configured_serial: str = "") -> dict[str, Any]:
    """Identity strings for the camera, with every handle released on return.

    Deliberately touches NO sensors and NO stream profiles. A held-open
    sensor/profile sweep is one of the two things the Mac/D455 diagnostic
    proved will break the `pipeline.start()` that follows it; the other is
    `can_resolve()`. Neither happens anywhere in the startup path any more.

    Never silently picks a different camera: a configured serial that is not
    present is an error, and so is more than one device with no serial set.
    """
    ctx = rs.context()
    devices = list(ctx.query_devices())
    try:
        found: list[dict[str, Any]] = []
        for dev in devices:
            row: dict[str, Any] = {}
            for attr, field in (("name", "name"),
                                ("serial_number", "serial"),
                                ("firmware_version", "firmware"),
                                ("usb_type_descriptor", "usb_type")):
                try:
                    row[field] = dev.get_info(getattr(rs.camera_info, attr))
                except Exception:  # noqa: BLE001
                    row[field] = None
            found.append(row)

        if not found:
            raise RuntimeError("no RealSense device found on USB")

        serials = ", ".join(str(r.get("serial")) for r in found)
        want = (configured_serial or "").strip()
        if want:
            for row in found:
                if str(row.get("serial") or "") == want:
                    return row
            raise RuntimeError(
                f"VISION_SERIAL={want} is not attached (found: {serials}). "
                "Refusing to open a different camera.")

        if len(found) > 1:
            raise RuntimeError(
                f"{len(found)} RealSense devices attached ({serials}); set "
                "VISION_SERIAL to bind one. Refusing to guess.")

        return found[0]
    finally:
        devices.clear()
        del devices, ctx
        gc.collect()


def describe_profile(p: dict[str, Any] | None) -> str:
    if not p:
        return "(none)"
    return (f"{p['stream']} {p['width']}x{p['height']} "
            f"{p['format'].upper()} @ {p['fps']}")


def select_stream_profile(profiles: list[dict[str, Any]], stream: str,
                          width: int, height: int, fps: int,
                          formats: tuple[str, ...]) -> dict[str, Any] | None:
    """Best actually-supported profile for a request. Pure function.

    Ranking, in order of importance:
      1. exact width x height  - the caller asked for a reason
      2. same aspect ratio     - a 4:3 crop of a 16:9 sensor is a different
                                 field of view, not a resize
      3. closest pixel count
      4. exact fps, then a rate at or below the request (never silently
         faster: USB bandwidth is the thing that usually breaks)
      5. closest fps
      6. format preference order
    Returns None when the device publishes nothing usable for that stream.
    """
    stream = stream.lower()
    fmts = tuple(f.lower() for f in formats)
    want_px = max(width * height, 1)
    want_aspect = width / height if height else 0.0

    cands = [p for p in profiles
             if str(p.get("stream", "")).lower() == stream
             and str(p.get("format", "")).lower() in fmts]
    if not cands:
        return None

    def key(p: dict[str, Any]):
        w, h, f = int(p["width"]), int(p["height"]), int(p["fps"])
        aspect = w / h if h else 0.0
        return (
            0 if (w == width and h == height) else 1,
            round(abs(aspect - want_aspect), 3),
            abs(w * h - want_px),
            0 if f == fps else 1,
            0 if f <= fps else 1,
            abs(f - fps),
            fmts.index(str(p["format"]).lower()),
            -w,  # deterministic tie-break
        )

    return sorted(cands, key=key)[0]


# --------------------------------------------------------------- camera --


class RealSenseSource:
    """Thin wrapper over pyrealsense2 with depth aligned to colour.

    Aligning matters more than it looks: without it a bounding box drawn on
    the colour image indexes the wrong pixels in the depth image, and the
    error grows with how close the subject is - exactly the range this demo
    operates in.

    Each stream can be enabled independently and alignment can be turned off,
    so the hardware diagnostic can isolate depth-only, colour-only, combined
    and aligned in that order and find out which layer is actually broken.

    Two timeouts, not one. The first frameset after `start()` costs the sensor
    a power-up, an auto-exposure convergence and (on macOS) a UVC negotiation:
    seconds, not milliseconds. Steady-state frames arrive at the frame period.
    Using one 2 s timeout for both is what turns a slow start into
    "Frame didn't arrive within 2000" forever.
    """

    def __init__(self, width: int, height: int, fps: int,
                 enable_color: bool = True, enable_depth: bool = True,
                 align: bool = True,
                 startup_timeout_ms: int | None = None,
                 runtime_timeout_ms: int | None = None,
                 warmup_frames: int | None = None) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.enable_color = enable_color
        self.enable_depth = enable_depth
        self.want_align = bool(align and enable_color and enable_depth)
        self.startup_timeout_ms = int(
            settings.VISION_STARTUP_TIMEOUT_MS if startup_timeout_ms is None
            else startup_timeout_ms)
        self.runtime_timeout_ms = int(
            settings.VISION_FRAME_TIMEOUT_MS if runtime_timeout_ms is None
            else runtime_timeout_ms)
        self.warmup_frames = int(
            settings.VISION_WARMUP_FRAMES if warmup_frames is None else warmup_frames)

        self._rs = None
        self._pipeline = None
        self._align = None
        self.depth_scale: float | None = None
        self.intrinsics: geo.Intrinsics | None = None
        self.device_name: str | None = None
        self.serial: str | None = None
        self.firmware: str | None = None
        self.usb_type: str | None = None

        self.available_profiles: list[dict[str, Any]] = []
        self.color_profile: dict[str, Any] | None = None
        self.depth_profile: dict[str, Any] | None = None
        self.first_frame_ms: float | None = None
        self.warmup_frames_read = 0

    # -- introspection -----------------------------------------------------
    def profile_summary(self) -> str:
        parts = []
        if self.depth_profile:
            parts.append(describe_profile(self.depth_profile))
        if self.color_profile:
            parts.append(describe_profile(self.color_profile))
        return " + ".join(parts) if parts else "(nothing enabled)"

    def supported(self, stream: str) -> list[str]:
        """Human-readable list of what the device publishes for one stream."""
        rows = sorted(
            {(p["width"], p["height"], p["format"], p["fps"])
             for p in self.available_profiles if p["stream"] == stream},
            key=lambda r: (-r[0] * r[1], r[2], -r[3]))
        return [f"{w}x{h} {f.upper()} @ {fps}" for w, h, f, fps in rows]

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if not (self.enable_color or self.enable_depth):
            raise RuntimeError("neither colour nor depth was enabled")

        try:
            import pyrealsense2 as rs  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise DependencyMissing(
                "pyrealsense2",
                "No wheel is published for Apple Silicon; see docs/VISION.md "
                "for the librealsense build. Original error: %s" % exc,
            ) from exc

        self._rs = rs

        # ------------------------------------------------------------------
        # Startup sequence, exactly as proven on the Mac / D455 by
        # scripts/diag_realsense_startup.py:
        #
        #   PASS   enable_device(serial)
        #          + enable_stream(depth, 640, 480, z16, 15)
        #          + pipeline.start(config)
        #   FAIL   the same config once can_resolve() is called first
        #   FAIL   the same config with a sensor/profile sweep held open
        #
        # So discovery is a separate step that releases every librealsense
        # handle before returning, and nothing between it and start() touches
        # the device. No can_resolve. No enumeration. No profile selection -
        # what a device publishes is not what it will start, which is the
        # whole reason the old path failed.
        # ------------------------------------------------------------------
        info = discover_device(rs, settings.VISION_SERIAL)
        self.device_name = info.get("name")
        self.serial = info.get("serial")
        self.firmware = info.get("firmware")
        self.usb_type = info.get("usb_type")
        del info
        gc.collect()

        if not self.serial:
            raise RuntimeError(
                "the camera reports no serial number, so it cannot be bound "
                "explicitly; set VISION_SERIAL")

        # A D455 negotiated down to USB 2.1 will not sustain dual 640x480@30.
        # Worth saying out loud rather than letting it look like a mystery
        # timeout.
        if self.usb_type and not str(self.usb_type).startswith("3"):
            log.warning("RealSense is on USB %s, not USB 3 - high frame rates "
                        "and dual streams may not be deliverable", self.usb_type)

        # Nothing is enumerated, so there is nothing to report until start()
        # answers.
        self.available_profiles = []
        self.depth_profile = None
        self.color_profile = None

        self._pipeline = rs.pipeline()
        cfg = rs.config()
        cfg.enable_device(self.serial)          # explicit binding, always

        if self.enable_depth:
            cfg.enable_stream(rs.stream.depth, self.width, self.height,
                              rs.format.z16, self.fps)
        if self.enable_color:
            cfg.enable_stream(rs.stream.color, self.width, self.height,
                              rs.format.bgr8, self.fps)

        log.info("RealSense start: serial=%s depth=%s color=%s %dx%d @%d",
                 self.serial, self.enable_depth, self.enable_color,
                 self.width, self.height, self.fps)

        profile = self._pipeline.start(cfg)

        # Post-start introspection: what was actually negotiated, read off the
        # profile start() just returned. Cannot contend for the device.
        if self.enable_depth:
            self.depth_profile = profile_from_stream(rs, profile, rs.stream.depth)
        if self.enable_color:
            self.color_profile = profile_from_stream(rs, profile, rs.stream.color)

        if self.enable_depth:
            depth_sensor = profile.get_device().first_depth_sensor()
            self.depth_scale = float(depth_sensor.get_depth_scale())

        # Align depth INTO the colour frame, so (u, v) means one thing.
        self._align = rs.align(rs.stream.color) if self.want_align else None

        ref = rs.stream.color if self.enable_color else rs.stream.depth
        ref_profile = profile.get_stream(ref).as_video_stream_profile()
        self.intrinsics = geo.Intrinsics.from_rs(ref_profile.get_intrinsics())

        log.info("RealSense open: %s (%s, fw %s, USB %s) profiles=[%s] "
                 "align=%s depth_scale=%s",
                 self.device_name, self.serial, self.firmware, self.usb_type,
                 self.profile_summary(), bool(self._align), self.depth_scale)

        self._warmup()

    def _warmup(self) -> None:
        """Discard the first few framesets before anybody looks at one.

        The first frames off a D400 are dark, half-exposed and sometimes
        missing one of the two streams. Running YOLO on them wastes the one
        moment the operator is watching, and depth sampled during auto-exposure
        convergence reads as invalid. The FIRST wait uses the startup timeout.
        """
        if self._pipeline is None:
            return
        t0 = time.time()
        self._pipeline.wait_for_frames(self.startup_timeout_ms)
        self.first_frame_ms = (time.time() - t0) * 1000.0
        self.warmup_frames_read = 1
        for _ in range(max(self.warmup_frames - 1, 0)):
            try:
                self._pipeline.wait_for_frames(self.runtime_timeout_ms)
                self.warmup_frames_read += 1
            except Exception:  # noqa: BLE001
                break

    def read(self, timeout_ms: int | None = None):
        """-> (colour BGR ndarray | None, depth uint16 ndarray | None).

        Raises on timeout or on a frameset missing a stream that was enabled.
        """
        import numpy as np  # local: keeps module import cheap

        assert self._pipeline is not None
        frames = self._pipeline.wait_for_frames(
            self.runtime_timeout_ms if timeout_ms is None else timeout_ms)
        if self._align is not None:
            frames = self._align.process(frames)

        colour = frames.get_color_frame() if self.enable_color else None
        depth = frames.get_depth_frame() if self.enable_depth else None
        if (self.enable_color and not colour) or (self.enable_depth and not depth):
            raise RuntimeError("incomplete frameset")
        return (
            np.asanyarray(colour.get_data()) if colour else None,
            np.asanyarray(depth.get_data()) if depth else None,
        )

    def close(self) -> None:
        try:
            if self._pipeline is not None:
                self._pipeline.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pipeline = None
            self._align = None


# ------------------------------------------------------------- detector --


class PersonDetector:
    """Ultralytics YOLO restricted to the `person` class, with a built-in tracker.

    Detection and tracking are one call on purpose. Ultralytics' `.track()`
    drives ByteTrack (or BoT-SORT) with the same association code the tracker
    authors ship, which is a great deal more reliable than a hand-rolled IOU
    matcher - and it hands back the stable `id` this phase needs.
    """

    PERSON_CLASS = 0

    def __init__(self, model_path: str, conf: float, imgsz: int,
                 tracker: str, device: str | None) -> None:
        self.model_path = model_path
        self.conf = conf
        self.imgsz = imgsz
        self.tracker = tracker
        self.device = device or None
        self.resolved_path = model_path
        self._model = None

    def load(self) -> None:
        try:
            from ultralytics import YOLO  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise DependencyMissing(
                "ultralytics",
                "pip install -r backend/requirements-vision.txt. Original error: %s" % exc,
            ) from exc
        path = self.model_path
        if not os.path.isabs(path):
            local = os.path.join(settings.VISION_MODEL_DIR, path)
            if os.path.exists(local):
                # Use the copy the installer fetched, so nothing tries to
                # download weights in the middle of a demo.
                path = local
        self._model = YOLO(path)
        self.resolved_path = path
        log.info("detector loaded: %s (tracker=%s, device=%s)",
                 path, self.tracker, self.device or "auto")

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def reset(self) -> None:
        """Drop tracker state - used after a camera reconnect."""
        try:
            if self._model is not None and hasattr(self._model, "predictor"):
                predictor = self._model.predictor
                if predictor is not None and hasattr(predictor, "trackers"):
                    for t in predictor.trackers:
                        if hasattr(t, "reset"):
                            t.reset()
        except Exception:  # noqa: BLE001
            pass

    def track(self, frame) -> list[dict[str, Any]]:
        """-> [{track_id, bbox, confidence}]. Detections with no id are dropped."""
        assert self._model is not None
        kwargs: dict[str, Any] = dict(
            source=frame,
            persist=True,
            classes=[self.PERSON_CLASS],
            conf=self.conf,
            imgsz=self.imgsz,
            tracker=self.tracker,
            verbose=False,
        )
        if self.device:
            kwargs["device"] = self.device
        results = self._model.track(**kwargs)
        out: list[dict[str, Any]] = []
        if not results:
            return out
        boxes = getattr(results[0], "boxes", None)
        if boxes is None or boxes.id is None:
            # Detections exist but the tracker has not assigned ids yet
            # (first frame, or everything was a new low-confidence detection).
            return out
        xyxy = boxes.xyxy.cpu().tolist()
        ids = boxes.id.int().cpu().tolist()
        confs = boxes.conf.cpu().tolist()
        for bbox, tid, conf in zip(xyxy, ids, confs):
            out.append({"track_id": int(tid), "bbox": tuple(float(v) for v in bbox),
                        "confidence": float(conf)})
        return out


# ------------------------------------------------------------- pipeline --


class VisionPipeline:
    """Owns the camera thread. One instance per process (see `pipeline`)."""

    def __init__(self) -> None:
        self.store = TrackStore(
            live_ttl_s=settings.VISION_TRACK_TTL_S,
            retire_s=settings.VISION_TRACK_RETIRE_S,
            trajectory_seconds=settings.VISION_TRAJECTORY_S,
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.state: str = STOPPED
        self.last_error: str | None = None
        self.missing_dependencies: list[str] = []
        self.started_at: datetime | None = None

        self.camera_connected = False
        self.color_stream_active = False
        self.depth_stream_active = False
        self.detector_loaded = False
        self.tracker_active = False

        self.fps = 0.0
        self.frames_processed = 0
        self.latest_frame_timestamp: datetime | None = None
        self.depth_scale: float | None = None
        self.intrinsics: geo.Intrinsics | None = None
        self.device_name: str | None = None
        self.usb_type: str | None = None
        self.color_profile: dict[str, Any] | None = None
        self.depth_profile: dict[str, Any] | None = None
        self.first_frame_ms: float | None = None

        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._jpeg_cv = threading.Condition()

        self.calibration_samples: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "reason": "already running", "state": self.state}
            self._stop.clear()
            self.last_error = None
            self.missing_dependencies = []
            self.state = STARTING
            self.started_at = utcnow()
            self._thread = threading.Thread(
                target=self._run, name="wastraq-vision", daemon=True)
            self._thread.start()
            return {"started": True, "state": self.state}

    def stop(self, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            self._thread = None
            self.state = STOPPED
            self.camera_connected = False
            self.color_stream_active = False
            self.depth_stream_active = False
            self.tracker_active = False
            self.fps = 0.0
        # Wake any MJPEG generator that is waiting for a frame.
        with self._jpeg_cv:
            self._jpeg_cv.notify_all()
        return {"stopped": True, "state": self.state}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        detector = PersonDetector(
            settings.VISION_MODEL, settings.VISION_CONF, settings.VISION_IMGSZ,
            settings.VISION_TRACKER, settings.VISION_DEVICE,
        )
        try:
            detector.load()
            self.detector_loaded = True
        except DependencyMissing as exc:
            self._fail(ERROR, str(exc), missing=[exc.name])
            return
        except Exception as exc:  # noqa: BLE001
            self._fail(ERROR, f"detector failed to load: {exc}")
            return

        source: RealSenseSource | None = None
        backoff = settings.VISION_RECONNECT_S

        while not self._stop.is_set():
            # ---- (re)connect -------------------------------------------
            if source is None:
                try:
                    source = RealSenseSource(
                        settings.VISION_WIDTH, settings.VISION_HEIGHT,
                        settings.VISION_DEPTH_FPS,
                        enable_color=settings.VISION_ENABLE_COLOR,
                        enable_depth=True)
                    source.open()
                    self.depth_scale = source.depth_scale
                    self.intrinsics = source.intrinsics
                    self.device_name = source.device_name
                    self.usb_type = source.usb_type
                    self.color_profile = source.color_profile
                    self.depth_profile = source.depth_profile
                    self.first_frame_ms = source.first_frame_ms
                    self.camera_connected = True
                    self.color_stream_active = bool(source.enable_color)
                    self.depth_stream_active = bool(source.enable_depth)
                    self.tracker_active = bool(source.enable_color)
                    self.state = RUNNING
                    self.last_error = None
                    self.missing_dependencies = []
                    detector.reset()
                    self.store.clear()
                    backoff = settings.VISION_RECONNECT_S
                except DependencyMissing as exc:
                    # Not recoverable by retrying - stop cleanly and say why.
                    self._fail(NO_CAMERA, str(exc), missing=[exc.name])
                    return
                except Exception as exc:  # noqa: BLE001
                    source = None
                    self.camera_connected = False
                    self.color_stream_active = False
                    self.depth_stream_active = False
                    self.tracker_active = False
                    self.state = NO_CAMERA
                    self.last_error = str(exc)
                    log.warning("RealSense not available (%s); retrying in %.0fs",
                                exc, backoff)
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 1.5, settings.VISION_RECONNECT_MAX_S)
                    continue

            # ---- one frame ---------------------------------------------
            try:
                colour, depth = source.read(timeout_ms=settings.VISION_FRAME_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("frame read failed (%s); reopening the camera", exc)
                self.last_error = f"frame read failed: {exc}"
                self.state = DEGRADED
                self.camera_connected = False
                try:
                    source.close()
                finally:
                    source = None
                if self._stop.wait(settings.VISION_RECONNECT_S):
                    break
                continue

            try:
                self._process(colour, depth, detector)
            except Exception as exc:  # noqa: BLE001
                # A single bad frame must not kill the thread.
                log.exception("frame processing failed")
                self.last_error = f"frame processing failed: {exc}"

        if source is not None:
            source.close()
        self.camera_connected = False
        self.color_stream_active = False
        self.depth_stream_active = False
        self.tracker_active = False
        if self.state != ERROR:
            self.state = STOPPED
        log.info("vision thread exited")

    def _fail(self, state: str, message: str, missing: list[str] | None = None) -> None:
        self.state = state
        self.last_error = message
        if missing:
            self.missing_dependencies = missing
        self.camera_connected = False
        self.tracker_active = False
        log.error("vision pipeline stopped: %s", message)

    def _process(self, colour, depth, detector: PersonDetector) -> None:
        ts = utcnow()
        intr = self.intrinsics
        assert intr is not None

        if colour is None:
            # Depth-only smoke test: no colour frame means there is nothing
            # for YOLO to look at. Count the frame and return. The detection,
            # tracking and depth-measurement code below is untouched and runs
            # unchanged the moment colour is enabled again.
            self._count_frame(ts)
            return

        detections = detector.track(colour)

        overlay_rows: list[tuple[dict[str, Any], Any]] = []
        for det in detections:
            sample, position = geo.measure_person(
                depth, det["bbox"], intr,
                depth_scale=self.depth_scale or 1.0,
                window=settings.VISION_DEPTH_WINDOW,
                min_valid=settings.VISION_DEPTH_MIN_VALID,
                cluster_tol_m=settings.VISION_DEPTH_CLUSTER_TOL_M,
                min_depth_m=settings.VISION_DEPTH_MIN_M,
                max_depth_m=settings.VISION_DEPTH_MAX_M,
                anchor_inset=settings.VISION_ANCHOR_INSET,
            )
            st = self.store.observe(
                det["track_id"], ts, det["bbox"], det["confidence"], sample, position)
            st.ema.alpha = settings.VISION_SMOOTH_ALPHA
            st.ema.max_jump_m = settings.VISION_SMOOTH_MAX_JUMP_M
            overlay_rows.append((det, st))

        self.store.prune(ts)
        self._count_frame(ts)

        if settings.VISION_STREAM_ENABLED:
            self._encode(colour, overlay_rows)

    def _count_frame(self, ts) -> None:
        # fps: exponential average over frame intervals, so the number on the
        # status panel is the real throughput, not the camera's nominal rate.
        if self.latest_frame_timestamp is not None:
            dt = (ts - self.latest_frame_timestamp).total_seconds()
            if dt > 0:
                inst = 1.0 / dt
                self.fps = round(inst if self.fps == 0 else 0.85 * self.fps + 0.15 * inst, 2)
        self.latest_frame_timestamp = ts
        self.frames_processed += 1

    # -- overlay + mjpeg ---------------------------------------------------
    def _encode(self, colour, rows) -> None:
        try:
            import cv2  # type: ignore
        except Exception:  # noqa: BLE001
            return

        img = colour.copy()
        for det, st in rows:
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
            valid = st.depth_valid and st.smoothed is not None
            colour_bgr = (90, 200, 90) if valid else (60, 160, 240)  # green / amber
            cv2.rectangle(img, (x1, y1), (x2, y2), colour_bgr, 2)

            if st.anchor_px:
                au, av = st.anchor_px
                cv2.drawMarker(img, (int(au), int(av)), (240, 240, 240),
                               cv2.MARKER_CROSS, 12, 1)

            if valid:
                sx, _sy, sz = st.smoothed  # type: ignore[misc]
                lines = [
                    st.label,
                    f"conf {st.detection_confidence:.2f}",
                    f"X {sx:+.2f} m",
                    f"Z {sz:.2f} m",
                ]
            else:
                lines = [st.label, f"conf {st.detection_confidence:.2f}", "depth invalid"]

            # A compact label block above the box; below it if there is no room.
            pad, lh = 4, 16
            bw = 100
            bh = lh * len(lines) + pad
            ty = y1 - bh - 2
            if ty < 0:
                ty = min(y2 + 2, img.shape[0] - bh - 2)
            cv2.rectangle(img, (x1, ty), (x1 + bw, ty + bh), (28, 30, 34), -1)
            for i, text in enumerate(lines):
                cv2.putText(img, text, (x1 + pad, ty + pad + lh * i + 11),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, colour_bgr, 1, cv2.LINE_AA)

        cv2.putText(img, f"{self.fps:.1f} fps  tracks {len(rows)}",
                    (8, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (220, 220, 220), 1, cv2.LINE_AA)

        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), settings.VISION_JPEG_QUALITY])
        if not ok:
            return
        payload = buf.tobytes()
        with self._jpeg_cv:
            self._jpeg = payload
            self._jpeg_seq += 1
            self._jpeg_cv.notify_all()

        # Rolling video evidence. Reuses the JPEG just encoded for the MJPEG
        # stream, so buffering the last ~20 s costs no extra work in this
        # loop. Deliberately outside `self._jpeg_cv`: the buffer has its own
        # lock and must never be able to stall a stream reader.
        if evidence_recorder is not None:
            evidence_recorder.add_frame(jpeg=payload,
                                        camera_id=EVIDENCE_CAMERA_ID)

    def latest_jpeg(self) -> tuple[bytes | None, int]:
        with self._jpeg_cv:
            return self._jpeg, self._jpeg_seq

    def wait_for_jpeg(self, last_seq: int, timeout: float = 2.0) -> tuple[bytes | None, int]:
        with self._jpeg_cv:
            if self._jpeg_seq == last_seq:
                self._jpeg_cv.wait(timeout)
            return self._jpeg, self._jpeg_seq

    # -- status ------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = utcnow()
        live = self.store.live(now)
        return {
            "camera_connected": self.camera_connected,
            "color_stream_active": self.color_stream_active,
            "depth_stream_active": self.depth_stream_active,
            "detector_loaded": self.detector_loaded,
            "tracker_active": self.tracker_active,
            "fps": self.fps,
            "latest_frame_timestamp": (
                self.latest_frame_timestamp.isoformat() if self.latest_frame_timestamp else None),
            "state": self.state,
            "running": self.running,
            "last_error": self.last_error,
            "missing_dependencies": self.missing_dependencies,
            "active_tracks": len(live),
            "known_tracks": len(self.store.all(now)),
            "frames_processed": self.frames_processed,
            "depth_scale": self.depth_scale,
            "intrinsics": self.intrinsics.to_dict() if self.intrinsics else None,
            "convention": geo.CONVENTION,
            "model": settings.VISION_MODEL,
            "tracker": settings.VISION_TRACKER,
            "device": self.device_name,
            "uptime_s": round((now - self.started_at).total_seconds(), 1) if self.started_at else 0.0,
            "usb_type": self.usb_type,
            # What was actually negotiated with the device, which is not
            # necessarily what was requested - see select_stream_profile.
            "negotiated_color_profile": describe_profile(self.color_profile),
            "negotiated_depth_profile": describe_profile(self.depth_profile),
            "first_frame_ms": (round(self.first_frame_ms, 1)
                               if self.first_frame_ms is not None else None),
            "config": {
                "width": settings.VISION_WIDTH,
                "height": settings.VISION_HEIGHT,
                "fps_target": settings.VISION_FPS,
                "startup_timeout_ms": settings.VISION_STARTUP_TIMEOUT_MS,
                "frame_timeout_ms": settings.VISION_FRAME_TIMEOUT_MS,
                "warmup_frames": settings.VISION_WARMUP_FRAMES,
                "conf_threshold": settings.VISION_CONF,
                "imgsz": settings.VISION_IMGSZ,
                "depth_window_px": settings.VISION_DEPTH_WINDOW,
                "depth_min_valid_px": settings.VISION_DEPTH_MIN_VALID,
                "depth_cluster_tol_m": settings.VISION_DEPTH_CLUSTER_TOL_M,
                "depth_range_m": [settings.VISION_DEPTH_MIN_M, settings.VISION_DEPTH_MAX_M],
                "anchor_inset_frac": settings.VISION_ANCHOR_INSET,
                "smooth_alpha": settings.VISION_SMOOTH_ALPHA,
                "smooth_max_jump_m": settings.VISION_SMOOTH_MAX_JUMP_M,
                "trajectory_seconds": settings.VISION_TRAJECTORY_S,
                "track_live_ttl_s": settings.VISION_TRACK_TTL_S,
                "track_retire_s": settings.VISION_TRACK_RETIRE_S,
            },
        }


pipeline = VisionPipeline()


@atexit.register
def _shutdown() -> None:  # pragma: no cover - process teardown
    try:
        if pipeline.running:
            pipeline.stop(timeout=3.0)
    except Exception:  # noqa: BLE001
        pass


def maybe_autostart() -> None:
    """Start on import only if explicitly asked for.

    Off by default on purpose: the backend is the same process that serves
    the property dashboards, and grabbing the camera every time someone runs
    `./scripts/run_backend.sh` would be a surprise, not a feature. The demo
    page starts it.
    """
    if settings.VISION_AUTOSTART and not pipeline.running:
        pipeline.start()
