"""
test_evidence_buffer.py
=======================

End-to-end test for the rolling video evidence feature. No camera, no
FastAPI, no YOLO -- a synthetic capture thread stands in for the camera so
this can be run anywhere.

    .venv/bin/python scripts/test_evidence_buffer.py

What it proves
--------------
 1. the buffer initialises and fills
 2. after 16 s the buffer holds the configured window and nothing more
 3. a NOT_SEGREGATED trigger produces a real, playable MP4
 4. the clip contains the seconds IMMEDIATELY BEFORE the trigger -- each
    synthetic frame carries its own frame number encoded as a 12-bit
    block pattern, which is read back out of the decoded MP4
 5. the capture thread keeps running while the clip is written
 6. a second trigger produces a second, independent clip
 7. duration / FPS are checked with ffprobe when it is available

Exit code is 0 only if every check passes.
"""

import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

os.environ.setdefault("EVIDENCE_DIR", os.path.join(REPO_ROOT, "evidence"))

from app.evidence_buffer import (  # noqa: E402
    PRE_ROLL_SECONDS,
    POST_ROLL_SECONDS,
    evidence_recorder,
)


WIDTH, HEIGHT = 640, 480
SOURCE_FPS = 15.0
CAMERA_ID = "test-camera"

BLOCK = 30          # size of one id block, px
BITS = 12           # frame ids 0..4095 then wrap

failures = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)
    return condition


# ---------------------------------------------------------------
# SYNTHETIC CAMERA
# ---------------------------------------------------------------

def stamp_id(frame, n):
    """Burn `n` into the top-left corner as BITS black/white blocks."""
    for i in range(BITS):
        value = 255 if (n >> i) & 1 else 0
        x = i * BLOCK
        frame[0:BLOCK, x:x + BLOCK] = value
    return frame


def read_id(image):
    """Read the block pattern back out of a decoded frame."""
    n = 0
    for i in range(BITS):
        x = i * BLOCK + BLOCK // 2
        y = BLOCK // 2
        if int(image[y, x].mean()) > 127:
            n |= (1 << i)
    return n


