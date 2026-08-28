"""
evidence_buffer.py
==================

Rolling video-evidence recorder for NOT_SEGREGATED events.

    VisionPipeline._run / _encode   (app/vision/pipeline.py)
          |  add_frame(jpeg, timestamp)          <- never blocks
          v
    RollingEvidenceBuffer                        <- last ~20 s, by timestamp
          |  trigger(...)                        <- returns in ~1 ms
          v
    EvidenceRecorder                             <- ids, filenames, registry
          |  worker thread: sleep POST_ROLL, snapshot, decode, encode
          v
    EvidenceWriter -> evidence/evidence_<prop>_<picker>_<stamp>.mp4 + .json

Design rules
------------
* The capture loop NEVER blocks. `add_frame()` is one append plus a cheap
  age-prune under the buffer's own lock. All JPEG decoding and MP4
  encoding happens on a worker thread, outside every capture lock.
* Retention is **timestamp-based**, not deque.maxlen-based. Frames older
  than BUFFER_SECONDS are purged on append. A deque maxlen would silently
  drop the oldest pre-trigger footage the moment the frame rate rose; the
  timestamp window cannot. HARD_MAX_FRAMES is only a memory backstop.
* The buffer holds 20 s so that a 12 s pre-roll survives the 3 s post-roll
  wait with margin. Never set BUFFER_SECONDS to PRE+POST exactly.
* Frames are stored as JPEG bytes, not ndarrays. `VisionPipeline._encode()`
  already encodes a JPEG for the MJPEG stream, so buffering costs no extra
  CPU at all, and 20 s of 640x480 fits in ~20-30 MB instead of ~400 MB.
  The cost is decoding them again at write time -- which happens only on an
  evidence event, on a worker thread, so it is free where it matters.
* This module knows nothing about YOLO, ByteTrack, RFID, GIS, the property
  matcher or PostgreSQL. Identifiers are opaque strings, so the RealSense
  pipeline, the episode detector and the GIS matcher can all supply them
  later without this module changing.
* Nothing here raises into the caller. A failure is logged, recorded as
  status=FAILED in the metadata, and the demo carries on.

Tuning (all optional environment variables)
-------------------------------------------
    EVIDENCE_DIR                 default <backend>/evidence
    EVIDENCE_BUFFER_SECONDS      20    seconds retained in RAM
    EVIDENCE_PRE_ROLL_SECONDS    12    seconds saved before the trigger
    EVIDENCE_POST_ROLL_SECONDS   3     seconds saved after the trigger
    EVIDENCE_FALLBACK_FPS        10    used when FPS cannot be measured
    EVIDENCE_JPEG_QUALITY        80    only used when we must encode
    EVIDENCE_MAX_FRAMES          2000  memory backstop, not the retention rule
    EVIDENCE_MIN_RETRIGGER_S     1.5   double-click guard, 0 disables
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import cv2
import numpy as np


# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------

# backend/app/evidence_buffer.py  ->  <repo root>/evidence/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))

EVIDENCE_DIR = os.environ.get(
    "EVIDENCE_DIR", os.path.join(REPO_ROOT, "evidence")
)


def _env_float(name, default):
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    return int(_env_float(name, default))


# Seconds of footage kept in RAM. MUST exceed PRE_ROLL + POST_ROLL, or the
# oldest pre-trigger frames get purged while the post-roll is still being
# collected.
BUFFER_SECONDS = _env_float("EVIDENCE_BUFFER_SECONDS", 20.0)

PRE_ROLL_SECONDS = _env_float("EVIDENCE_PRE_ROLL_SECONDS", 12.0)
POST_ROLL_SECONDS = _env_float("EVIDENCE_POST_ROLL_SECONDS", 3.0)

# Target clip length -- informational, derived from the two above.
TARGET_CLIP_SECONDS = PRE_ROLL_SECONDS + POST_ROLL_SECONDS

# Used before enough frames have arrived to measure the real rate, and for
# any source too irregular to time. The D455 is configured for
# VISION_FPS=30, but the pipeline publishes slower than it captures, so a
# conservative 10 is the fallback -- it is only ever used when the real
# rate cannot be measured, which in practice is the first ~5 frames.
FALLBACK_FPS = _env_float("EVIDENCE_FALLBACK_FPS", 10.0)

JPEG_QUALITY = _env_int("EVIDENCE_JPEG_QUALITY", 80)

# Pure memory backstop for a pathological frame rate. Retention is decided
# by timestamps; this only stops the process from being OOM-killed.
HARD_MAX_FRAMES = _env_int("EVIDENCE_MAX_FRAMES", 2000)

# Two clicks on the same house within this many seconds are one event.
MIN_RETRIGGER_SECONDS = _env_float("EVIDENCE_MIN_RETRIGGER_S", 1.5)

# Two operators mashing the button must not spawn unbounded encoder threads.
MAX_CONCURRENT_WRITES = 3

DEFAULT_CAMERA_ID = "vision-pipeline"

STATUS_RECORDING = "RECORDING"
STATUS_SAVED = "SAVED"
STATUS_FAILED = "FAILED"

if BUFFER_SECONDS < PRE_ROLL_SECONDS + POST_ROLL_SECONDS:
    BUFFER_SECONDS = PRE_ROLL_SECONDS + POST_ROLL_SECONDS + 5.0


# ---------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------

log = logging.getLogger("wastraq.evidence")

if not log.handlers and not logging.getLogger().handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [EVIDENCE] %(levelname)s %(message)s")
    )
    log.addHandler(_handler)

log.setLevel(os.environ.get("EVIDENCE_LOG_LEVEL", "INFO").upper())
log.propagate = True


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value, fallback):
    """Filename-safe token. Never empty, never a path traversal."""
    text = _SAFE.sub("-", str(value or "").strip())
    text = text.strip("-.") or fallback
    return text[:48]


def _iso(ts):
    """Local ISO-8601 with offset, millisecond precision."""
    if ts is None:
        return None
    try:
        return (
            datetime.fromtimestamp(float(ts))
            .astimezone()
            .isoformat(timespec="milliseconds")
        )
    except Exception:
        return None


def _encode_jpeg(frame):
    try:
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
        )
        return buf.tobytes() if ok else None
    except Exception as e:
        log.warning("JPEG encode failed: %r", e)
        return None


def _decode_jpeg(data):
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8),
                            cv2.IMREAD_COLOR)
    except Exception:
        return None


# ===============================================================
# ROLLING BUFFER
# ===============================================================

class RollingEvidenceBuffer:
    """
    The most recent `window_seconds` of one camera, as (timestamp, jpeg).

    Retention is by timestamp. Every append purges the head of the deque
    until the oldest frame is within the window, so the buffer always holds
    the last N seconds regardless of how the frame rate moves.
    """

    def __init__(
        self,
        camera_id=DEFAULT_CAMERA_ID,
        window_seconds=BUFFER_SECONDS,
        fallback_fps=FALLBACK_FPS,
    ):
        self.camera_id = camera_id
        self.window_seconds = max(1.0, float(window_seconds))
        self.fallback_fps = (
            float(fallback_fps) if fallback_fps and fallback_fps > 0 else 10.0
        )

        self._lock = threading.Lock()
        self._frames = deque()          # (timestamp, jpeg bytes)

        self._intervals = deque(maxlen=60)
        self._last_ts = None

        self.measured_fps = 0.0
        self.frames_seen = 0
        self.frames_purged = 0
        self._announced = False

    # ---------------------------------------------------------

    def add(self, frame=None, jpeg=None, timestamp=None):
        """
        Append one frame. Returns True if it was stored.

        Called once per frame from the capture loop, so it must stay cheap:
        one append, a short purge loop, no I/O.

        `jpeg` is preferred -- the pipeline has already encoded one for the
        MJPEG stream. `frame` (a BGR ndarray) is encoded here as a fallback.
        """

        ts = float(timestamp) if timestamp is not None else time.time()

        data = jpeg
        if data is None:
            if frame is None:
                return False
            data = _encode_jpeg(frame)
        if not data:
            return False

        with self._lock:

            if self._last_ts is not None:
                dt = ts - self._last_ts
                # Ignore absurd gaps (a reconnect) and non-monotonic clocks.
                if 0.0 < dt < 5.0:
                    self._intervals.append(dt)

                if len(self._intervals) >= 5:
                    mean = sum(self._intervals) / len(self._intervals)
                    if mean > 0:
                        self.measured_fps = round(1.0 / mean, 2)

            self._last_ts = ts

            self._frames.append((ts, data))
            self.frames_seen += 1

            # ---- timestamp retention (the actual rule) ----
            cutoff = ts - self.window_seconds
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()
                self.frames_purged += 1

            # ---- memory backstop only ----
            while len(self._frames) > HARD_MAX_FRAMES:
                self._frames.popleft()
                self.frames_purged += 1

        if not self._announced:
            self._announced = True
            log.info(
                "buffer initialized camera=%s window=%.1fs "
                "pre_roll=%.1fs post_roll=%.1fs fallback_fps=%.1f",
                self.camera_id, self.window_seconds,
                PRE_ROLL_SECONDS, POST_ROLL_SECONDS, self.fallback_fps,
            )

        return True

    def snapshot(self, start_ts, end_ts):
        """
        Copy out every buffered frame whose timestamp is in
        [start_ts, end_ts].

        Only the list of references is built under the lock -- the JPEG
        payloads are immutable bytes, so decoding and encoding happen
        entirely outside it while the capture loop keeps appending.
        """
        with self._lock:
            return [
                (ts, data)
                for ts, data in self._frames
                if start_ts <= ts <= end_ts
            ]

    def effective_fps(self):
        return self.measured_fps if self.measured_fps > 0 else self.fallback_fps

    def is_stale(self, seconds=5.0):
        with self._lock:
            return self._last_ts is None or (time.time() - self._last_ts) > seconds

    def stats(self):
        with self._lock:
            count = len(self._frames)
            span = (self._frames[-1][0] - self._frames[0][0]) if count > 1 else 0.0
            held = sum(len(d) for _, d in self._frames)
            last = self._last_ts
        return {
            "camera_id": self.camera_id,
            "buffered_frames": count,
            "buffered_seconds": round(span, 2),
            "buffered_bytes": held,
            "window_seconds": self.window_seconds,
            "measured_fps": self.measured_fps,
            "effective_fps": round(self.effective_fps(), 2),
            "frames_seen": self.frames_seen,
            "frames_purged": self.frames_purged,
            "last_frame_age_s": round(time.time() - last, 2) if last else None,
            "live": not self.is_stale(),
        }


# ===============================================================
# WRITER
# ===============================================================

class EvidenceWriter:
    """Turns a list of (timestamp, jpeg) into a playable MP4."""

    # avc1 (H.264) first for the widest player support; mp4v is the
    # fallback that OpenCV can always mux on a plain FFMPEG build.
    CODECS = ("avc1", "mp4v")

    @staticmethod
    def write(frames, path, fps):
        """
        Encode to `<name>.part.mp4`, close the writer, then atomically
        rename to `<name>.mp4`.

        The temp name MUST keep an .mp4 extension -- OpenCV/FFMPEG pick the
        muxer from it, and a name ending in ".part" simply fails to open.
        The rename means a half-written clip is never visible at the real
        path, and two concurrent triggers can never share a file.
        """

        if not frames:
            return {"ok": False, "error": "no frames to write"}

        fps = max(1.0, min(60.0, float(fps or FALLBACK_FPS)))

        first = None
        for _, data in frames:
            first = _decode_jpeg(data)
            if first is not None:
                break

        if first is None:
            return {"ok": False, "error": "no decodable frames"}

        height, width = first.shape[:2]

        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)

        base, _ = os.path.splitext(path)
        temp_path = f"{base}.part.mp4"

        last_error = None

        for codec in EvidenceWriter.CODECS:

            writer = None
            written = 0

            try:
                fourcc = cv2.VideoWriter_fourcc(*codec)
                writer = cv2.VideoWriter(temp_path, fourcc, fps, (width, height))

                if not writer.isOpened():
                    last_error = f"codec {codec} unavailable"
                    writer.release()
                    writer = None
                    continue

                for _, data in frames:
                    img = _decode_jpeg(data)
                    if img is None:
                        continue
                    if img.shape[0] != height or img.shape[1] != width:
                        img = cv2.resize(img, (width, height))
                    writer.write(img)
                    written += 1

            except Exception as e:
                last_error = repr(e)
            finally:
                if writer is not None:
                    try:
                        writer.release()   # must close before the rename
                    except Exception:
                        pass

            size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0

            if written > 0 and size > 0:
                os.replace(temp_path, path)     # atomic within the directory
                return {
                    "ok": True,
                    "codec": codec,
                    "fps": round(fps, 2),
                    "width": width,
                    "height": height,
                    "frames_written": written,
                    "file_size_bytes": os.path.getsize(path),
                    "duration_seconds": round(written / fps, 2),
                }

            last_error = last_error or f"codec {codec} produced an empty file"
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        return {"ok": False, "error": last_error or "no usable codec"}


# ===============================================================
# RECORDER
# ===============================================================

class EvidenceRecorder:
    """One per process. Owns the per-camera buffers and the clip registry."""

    def __init__(self, directory=EVIDENCE_DIR):
        self.directory = directory
        self._buffers = {}
        self._records = {}
        self._order = []
        self._lock = threading.RLock()
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT_WRITES)
        self._sequence = 0
        self._last_trigger = {}
        self._default_camera_id = None

        try:
            os.makedirs(self.directory, exist_ok=True)
        except Exception as e:
            log.error("could not create evidence directory %s: %r",
                      self.directory, e)

    # ---------------------------------------------------------
    # CAPTURE SIDE
    # ---------------------------------------------------------

    def get_buffer(self, camera_id=DEFAULT_CAMERA_ID):
        camera_id = camera_id or DEFAULT_CAMERA_ID
        with self._lock:
            buf = self._buffers.get(camera_id)
            if buf is None:
                buf = RollingEvidenceBuffer(camera_id=camera_id)
                self._buffers[camera_id] = buf
                if self._default_camera_id is None:
                    self._default_camera_id = camera_id
            return buf

    def add_frame(self, frame=None, jpeg=None,
                  camera_id=DEFAULT_CAMERA_ID, timestamp=None):
        """
        Called from the capture loop, once per frame. Cheap, and it never
        raises -- a buffering problem must not be able to stop the camera.
        """
        try:
            buf = self.get_buffer(camera_id)
            ok = buf.add(frame=frame, jpeg=jpeg, timestamp=timestamp)
            if ok:
                self._default_camera_id = buf.camera_id
            return ok
        except Exception as e:
            log.warning("add_frame failed: %r", e)
            return False

    # ---------------------------------------------------------
    # TRIGGER SIDE
    # ---------------------------------------------------------

    def trigger(
        self,
        property_id=None,
        picker_id=None,
        camera_id=None,
        event_type="NOT_SEGREGATED",
        event_timestamp=None,
        pre_seconds=None,
        post_seconds=None,
        extra=None,
    ):
        """
        Record the trigger instant, then hand the work to a thread.

        The trigger timestamp is taken HERE, on the request thread, the
        moment NOT_SEGREGATED arrives -- not after the post-roll -- so the
        clip window is anchored to when the operator actually decided.

        Returns metadata immediately with status=RECORDING. The worker
        sleeps out the post-roll, selects
        [trigger - PRE_ROLL, trigger + POST_ROLL] by timestamp, encodes,
        and flips the record to SAVED (or FAILED). Never raises.
        """

        trigger_ts = float(event_timestamp) if event_timestamp else time.time()

        pre = PRE_ROLL_SECONDS if pre_seconds is None else max(0.0, float(pre_seconds))
        post = POST_ROLL_SECONDS if post_seconds is None else max(0.0, float(post_seconds))

        camera_id = camera_id or self._default_camera_id or DEFAULT_CAMERA_ID

        log.info(
            "evidence trigger received property=%s picker=%s camera=%s "
            "type=%s at=%s",
            property_id, picker_id, camera_id, event_type, _iso(trigger_ts),
        )

        with self._lock:

            # ---- double-click guard ----------------------------------
            # Two clicks on the same house inside the window are one event
            # and return the same record. A different house, a different
            # camera, or a later click is always a new clip.
            if MIN_RETRIGGER_SECONDS > 0:
                key = (camera_id, str(property_id), str(event_type))
                previous = self._last_trigger.get(key)
                if previous and (trigger_ts - previous[0]) < MIN_RETRIGGER_SECONDS:
                    log.info(
                        "evidence trigger de-bounced (%.2fs since %s)",
                        trigger_ts - previous[0], previous[1],
                    )
                    existing = self._records.get(previous[1])
                    if existing:
                        out = dict(existing)
                        out["deduplicated"] = True
                        return out

            self._sequence += 1
            sequence = self._sequence

            buf = self._buffers.get(camera_id)

            stamp = datetime.fromtimestamp(trigger_ts).strftime("%Y%m%d_%H%M%S")
            evidence_id = f"EV-{stamp}-{sequence:03d}-{uuid.uuid4().hex[:6]}"

            filename = self._unique_filename(property_id, picker_id, stamp)
            video_path = os.path.join(self.directory, filename)

            meta = {
                "evidence_id": evidence_id,
                "picker_id": picker_id,
                "property_id": property_id,
                # Demo 2 identifies properties as HOUSE_xxx; carry both names
                # so the GIS side can use property_id later without a change.
                "house_id": property_id,
                "event_type": event_type,
                "trigger_timestamp": _iso(trigger_ts),
                "event_timestamp": _iso(trigger_ts),
                "clip_start_timestamp": _iso(trigger_ts - pre),
                "clip_end_timestamp": _iso(trigger_ts + post),
                "duration_seconds": None,
                "video_path": video_path,
                "camera_id": camera_id,
                # --- convenience, not part of the required contract ---
                "video_filename": filename,
                "video_url": f"/evidence_files/{filename}",
                "status": STATUS_RECORDING,
                "pre_roll_seconds": pre,
                "post_roll_seconds": post,
                "sequence": sequence,
                "error": None,
            }

            if extra:
                meta["extra"] = extra

            if buf is None:
                meta["status"] = STATUS_FAILED
                meta["error"] = (
                    f"no frame buffer for camera '{camera_id}' -- is the "
                    f"vision pipeline running? (POST /vision/start)"
                )
                self._store(meta)
                log.error("evidence creation failure %s: %s",
                          evidence_id, meta["error"])
                return dict(meta)

            self._store(meta)
            self._last_trigger[(camera_id, str(property_id), str(event_type))] = (
                trigger_ts, evidence_id
            )

        threading.Thread(
            target=self._finalise,
            args=(evidence_id, buf, trigger_ts, pre, post),
            name=f"evidence-writer-{sequence}",
            daemon=True,
        ).start()

        return dict(meta)

    # ---------------------------------------------------------

    def _finalise(self, evidence_id, buf, trigger_ts, pre, post):
        """Worker thread: wait out the post-roll, then select and encode."""

        try:
            if post > 0:
                # The capture loop keeps filling the buffer while we sleep;
                # the 20 s window guarantees the pre-roll is still there
                # when we wake up.
                time.sleep(post)

            start_ts = trigger_ts - pre
            end_ts = trigger_ts + post

            frames = buf.snapshot(start_ts, end_ts)

            if not frames:
                # Triggered before enough footage existed, or the camera
                # died: save whatever is valid rather than failing.
                frames = buf.snapshot(0.0, end_ts)

            if not frames:
                self._fail(evidence_id, "buffer was empty at trigger time")
                return

            meta = self.get(evidence_id) or {}
            path = meta.get("video_path")

            fps = self._clip_fps(frames, buf)

            before = sum(1 for ts, _ in frames if ts <= trigger_ts)

            log.info(
                "clip writing started %s frames=%d (%d before / %d after "
                "trigger) fps=%.2f -> %s",
                evidence_id, len(frames), before, len(frames) - before, fps,
                os.path.basename(path or ""),
            )

            with self._sem:
                result = EvidenceWriter.write(frames, path, fps)

            if not result.get("ok"):
                self._fail(evidence_id, result.get("error", "write failed"))
                return

            snapshot = None

            with self._lock:
                record = self._records.get(evidence_id)
                if record is not None:
                    record["status"] = STATUS_SAVED
                    record["clip_start_timestamp"] = _iso(frames[0][0])
                    record["clip_end_timestamp"] = _iso(frames[-1][0])
                    record["duration_seconds"] = result["duration_seconds"]
                    record["frame_count"] = result["frames_written"]
                    record["frames_before_trigger"] = before
                    record["frames_after_trigger"] = len(frames) - before
                    record["fps"] = result["fps"]
                    record["codec"] = result["codec"]
                    record["width"] = result["width"]
                    record["height"] = result["height"]
                    record["file_size_bytes"] = result["file_size_bytes"]
                    record["wallclock_span_seconds"] = round(
                        frames[-1][0] - frames[0][0], 2
                    )
                    record["error"] = None
                    snapshot = dict(record)

            self._write_sidecar(snapshot)

            log.info(
                "clip successfully saved %s -> %s (%d frames, %.2fs, "
                "%.2f fps, %d bytes, codec=%s)",
                evidence_id, path, result["frames_written"],
                result["duration_seconds"], result["fps"],
                result["file_size_bytes"], result["codec"],
            )

        except Exception as e:
            self._fail(evidence_id, repr(e))

    def _clip_fps(self, frames, buf):
        """
        Playback rate, taken from the clip's own timestamps so the video
        plays at real speed. Falls back to the buffer's measured rate, then
        to EVIDENCE_FALLBACK_FPS.
        """
        if len(frames) > 1:
            span = frames[-1][0] - frames[0][0]
            if span > 0.2:
                return max(1.0, min(60.0, (len(frames) - 1) / span))
        return buf.effective_fps()

    def _fail(self, evidence_id, message):
        snapshot = None
        with self._lock:
            record = self._records.get(evidence_id)
            if record is not None:
                record["status"] = STATUS_FAILED
                record["error"] = message
                snapshot = dict(record)
        log.error("evidence creation failure %s: %s", evidence_id, message)
        if snapshot:
            self._write_sidecar(snapshot)

    def _write_sidecar(self, meta):
        """
        JSON next to the MP4. Deliberately not a database migration: the
        demo has no evidence table, and this feature does not need one.
        """
        try:
            path = meta.get("video_path")
            if not path:
                return
            with open(os.path.splitext(path)[0] + ".json", "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            log.warning("could not write metadata sidecar: %r", e)

    def _unique_filename(self, property_id, picker_id, stamp):
        base = (
            f"evidence_{_slug(property_id, 'UNKNOWN-PROPERTY')}"
            f"_{_slug(picker_id, 'UNKNOWN-PICKER')}"
            f"_{stamp}"
        )
        candidate = f"{base}.mp4"
        n = 2
        while os.path.exists(os.path.join(self.directory, candidate)):
            candidate = f"{base}_{n}.mp4"
            n += 1
        return candidate

    def _store(self, meta):
        self._records[meta["evidence_id"]] = meta
        self._order.append(meta["evidence_id"])
        # Bound the in-memory registry; the JSON sidecars are the durable
        # record.
        while len(self._order) > 500:
            self._records.pop(self._order.pop(0), None)

    # ---------------------------------------------------------
    # READ SIDE
    # ---------------------------------------------------------

    def get(self, evidence_id):
        with self._lock:
            record = self._records.get(evidence_id)
            return dict(record) if record else None

    def list(self, limit=50):
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [dict(self._records[i]) for i in ids if i in self._records]

    def wait_for(self, evidence_id, timeout=30.0):
        """Block until the clip is SAVED or FAILED. For tests and for
        POST /evidence/not-segregated?wait=true only -- never called from
        the capture loop or the dashboard's normal path."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.get(evidence_id)
            if record is None:
                return None
            if record.get("status") != STATUS_RECORDING:
                return record
            time.sleep(0.1)
        return self.get(evidence_id)

    def status(self):
        with self._lock:
            buffers = [b.stats() for b in self._buffers.values()]
            total = len(self._order)
            default_camera = self._default_camera_id
        return {
            "evidence_dir": self.directory,
            "default_camera_id": default_camera,
            "buffer_seconds": BUFFER_SECONDS,
            "pre_roll_seconds": PRE_ROLL_SECONDS,
            "post_roll_seconds": POST_ROLL_SECONDS,
            "target_clip_seconds": TARGET_CLIP_SECONDS,
            "fallback_fps": FALLBACK_FPS,
            "min_retrigger_seconds": MIN_RETRIGGER_SECONDS,
            "buffers": buffers,
            "evidence_count": total,
            "recent": self.list(limit=5),
        }


# The singleton the rest of the backend imports.
evidence_recorder = EvidenceRecorder()


def trigger_evidence(property_id=None, picker_id=None, camera_id=None,
                     event_type="NOT_SEGREGATED", **kwargs):
    """Convenience wrapper so callers need one import, not two."""
    return evidence_recorder.trigger(
        property_id=property_id,
        picker_id=picker_id,
        camera_id=camera_id,
        event_type=event_type,
        **kwargs
    )
