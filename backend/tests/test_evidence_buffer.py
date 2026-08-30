"""The rolling evidence buffer and clip extraction.

Phase 9. The encoder and the video writer are injected, so none of this
needs OpenCV, a camera, or a real video codec.
"""

import tempfile
import unittest
from pathlib import Path

from evidence import RollingClipBuffer


def fake_encoder(frame):
    """Frames are just ints in these tests."""

    return f"FRAME-{frame}".encode()


class RecordingWriter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, frames, path, fps):
        self.calls.append((list(frames), Path(path), fps))
        if self.fail:
            return None
        return f"{path}.mp4"


def build(writer=None, **kwargs):
    options = {
        "buffer_s": 20.0,
        "capture_hz": 10.0,
        "pre_s": 10.0,
        "post_s": 3.0,
        "enabled": True,
        "encoder": fake_encoder,
    }
    options.update(kwargs)
    return RollingClipBuffer(
        directory=tempfile.mkdtemp(prefix="gv-evidence-"),
        writer=writer or RecordingWriter(),
        **options,
    )


def fill(buffer, start=1000.0, count=200, step=0.1):
    for index in range(count):
        buffer.add_frame(index, timestamp=start + index * step)


class RingTests(unittest.TestCase):
    def test_frames_are_stored_at_the_capture_rate_not_camera_fps(self):
        buffer = build(capture_hz=10.0)
        # 30 FPS in for one second.
        for index in range(30):
            buffer.add_frame(index, timestamp=1000.0 + index / 30.0)
        # About 10 kept, not 30.
        self.assertGreaterEqual(buffer.frames_stored, 9)
        self.assertLessEqual(buffer.frames_stored, 11)

    def test_ring_is_bounded_by_the_retention_window(self):
        buffer = build(buffer_s=5.0, capture_hz=10.0)
        fill(buffer, count=200, step=0.1)  # 20 seconds of frames
        stats = buffer.stats()
        self.assertLessEqual(stats["buffered_seconds"], 5.01)
        self.assertLessEqual(stats["buffered_frames"], 52)

    def test_a_failing_encoder_never_raises_into_the_capture_loop(self):
        def explode(_frame):
            raise RuntimeError("no codec")

        buffer = build(encoder=explode)
        self.assertFalse(buffer.add_frame(1, timestamp=1000.0))
        self.assertEqual(buffer.frames_skipped, 1)

    def test_disabled_buffer_stores_nothing(self):
        buffer = build(enabled=False)
        self.assertFalse(buffer.add_frame(1, timestamp=1000.0))
        self.assertEqual(buffer.stats()["buffered_frames"], 0)


class ClipTests(unittest.TestCase):
    def test_clip_covers_pre_and_post_around_the_trigger(self):
        writer = RecordingWriter()
        buffer = build(writer=writer, pre_s=10.0, post_s=3.0, buffer_s=30.0)
        fill(buffer, start=1000.0, count=200, step=0.1)  # 1000.0 .. 1019.9

        clip = buffer.request_clip(trigger_time=1015.0, track_id=17, blocking=True)

        self.assertEqual(clip.status, "READY")
        self.assertEqual(clip.start_time, 1005.0)
        self.assertEqual(clip.end_time, 1018.0)
        # 13 seconds at 10 Hz, inclusive of both ends.
        self.assertEqual(clip.frame_count, 131)
        self.assertEqual(len(writer.calls[0][0]), 131)

    def test_clip_metadata_is_carried_through(self):
        buffer = build()
        fill(buffer, count=50)
        clip = buffer.request_clip(
            trigger_time=1002.0,
            track_id=17,
            rfid_event_id="evt-1",
            session_id="sess-1",
            blocking=True,
        )
        payload = clip.to_dict()
        self.assertEqual(payload["track_id"], 17)
        self.assertEqual(payload["rfid_event_id"], "evt-1")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertTrue(payload["clip_id"].startswith("CLIP-"))

    def test_empty_window_reports_rather_than_writing_a_stub(self):
        writer = RecordingWriter()
        buffer = build(writer=writer)
        clip = buffer.request_clip(trigger_time=5000.0, blocking=True)
        self.assertEqual(clip.status, "EMPTY")
        self.assertIsNone(clip.file_path)
        self.assertEqual(writer.calls, [])

    def test_writer_failure_is_reported_not_swallowed(self):
        buffer = build(writer=RecordingWriter(fail=True))
        fill(buffer, count=50)
        clip = buffer.request_clip(trigger_time=1002.0, blocking=True)
        self.assertEqual(clip.status, "FAILED")
        self.assertIsNotNone(clip.error)
        self.assertEqual(buffer.stats()["clips_failed"], 1)

    def test_disabled_buffer_declines_clearly(self):
        buffer = build(enabled=False)
        clip = buffer.request_clip(trigger_time=1000.0, blocking=True)
        self.assertEqual(clip.status, "DISABLED")

    def test_on_ready_callback_receives_the_finished_clip(self):
        seen = []
        buffer = build()
        fill(buffer, count=50)
        buffer.request_clip(
            trigger_time=1002.0, on_ready=seen.append, blocking=True
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].status, "READY")

    def test_a_raising_callback_does_not_break_the_clip(self):
        def explode(_clip):
            raise RuntimeError("downstream is down")

        buffer = build()
        fill(buffer, count=50)
        clip = buffer.request_clip(
            trigger_time=1002.0, on_ready=explode, blocking=True
        )
        self.assertEqual(clip.status, "READY")

    def test_clips_are_listed_for_the_status_endpoint(self):
        buffer = build()
        fill(buffer, count=50)
        clip = buffer.request_clip(trigger_time=1002.0, blocking=True)
        self.assertEqual(len(buffer.clips()), 1)
        self.assertIsNotNone(buffer.find(clip.clip_id))
        self.assertIsNone(buffer.find("CLIP-nope"))


if __name__ == "__main__":
    unittest.main()
