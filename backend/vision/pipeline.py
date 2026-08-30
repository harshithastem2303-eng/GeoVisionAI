"""The capture loop: frames in, worker observations out.

Flow per frame::

    frame -> YOLO detects every person
          -> BoT-SORT tracks every person
          -> registry says which tracks are bound to a collector
          -> optional depth gives camera-relative XYZ
          -> location provider gives a coarse fix
          -> observation

Pedestrians are never hidden. They appear with
``is_authorized_picker: false`` and ``collector_id: null``. Only tracks with
a live RFID binding are authorised, and downstream collection logic is the
only consumer that should filter on that flag.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from .camera import CameraUnavailable, FrameSource, build_source
from .depth_position import describe_person_depth
from .detector import PersonCropWriter, PersonDetector
from .track_history import TrackHistory
from .types import PersonDetection
from .worker_registry import WorkerRegistry

logger = logging.getLogger(__name__)


def _iso(timestamp: float) -> str:
    """ISO-8601 UTC with an explicit ``Z`` and millisecond precision.

    Two machines with two clocks have to agree on when things happened, so
    the format is fixed and absolute -- never a frame index, never a naive
    local time.
    """

    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


#: Depth keys copied verbatim from :func:`describe_person_depth` onto every
#: observation, so the outbound schema has one source of truth.
DEPTH_KEYS = (
    "depth_m",
    "camera_x_m",
    "camera_y_m",
    "camera_z_m",
    "relative_x_m",
    "relative_forward_m",
    "depth_valid",
    "depth_status",
)


class VisionPipeline:
    """Owns the camera thread and the current view of tracked people."""

    def __init__(
        self,
        settings,
        registry: WorkerRegistry,
        history: TrackHistory,
        location_service=None,
        publisher=None,
        clip_buffer=None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.history = history
        self.location_service = location_service
        # Both optional and both fire-and-forget. Neither may block, raise
        # into, or otherwise reach back into the capture loop.
        self.publisher = publisher
        self.clip_buffer = clip_buffer

        self.source: Optional[FrameSource] = None
        self.detector = PersonDetector(
            model_path=settings.YOLO_MODEL,
            confidence=settings.YOLO_CONFIDENCE,
            tracker=settings.TRACKER_CONFIG,
        )
        self.crop_writer = PersonCropWriter(
            directory=settings.PERSON_CROP_DIR,
            interval_s=settings.PERSON_CROP_INTERVAL_S,
            min_confidence=settings.PERSON_CROP_MIN_CONFIDENCE,
            max_per_id=settings.PERSON_CROP_MAX_PER_ID,
            enabled=settings.SAVE_PERSON_CROPS,
        )

        self.running = False
        self.recording = False
        self.last_error: Optional[str] = None

        self._thread: Optional[threading.Thread] = None
        self._frame_lock = threading.Lock()
        self._latest_annotated = None
        self._latest_depth = None
        self._observations: List[dict] = []

        self.frames_processed = 0
        self.fps = 0.0
        self.started_at: Optional[float] = None
        self.track_events_published = 0

    # -- lifecycle --------------------------------------------------------

    @property
    def camera_connected(self) -> bool:
        return self.source is not None and self.source.is_open

    @property
    def depth_available(self) -> bool:
        return bool(
            self.settings.DEPTH_ENABLED
            and self.source is not None
            and self.source.depth_scale
            and self.source.intrinsics
        )

    def connect(self) -> dict:
        """Open the frame source. Raises :class:`CameraUnavailable` on failure."""

        if self.camera_connected:
            return self.describe_camera()

        self.source = build_source(
            backend=self.settings.CAMERA_BACKEND,
            width=self.settings.CAMERA_WIDTH,
            height=self.settings.CAMERA_HEIGHT,
            fps=self.settings.CAMERA_FPS,
            serial=self.settings.VISION_SERIAL,
            enable_color=self.settings.ENABLE_COLOR,
            enable_depth=self.settings.ENABLE_DEPTH,
            frame_timeout_ms=self.settings.FRAME_TIMEOUT_MS,
        )
        try:
            self.source.open()
            self.last_error = None
        except CameraUnavailable as exc:
            self.last_error = str(exc)
            self.source = None
            raise

        return self.describe_camera()

    def start(self) -> str:
        """Begin capture. Starts a fresh identity session.

        Track ids and worker bindings are invalidated together: BoT-SORT
        renumbers on restart, so a binding from the previous run would point
        at a different human.
        """

        if self.running:
            return self.registry.session_id

        if not self.camera_connected:
            self.connect()

        session_id = self.registry.start_session()
        self.detector.reset_tracker()
        self.history.clear()
        if self.publisher is not None:
            # Track ids restart with the session; so must their throttles.
            self.publisher.reset_rate_limiter()
        if self.clip_buffer is not None:
            self.clip_buffer.clear()

        self.running = True
        self.recording = True
        self.frames_processed = 0
        self.started_at = time.time()

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Capture started (session %s)", session_id)
        return session_id

    def stop(self) -> None:
        if not self.running:
            self.recording = False
            return
        self.running = False
        self.recording = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self.fps = 0.0
        self.started_at = None
        logger.info("Capture stopped")

    def disconnect(self) -> None:
        self.stop()
        if self.source is not None:
            self.source.close()
        self.source = None
        with self._frame_lock:
            self._latest_annotated = None
            self._latest_depth = None
            self._observations = []

    # -- capture loop -----------------------------------------------------

    def _loop(self) -> None:
        previous = time.time()
        while self.running:
            try:
                color, depth = self.source.read()
                if color is None:
                    time.sleep(0.01)
                    continue

                now = time.time()
                annotated, detections = self.detector.detect(color)

                # Identity bookkeeping. touch() first so a track seen this
                # frame is not expired by the very next call.
                self.registry.touch(
                    [d.track_id for d in detections if d.track_id is not None],
                    now=now,
                )
                self.registry.expire(now=now)

                # Depth is measured once per frame and attached to the
                # detections *before* they enter the history. An RFID tap has
                # to be attributed to the person closest to the camera at the
                # tap instant, and that frame is long gone by the time the
                # HTTP request lands.
                measured = self._measure_depth(detections, depth)

                self.history.add(now, [d for d, _ in measured])
                self.crop_writer.maybe_save(color, detections, now=now)

                observations = self._build_observations(
                    detections, depth, now, measured=measured
                )

                with self._frame_lock:
                    self._latest_annotated = annotated
                    self._latest_depth = depth
                    self._observations = observations

                # Both of these are best-effort side channels. They are
                # wrapped individually so a failure in one cannot stop
                # capture or suppress the other.
                self._buffer_evidence_frame(annotated, now)
                self._publish_observations(observations, now)

                self.frames_processed += 1

                if now > previous:
                    self.fps = round(1.0 / (now - previous), 2)
                previous = now

                if self.settings.VERBOSE_FRAME_LOGGING:
                    logger.debug(
                        "frame %d: %d person(s), %d authorised",
                        self.frames_processed,
                        len(observations),
                        sum(1 for o in observations if o["is_authorized_picker"]),
                    )

            except Exception as exc:
                self.last_error = repr(exc)
                logger.exception("Capture loop error: %s", exc)
                break

        logger.info("Capture thread exited")

    # -- observations -----------------------------------------------------

    def _measure_depth(self, detections: List[PersonDetection], depth_image) -> list:
        """Pair every detection with its depth description, once per frame.

        Returns ``[(detection_with_depth, depth_dict), ...]``. The detection
        carries only ``depth_m``/``depth_valid`` -- enough to rank people by
        distance later -- while the full dict feeds the outbound observation.
        """

        intrinsics = self.source.intrinsics if self.source else None
        depth_scale = self.source.depth_scale if self.source else None

        measured = []
        for detection in detections:
            depth = describe_person_depth(
                bbox=detection.bbox,
                depth_image=depth_image if self.settings.DEPTH_ENABLED else None,
                intrinsics=intrinsics,
                depth_scale=depth_scale,
                patch_radius=self.settings.DEPTH_PATCH_RADIUS,
                min_m=self.settings.DEPTH_MIN_M,
                max_m=self.settings.DEPTH_MAX_M,
            )
            measured.append(
                (
                    detection.with_depth(depth["depth_m"], depth["depth_valid"]),
                    depth,
                )
            )
        return measured

    def _build_observations(
        self,
        detections: List[PersonDetection],
        depth_image=None,
        now: float = 0.0,
        measured: Optional[list] = None,
    ) -> List[dict]:
        """Observations for one frame.

        ``measured`` lets the capture loop hand over the depth it already
        computed for the history, so the depth patch is sampled once per
        person per frame rather than twice. Callers that do not have it --
        tests, and anything reconstructing a single frame -- pass detections
        and a depth image as before.
        """

        if measured is None:
            measured = self._measure_depth(detections, depth_image)

        session_id = self.registry.session_id
        location = None
        if self.location_service is not None:
            fix = self.location_service.current_fix()
            location = fix.to_dict() if fix else None

        observations: List[dict] = []
        for detection, depth in measured:
            binding = self.registry.binding_for_track(detection.track_id, now=now)

            observation = {
                "session_id": session_id,
                "source_id": getattr(self.settings, "SOURCE_ID", "GEOVISION"),
                "timestamp": _iso(now),
                "timestamp_epoch": now,
                "track_id": detection.track_id,
                "bbox": list(detection.bbox),
                "detection_confidence": round(detection.confidence, 3),
                "is_authorized_picker": binding is not None,
                "collector_id": binding.collector_id if binding else None,
                "rfid_id": binding.rfid_id if binding else None,
                "identity_confidence": binding.confidence if binding else None,
                # Preserved {x, y, z} shape -- the dashboard renders it.
                "camera_position_m": (
                    {
                        "x": depth["camera_x_m"],
                        "y": depth["camera_y_m"],
                        "z": depth["camera_z_m"],
                    }
                    if depth["depth_valid"]
                    else None
                ),
                "location": location,
            }
            for key in DEPTH_KEYS:
                observation[key] = depth[key]

            observations.append(observation)

        return observations

    # -- side channels ----------------------------------------------------

    def _buffer_evidence_frame(self, frame, now: float) -> None:
        if self.clip_buffer is None:
            return
        try:
            self.clip_buffer.add_frame(frame, timestamp=now)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Evidence buffering failed: %r", exc)

    def _publish_observations(self, observations: List[dict], now: float) -> None:
        """Hand due observations to the outbound publisher.

        The publisher enqueues and returns; no HTTP happens on this thread.
        Rate limiting is per track, so the camera keeps running at full FPS
        while WASTRAQ sees roughly ``WASTRAQ_TRACK_PUBLISH_HZ`` per person.
        """

        if self.publisher is None:
            return
        try:
            from integration.events import track_update_event

            for observation in observations:
                track_id = observation.get("track_id")
                if track_id is None:
                    continue
                if not self.publisher.should_publish_track(track_id, now=now):
                    continue
                event = track_update_event(
                    source_id=observation["source_id"],
                    observation=observation,
                    timestamp=now,
                    gps=(
                        observation.get("location")
                        if getattr(self.settings, "WASTRAQ_INCLUDE_GPS", True)
                        else None
                    ),
                    session_id=observation.get("session_id"),
                )
                if self.publisher.publish(event):
                    self.track_events_published += 1
                    self.publisher.last_track_sent_at = now
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Outbound publish failed: %r", exc)

    # -- accessors --------------------------------------------------------

    def observations(self) -> List[dict]:
        with self._frame_lock:
            return list(self._observations)

    @staticmethod
    def normalize(observation: dict) -> dict:
        """One observation in the shape ``/people`` and ``/tracks`` return.

        ``bbox`` is an object because that is what the WASTRAQ-facing schema
        specifies; ``bbox_xyxy``, ``id`` and ``confidence`` are kept as
        aliases so nothing that read the older shape breaks.
        """

        x1, y1, x2, y2 = observation["bbox"]
        normalized = {
            "track_id": observation["track_id"],
            # Backward-compatible aliases.
            "id": observation["track_id"],
            "confidence": observation["detection_confidence"],
            "detection_confidence": observation["detection_confidence"],
            "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "bbox_xyxy": [x1, y1, x2, y2],
            "is_authorized_picker": observation["is_authorized_picker"],
            "collector_id": observation["collector_id"],
            "identity_confidence": observation["identity_confidence"],
            "camera_position_m": observation["camera_position_m"],
        }
        for key in DEPTH_KEYS:
            normalized[key] = observation.get(key)
        return normalized

    def people(self) -> List[dict]:
        """Compact per-track view for the dashboard and for WASTRAQ."""

        return [self.normalize(o) for o in self.observations()]

    def tracks(self) -> dict:
        """The full normalised track payload, envelope included."""

        people = self.people()
        return {
            "timestamp": _iso(time.time()),
            "source_id": getattr(self.settings, "SOURCE_ID", "GEOVISION"),
            "session_id": self.registry.session_id,
            "count": len(people),
            "authorized_count": sum(
                1 for person in people if person["is_authorized_picker"]
            ),
            "people": people,
        }

    def jpeg_frame(self) -> Optional[bytes]:
        with self._frame_lock:
            frame = self._latest_annotated
        if frame is None:
            return None
        try:
            import cv2
        except Exception:  # pragma: no cover
            return None
        ok, buffer = cv2.imencode(".jpg", frame)
        return buffer.tobytes() if ok else None

    def describe_camera(self) -> dict:
        base = self.source.describe() if self.source else {
            "backend": self.settings.CAMERA_BACKEND,
            "open": False,
        }
        base["depth_available"] = self.depth_available
        base["last_error"] = self.last_error
        return base

    def stats(self) -> dict:
        duration = "00:00:00"
        if self.running and self.started_at:
            elapsed = int(time.time() - self.started_at)
            duration = (
                f"{elapsed // 3600:02}:"
                f"{(elapsed % 3600) // 60:02}:"
                f"{elapsed % 60:02}"
            )

        observations = self.observations()
        return {
            "camera": self.camera_connected,
            "recording": self.recording,
            "running": self.running,
            "fps": self.fps,
            "frames": self.frames_processed,
            "duration": duration,
            "resolution": f"{self.settings.CAMERA_WIDTH}x{self.settings.CAMERA_HEIGHT}",
            "people": len(observations),
            "authorized_pickers": sum(
                1 for o in observations if o["is_authorized_picker"]
            ),
            "session_id": self.registry.session_id,
            "backend": self.settings.CAMERA_BACKEND,
            "depth_available": self.depth_available,
            "source_id": getattr(self.settings, "SOURCE_ID", "GEOVISION"),
            "track_events_published": self.track_events_published,
            "detector_loaded": self.detector.loaded,
            "detector_error": self.detector.load_error,
            "last_error": self.last_error,
        }