class SyntheticCamera(threading.Thread):
    """Stands in for demo_pipeline's capture loop."""

    daemon = True

    def __init__(self):
        super().__init__(name="synthetic-camera")
        self.running = True
        self.frames_produced = 0
        self.frame_times = {}      # frame number -> wall clock
        self._lock = threading.Lock()

    def run(self):
        interval = 1.0 / SOURCE_FPS
        n = 0
        while self.running:
            start = time.time()

            frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
            frame[:, :] = (30, 30, 40)
            cv2.rectangle(
                frame,
                ((n * 9) % (WIDTH - 90), 200),
                ((n * 9) % (WIDTH - 90) + 80, 300),
                (0, 200, 255), -1,
            )
            cv2.putText(frame, f"frame {n}", (20, 120),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            cv2.putText(frame, time.strftime("%H:%M:%S"), (20, 400),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (200, 255, 200), 2)
            stamp_id(frame, n % (1 << BITS))

            ok, buf = cv2.imencode(".jpg", frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            now = time.time()

            if ok:
                # exactly what demo_pipeline._publish does
                evidence_recorder.add_frame(
                    jpeg=buf.tobytes(), camera_id=CAMERA_ID, timestamp=now
                )
                with self._lock:
                    self.frame_times[n % (1 << BITS)] = now
                    self.frames_produced += 1

            n += 1

            spent = time.time() - start
            if interval > spent:
                time.sleep(interval - spent)

    def count(self):
        with self._lock:
            return self.frames_produced

    def time_of(self, frame_id):
        with self._lock:
            return self.frame_times.get(frame_id)


# ---------------------------------------------------------------
# VERIFICATION
# ---------------------------------------------------------------

def probe(path):
    """ffprobe summary, or None if ffprobe is unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,nb_read_frames,avg_frame_rate,width,height"
             ":format=duration",
             "-count_frames", "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        return dict(
            line.split("=", 1)
            for line in out.stdout.strip().splitlines() if "=" in line
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def frame_ids_in(path):
    """Every id encoded in the MP4, in order."""
    cap = cv2.VideoCapture(path)
    ids = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        ids.append(read_id(img))
    cap.release()
    return ids


def verify_clip(label, record, camera, trigger_time):

    print(f"\n--- {label}: {record['evidence_id']} ---")

    path = record.get("video_path")

    check("status is SAVED", record.get("status") == "SAVED",
          record.get("error") or record.get("status"))

    if not path or not os.path.exists(path):
        check("MP4 exists on disk", False, str(path))
        return

    size = os.path.getsize(path)
    check("MP4 exists and is non-empty", size > 1000, f"{size} bytes")
    import glob as _glob
    check("no stray .part file left behind",
          not _glob.glob(os.path.join(os.path.dirname(path), ".*.part.mp4")))
    check("metadata sidecar written",
          os.path.exists(os.path.splitext(path)[0] + ".json"))

    for field in ("evidence_id", "picker_id", "property_id", "house_id",
                  "event_type", "trigger_timestamp", "event_timestamp",
                  "clip_start_timestamp", "clip_end_timestamp",
                  "duration_seconds", "video_path", "camera_id"):
        check(f"metadata field present: {field}", field in record)

    ids = frame_ids_in(path)
    check("clip is decodable", len(ids) > 0, f"{len(ids)} frames decoded")

    if not ids:
        return

    duration = record.get("duration_seconds", 0)
    expected = PRE_ROLL_SECONDS + POST_ROLL_SECONDS

    check(
        f"duration is 10-15 s (target ~{expected:.0f}s)",
        10.0 <= duration <= 15.5,
        f"{duration:.2f}s over {record.get('frame_count')} frames "
        f"at {record.get('fps')} fps",
    )

    check("frame count matches decoded frames",
          abs(len(ids) - record.get("frame_count", 0)) <= 1,
          f"decoded {len(ids)} vs recorded {record.get('frame_count')}")

    # --- the important one: is this the footage BEFORE the trigger? ---

    first_seen = camera.time_of(ids[0])
    last_seen = camera.time_of(ids[-1])

    if first_seen and last_seen:
        lead = trigger_time - first_seen
        lag = last_seen - trigger_time

        check(
            "clip STARTS ~PRE_ROLL seconds before the trigger",
            abs(lead - PRE_ROLL_SECONDS) < 1.5,
            f"first frame is {lead:.2f}s before the trigger "
            f"(pre-roll = {PRE_ROLL_SECONDS}s)",
        )
        check(
            "clip ENDS after the trigger (post-roll captured)",
            0.0 < lag <= POST_ROLL_SECONDS + 1.5,
            f"last frame is {lag:+.2f}s from the trigger "
            f"(post-roll = {POST_ROLL_SECONDS}s)",
        )

        # The point of the whole feature: footage from BOTH sides of the
        # decision, read back out of the encoded MP4 itself.
        times = [camera.time_of(i) for i in ids]
        times = [t for t in times if t]
        before = sum(1 for t in times if t <= trigger_time)
        after = len(times) - before

        check(
            "MP4 contains frames from BEFORE the trigger",
            before > 10, f"{before} pre-trigger frames in the file",
        )
        check(
            "MP4 contains frames from AFTER the trigger",
            after > 10, f"{after} post-trigger frames in the file",
        )
    else:
        check("frame ids traceable to capture times", False)

    check("frame ids are monotonic (no shuffled/duplicated frames)",
          all(b - a in (1, 1 - (1 << BITS)) for a, b in zip(ids, ids[1:])),
          f"ids {ids[0]} .. {ids[-1]}")

    info = probe(path)
    if info:
        check("ffprobe reads the file",
              info.get("codec_name") is not None, str(info))
        try:
            check("ffprobe duration is 10-15 s",
                  10.0 <= float(info.get("duration", 0)) <= 15.5,
                  f"{float(info.get('duration', 0)):.2f}s")
            check("ffprobe frame count matches",
                  abs(int(info.get("nb_read_frames", 0)) - len(ids)) <= 1,
                  f"{info.get('nb_read_frames')} frames, "
                  f"rate {info.get('avg_frame_rate')}")
        except (TypeError, ValueError):
            pass
    else:
        print("  [SKIP] ffprobe not installed -- OpenCV decode used instead")


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------

def main():

    print("=" * 66)
    print("ROLLING EVIDENCE BUFFER -- END TO END TEST")
    print("=" * 66)
    print(f"evidence dir : {evidence_recorder.directory}")
    print(f"pre-roll     : {PRE_ROLL_SECONDS}s")
    print(f"post-roll    : {POST_ROLL_SECONDS}s")
    print(f"source       : synthetic {WIDTH}x{HEIGHT} @ {SOURCE_FPS} fps")

    camera = SyntheticCamera()
    camera.start()

    print("\n[1] Camera started, filling buffer for 16 s...")

    for remaining in range(16, 0, -1):
        time.sleep(1)
        if remaining % 4 == 0 or remaining <= 2:
            s = evidence_recorder.get_buffer(CAMERA_ID).stats()
            print(f"    t-{remaining:>2}s  buffered={s['buffered_frames']:>4} "
                  f"frames  {s['buffered_seconds']:>5.1f}s  "
                  f"measured_fps={s['measured_fps']}")

    stats = evidence_recorder.get_buffer(CAMERA_ID).stats()

    print("\n[2] Buffer checks")
    check("buffer filled", stats["buffered_frames"] > 50,
          f"{stats['buffered_frames']} frames")
    check("buffer holds the configured window, not everything",
          stats["buffered_seconds"] <= stats["window_seconds"] + 1.0,
          f"{stats['buffered_seconds']}s held, window "
          f"{stats['window_seconds']}s, {camera.count()} frames produced")
    check("retention window exceeds pre-roll + post-roll",
          stats["window_seconds"] >= PRE_ROLL_SECONDS + POST_ROLL_SECONDS + 1,
          f"window {stats['window_seconds']}s vs "
          f"{PRE_ROLL_SECONDS}+{POST_ROLL_SECONDS}s needed")
    check("FPS measured from the real frame rate",
          abs(stats["measured_fps"] - SOURCE_FPS) < 3.0,
          f"measured {stats['measured_fps']} vs actual {SOURCE_FPS}")

    # --- EVENT 1 -------------------------------------------------

    print("\n[3] Triggering NOT_SEGREGATED (event 1)")
    frames_before = camera.count()
    t1 = time.time()

    rec1 = evidence_recorder.trigger(
        property_id="PROP-004",
        picker_id="PICKER-01",
        camera_id=CAMERA_ID,
        event_type="NOT_SEGREGATED",
    )

    elapsed = time.time() - t1
    check("trigger returns immediately (capture loop not blocked)",
          elapsed < 0.5, f"returned in {elapsed * 1000:.0f} ms")
    print(f"    -> {rec1['evidence_id']}  {os.path.basename(rec1['video_path'])}")

    rec1 = evidence_recorder.wait_for(rec1["evidence_id"], timeout=40)
    verify_clip("EVENT 1", rec1, camera, t1)

    print("\n[4] Camera still alive after writing?")
    time.sleep(2)
    check("capture thread survived the write",
          camera.count() > frames_before + 20,
          f"{camera.count() - frames_before} more frames produced")
    check("buffer still filling",
          not evidence_recorder.get_buffer(CAMERA_ID).is_stale())

    # --- EVENT 2 -------------------------------------------------

    print("\n[5] Waiting 4 s, then triggering a second event")
    time.sleep(4)

    t2 = time.time()
    rec2 = evidence_recorder.trigger(
        property_id="PROP-007",
        picker_id="PICKER-02",
        camera_id=CAMERA_ID,
        event_type="NOT_SEGREGATED",
    )
    print(f"    -> {rec2['evidence_id']}  {os.path.basename(rec2['video_path'])}")

    rec2 = evidence_recorder.wait_for(rec2["evidence_id"], timeout=40)
    verify_clip("EVENT 2", rec2, camera, t2)

    print("\n[6] Independence checks")
    check("two different evidence ids",
          rec1["evidence_id"] != rec2["evidence_id"])
    check("two different files",
          rec1["video_path"] != rec2["video_path"])
    check("both files still on disk",
          os.path.exists(rec1["video_path"]) and os.path.exists(rec2["video_path"]))
    check("filenames follow evidence_<property>_<picker>_<timestamp>.mp4",
          os.path.basename(rec2["video_path"]).startswith(
              "evidence_PROP-007_PICKER-02_"))

    # --- EMPTY BUFFER --------------------------------------------

    print("\n[7] Double-click guard")
    a = evidence_recorder.trigger(property_id="PROP-009", picker_id="PICKER-01",
                                  camera_id=CAMERA_ID)
    b = evidence_recorder.trigger(property_id="PROP-009", picker_id="PICKER-01",
                                  camera_id=CAMERA_ID)
    c = evidence_recorder.trigger(property_id="PROP-010", picker_id="PICKER-01",
                                  camera_id=CAMERA_ID)
    check("a second click on the SAME house is de-duplicated",
          b["evidence_id"] == a["evidence_id"] and b.get("deduplicated"))
    check("a click on a DIFFERENT house is a separate event",
          c["evidence_id"] != a["evidence_id"])

    print("\n[8] Graceful failure on an unknown camera")
    bad = evidence_recorder.trigger(property_id="PROP-001",
                                    camera_id="no-such-camera")
    check("unknown camera fails cleanly instead of crashing",
          bad["status"] == "FAILED" and bad.get("error"),
          bad.get("error", ""))

    camera.running = False
    camera.join(timeout=3)

    # --- RESULT ---------------------------------------------------

    print("\n" + "=" * 66)
    if failures:
        print(f"FAILED -- {len(failures)} check(s):")
        for f in failures:
            print("   *", f)
        print("=" * 66)
        return 1

    print("ALL CHECKS PASSED")
    print(f"clip 1: {rec1['video_path']}")
    print(f"clip 2: {rec2['video_path']}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
