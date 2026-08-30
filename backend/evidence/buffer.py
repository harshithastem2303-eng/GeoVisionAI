"""A bounded in-memory ring of recent frames, and clip extraction from it.

Why JPEG in the ring rather than raw arrays
-------------------------------------------
640x480x3 bytes at 30 FPS for 20 seconds is roughly 550 MB of live numpy.
The same window JPEG-encoded at ``capture_hz`` is single-digit megabytes.
Evidence needs to be *reviewable*, not lossless, so the trade is free.

Why the ring is fed below camera FPS
------------------------------------
``capture_hz`` (default 10) decouples the evidence rate from the tracking
rate. Encoding every one of 30 frames per second would put a JPEG encode on
the critical path of the RealSense loop for no reviewer-visible benefit.

Why writing happens on its own thread
-------------------------------------
A clip covers ``T-pre .. T+post``: the future half does not exist yet when
the trigger arrives. Waiting for it, or encoding a video file, inside the
capture loop would stall frame acquisition. :meth:`RollingClipBuffer.request_clip`
returns immediately and a worker thread finishes the job.

Every dependency that might be missing (OpenCV) is injected with a default,
so the whole module is testable with no vision stack installed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ClipRequest:
    """One pending or completed evidence clip."""

    clip_id: str
    trigger_time: float
    start_time: float
    end_time: float
    track_id: Optional[int] = None
    rfid_event_id: Optional[str] = None
    #: WASTRAQ's episode handle, when the clip was triggered inside one.
    #: Carried, never invented: GeoVision mirrors episodes and opens none, so
    #: this is null for a clip captured outside a collection.
    episode_id: Optional[str] = None
    session_id: Optional[str] = None
    status: str = "PENDING"
    file_path: Optional[str] = None
    frame_count: int = 0
    error: Optional[str] = None
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "status": self.status,
            "file_path": self.file_path,
            "frame_count": self.frame_count,
            "track_id": self.track_id,
            "rfid_event_id": self.rfid_event_id,
            "episode_id": self.episode_id,
            "session_id": self.session_id,
            "trigger_time": self.trigger_time,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Default codecs (OpenCV), resolved lazily so import never needs it
# ---------------------------------------------------------------------------


def default_encoder(frame, quality: int = 70) -> Optional[bytes]:
    """Annotated BGR frame -> JPEG bytes, or ``None`` if it cannot be done."""

    try:
        import cv2
    except Exception:
        return None
    try:
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    except Exception as exc:  # pragma: no cover - depends on frame dtype
        logger.debug("Evidence encode failed: %r", exc)
        return None
    return buffer.tobytes() if ok else None


def default_writer(frames: List[bytes], path: Path, fps: float) -> Optional[str]:
    """JPEG frames -> an .mp4 beside ``path``.

    Falls back to a directory of numbered JPEGs when OpenCV (or the codec)
    is unavailable: a folder of stills is still usable evidence, whereas a
    failed write is none.

    Written to ``<clip>.part.mp4`` and renamed into place only after the
    encoder has been released and the result is a non-empty file. Since
    STEP 4B the clip is *servable over HTTP* the moment its path is
    announced, so the final name must never exist while it is still being
    filled -- a reviewer fetching a half-muxed MP4 sees a broken clip and
    concludes the evidence is broken.

    The temporary name keeps the ``.mp4`` extension deliberately: OpenCV's
    FFMPEG backend picks the muxer from the extension and refuses a bare
    ``.part``.
    """

    if not frames:
        return None

    try:
        import cv2
        import numpy as np
    except Exception:
        return _write_stills(frames, path)

    video_path = path.with_suffix(".mp4")
    # with_name, not with_suffix: a clip id is dot-free today, but a suffix
    # containing a dot is not something to rely on with_suffix accepting.
    partial_path = video_path.with_name(f"{video_path.stem}.part.mp4")

    try:
        first = cv2.imdecode(np.frombuffer(frames[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            return _write_stills(frames, path)
        height, width = first.shape[:2]

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(partial_path), fourcc, max(1.0, fps), (width, height)
        )
        if not writer.isOpened():
            return _write_stills(frames, path)

        for raw in frames:
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                writer.write(image)
        writer.release()

        if not partial_path.is_file() or partial_path.stat().st_size == 0:
            logger.warning(
                "Encoder produced no bytes for %s; falling back to stills",
                path.name,
            )
            _discard(partial_path)
            return _write_stills(frames, path)

        # Atomic on both NTFS and APFS for a same-directory replace.
        os.replace(partial_path, video_path)
        return str(video_path)
    except Exception as exc:
        logger.warning("Video write failed (%r); falling back to stills", exc)
        _discard(partial_path)
        return _write_stills(frames, path)


def _discard(path: Path) -> None:
    """Remove a half-written clip. Never raises: this runs in a finally-ish."""

    try:
        if path.exists():
            path.unlink()
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.debug("Could not remove partial clip %s: %r", path.name, exc)


def _write_stills(frames: List[bytes], path: Path) -> Optional[str]:
    directory = path.with_suffix("")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for index, raw in enumerate(frames):
            (directory / f"{index:05d}.jpg").write_bytes(raw)
        return str(directory)
    except Exception as exc:
        logger.error("Could not write evidence stills: %r", exc)
        return None


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class RollingClipBuffer:
    """Time-bounded ring of encoded frames plus asynchronous clip extraction."""

    def __init__(
        self,
        directory,
        buffer_s: float = 20.0,
        capture_hz: float = 10.0,
        pre_s: float = 10.0,
        post_s: float = 3.0,
        jpeg_quality: int = 70,
        enabled: bool = True,
        encoder: Optional[Callable] = None,
        writer: Optional[Callable] = None,
    ) -> None:
        self.directory = Path(directory)
        self.buffer_s = buffer_s
        self.capture_hz = capture_hz
        self.pre_s = pre_s
        self.post_s = post_s
        self.jpeg_quality = jpeg_quality
        self.enabled = enabled

        self._encoder = encoder or (lambda frame: default_encoder(frame, jpeg_quality))
        self._writer = writer or default_writer

        self._frames: Deque[Tuple[float, bytes]] = deque()
        self._lock = threading.Lock()

        self._clips: Deque[ClipRequest] = deque(maxlen=50)
        self._pending = 0
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()

        self.frames_stored = 0
        self.frames_skipped = 0
        self.clips_written = 0
        self.clips_failed = 0
        self.last_clip_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # -- ingest -----------------------------------------------------------

    @property
    def min_frame_interval_s(self) -> float:
        if not self.capture_hz or self.capture_hz <= 0:
            return float("inf")
        return 1.0 / float(self.capture_hz)

    def add_frame(self, frame, timestamp: Optional[float] = None) -> bool:
        """Offer a frame to the ring. Returns whether it was stored.

        Called from the capture loop, so it never raises: any failure to
        encode is counted and skipped. Losing evidence is bad; losing the
        tracker is worse.
        """

        if not self.enabled or frame is None:
            return False

        now = time.time() if timestamp is None else timestamp
        interval = self.min_frame_interval_s
        if interval == float("inf"):
            return False

        with self._lock:
            # Epsilon: a feed arriving at exactly capture_hz must not lose
            # every other frame to float subtraction (0.1 + 0.1 + 0.1 is not
            # 0.3), which would silently halve the evidence rate.
            if self._frames and (now - self._frames[-1][0]) < interval - 1e-9:
                return False

        try:
            encoded = self._encoder(frame)
        except Exception as exc:
            self.frames_skipped += 1
            self.last_error = f"encode: {exc!r}"
            return False

        if not encoded:
            self.frames_skipped += 1
            return False

        with self._lock:
            self._frames.append((now, encoded))
            self.frames_stored += 1
            self._trim(now)
        return True

    def _trim(self, now: float) -> None:
        """Drop anything older than the retention window. Caller holds the lock."""

        cutoff = now - self.buffer_s
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    def window(self, start: float, end: float) -> List[bytes]:
        with self._lock:
            return [data for ts, data in self._frames if start <= ts <= end]

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    # -- clip extraction --------------------------------------------------

    def request_clip(
        self,
        trigger_time: Optional[float] = None,
        track_id: Optional[int] = None,
        rfid_event_id: Optional[str] = None,
        session_id: Optional[str] = None,
        on_ready: Optional[Callable[[ClipRequest], None]] = None,
        blocking: bool = False,
        episode_id: Optional[str] = None,
    ) -> ClipRequest:
        """Ask for ``T-pre .. T+post`` around ``trigger_time``.

        Returns straight away with a ``PENDING`` request; ``on_ready`` is
        called with the completed request once the trailing seconds have
        elapsed and the file exists. ``blocking=True`` is for tests.
        """

        trigger = time.time() if trigger_time is None else trigger_time
        request = ClipRequest(
            clip_id=f"CLIP-{uuid.uuid4().hex[:12]}",
            trigger_time=trigger,
            start_time=trigger - self.pre_s,
            end_time=trigger + self.post_s,
            track_id=track_id,
            rfid_event_id=rfid_event_id,
            episode_id=episode_id,
            session_id=session_id,
        )

        if not self.enabled:
            request.status = "DISABLED"
            request.error = "Evidence buffer is disabled (GEOVISION_EVIDENCE_ENABLED)."
            self._clips.append(request)
            return request

        self._clips.append(request)
        self._pending += 1

        if blocking:
            self._finalise(request, on_ready, wait=False)
            return request

        thread = threading.Thread(
            target=self._finalise,
            args=(request, on_ready, True),
            name=f"evidence-{request.clip_id}",
            daemon=True,
        )
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.is_alive() or t is thread]
        thread.start()
        return request

    def _finalise(
        self,
        request: ClipRequest,
        on_ready: Optional[Callable[[ClipRequest], None]],
        wait: bool,
    ) -> None:
        try:
            if wait:
                # Wait out the trailing window; abandon early if we are
                # shutting down rather than holding the process open.
                remaining = max(0.0, request.end_time - time.time())
                if self._stop.wait(timeout=remaining):
                    request.status = "ABANDONED"
                    request.error = "Shutting down before the clip window closed."
                    return

            frames = self.window(request.start_time, request.end_time)
            request.frame_count = len(frames)

            if not frames:
                request.status = "EMPTY"
                request.error = (
                    "No buffered frames in the requested window -- was the "
                    "camera running?"
                )
                self.clips_failed += 1
                return

            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / request.clip_id
            path = self._writer(frames, target, self.capture_hz)

            if not path:
                request.status = "FAILED"
                request.error = "Clip writer produced no file."
                self.clips_failed += 1
                self.last_error = request.error
                return

            request.file_path = path
            request.status = "READY"
            self.clips_written += 1
            self.last_clip_at = time.time()
            logger.info(
                "Evidence clip %s written (%d frames) -> %s",
                request.clip_id,
                request.frame_count,
                path,
            )
        except Exception as exc:  # pragma: no cover - defensive
            request.status = "FAILED"
            request.error = repr(exc)
            self.clips_failed += 1
            self.last_error = repr(exc)
            logger.exception("Evidence clip %s failed", request.clip_id)
        finally:
            self._pending = max(0, self._pending - 1)
            if on_ready is not None:
                try:
                    on_ready(request)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Evidence callback failed: %r", exc)

    # -- introspection ----------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def clips(self) -> List[dict]:
        return [clip.to_dict() for clip in self._clips]

    def find(self, clip_id: str) -> Optional[ClipRequest]:
        for clip in self._clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    def stats(self) -> dict:
        with self._lock:
            buffered = len(self._frames)
            span = (
                round(self._frames[-1][0] - self._frames[0][0], 2)
                if len(self._frames) > 1
                else 0.0
            )
            bytes_held = sum(len(data) for _ts, data in self._frames)
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "buffered_frames": buffered,
            "buffered_seconds": span,
            "buffered_bytes": bytes_held,
            "buffer_s": self.buffer_s,
            "capture_hz": self.capture_hz,
            "pre_s": self.pre_s,
            "post_s": self.post_s,
            "frames_stored": self.frames_stored,
            "frames_skipped": self.frames_skipped,
            "clips_pending": self._pending,
            "clips_written": self.clips_written,
            "clips_failed": self.clips_failed,
            "last_clip_at": self.last_clip_at,
            "last_error": self.last_error,
        }
