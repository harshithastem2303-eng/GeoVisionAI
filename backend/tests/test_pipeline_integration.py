"""The capture loop's two side channels, exercised without a camera.

``_build_observations`` -> ``_publish_observations`` -> outbound event is the
path that actually reaches WASTRAQ, so it is tested end to end here with a
recording transport standing in for the network and a plain nested list
standing in for the depth frame. No RealSense, no ultralytics, no HTTP.
"""

import re
import unittest

import config
from evidence import RollingClipBuffer
from integration.client import TransportError, WastraqClient
from integration.events import find_property_fields
from integration.publisher import EventPublisher
from vision.camera import MockSource
from vision.pipeline import VisionPipeline
from vision.track_history import TrackHistory
from vision.types import PersonDetection
from vision.worker_registry import WorkerRegistry

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class Grid(list):
    """Depth frame as nested lists, sliceable like a numpy array."""

    def __init__(self, value_mm=3000, width=640, height=480):
        super().__init__([[value_mm] * width for _ in range(height)])
        self.shape = (height, width)

    def __getitem__(self, key):
        if isinstance(key, tuple):
            rows, cols = key
            return [row[cols] for row in list.__getitem__(self, rows)]
        return list.__getitem__(self, key)


class RecordingTransport:
    def __init__(self, always_fail=False):
        self.sent = []
        self.always_fail = always_fail

    def post_json(self, url, payload):
        if self.always_fail:
            raise TransportError("Connection refused")
        self.sent.append(payload)
        return 202


def build_pipeline(always_fail=False, clip_buffer=None):
    transport = RecordingTransport(always_fail=always_fail)
    client = WastraqClient(
        base_url="http://192.0.2.10:8000", enabled=True, transport=transport
    )
    publisher = EventPublisher(
        client=client,
        queue_max=50,
        retry_backoff_s=0.0,
        max_attempts=2,
        track_publish_hz=5.0,
    )
    pipeline = VisionPipeline(
        settings=config,
        registry=WorkerRegistry(),
        history=TrackHistory(maxlen=50),
        location_service=None,
        publisher=publisher,
        clip_buffer=clip_buffer,
    )
    # Intrinsics and depth scale only; no frames are ever read from it.
    pipeline.source = MockSource()
    return pipeline, publisher, transport


def detections():
    return [
        PersonDetection(track_id=17, bbox=(270, 100, 370, 400), confidence=0.94),
        PersonDetection(track_id=None, bbox=(10, 10, 40, 90), confidence=0.51),
    ]


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.pipeline, _pub, _tr = build_pipeline()
        self.observations = self.pipeline._build_observations(
            detections(), Grid(3540), now=1787900412.341
        )

    def test_every_observation_is_timestamped_and_sourced(self):
        for observation in self.observations:
            self.assertTrue(ISO_Z.match(observation["timestamp"]))
            self.assertEqual(observation["source_id"], config.SOURCE_ID)
            self.assertTrue(observation["session_id"])

    def test_depth_is_attached_per_track(self):
        person = self.observations[0]
        self.assertTrue(person["depth_valid"])
        self.assertAlmostEqual(person["depth_m"], 3.54, places=2)
        self.assertEqual(person["relative_forward_m"], person["camera_z_m"])
        self.assertEqual(person["relative_x_m"], person["camera_x_m"])

    def test_legacy_camera_position_shape_is_preserved(self):
        self.assertEqual(
            set(self.observations[0]["camera_position_m"]), {"x", "y", "z"}
        )

    def test_unidentified_people_are_present_not_filtered(self):
        self.assertEqual(len(self.observations), 2)
        self.assertFalse(self.observations[0]["is_authorized_picker"])

    def test_missing_depth_frame_does_not_stop_the_observation(self):
        observations = self.pipeline._build_observations(
            detections(), None, now=1787900412.341
        )
        self.assertEqual(len(observations), 2)
        self.assertFalse(observations[0]["depth_valid"])
        self.assertEqual(observations[0]["depth_status"], "NO_DEPTH_FRAME")

    def test_tracks_envelope(self):
        self.pipeline._observations = self.observations
        payload = self.pipeline.tracks()
        self.assertTrue(ISO_Z.match(payload["timestamp"]))
        self.assertEqual(payload["source_id"], config.SOURCE_ID)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["authorized_count"], 0)
        self.assertEqual(
            payload["people"][0]["bbox"],
            {"x1": 270, "y1": 100, "x2": 370, "y2": 400},
        )


class PublishingTests(unittest.TestCase):
    def test_tracked_people_are_published_and_untracked_ones_are_not(self):
        pipeline, publisher, transport = build_pipeline()
        observations = pipeline._build_observations(
            detections(), Grid(3540), now=1000.0
        )
        pipeline._publish_observations(observations, now=1000.0)
        publisher.drain_once()

        self.assertEqual(len(transport.sent), 1)
        event = transport.sent[0]
        self.assertEqual(event["event_type"], "TRACK_UPDATE")
        self.assertEqual(event["track_id"], 17)
        self.assertEqual(event["bbox"], {"x1": 270, "y1": 100, "x2": 370, "y2": 400})
        self.assertTrue(event["depth_valid"])

    def test_publishing_is_throttled_not_once_per_frame(self):
        pipeline, publisher, transport = build_pipeline()
        # One second of 30 FPS capture.
        for frame in range(30):
            now = 1000.0 + frame / 30.0
            observations = pipeline._build_observations(
                detections(), Grid(3000), now=now
            )
            pipeline._publish_observations(observations, now=now)
        publisher.drain_once(limit=100)
        self.assertLessEqual(len(transport.sent), 6)
        self.assertGreaterEqual(len(transport.sent), 4)

    def test_no_property_association_reaches_the_wire(self):
        pipeline, publisher, transport = build_pipeline()
        observations = pipeline._build_observations(
            detections(), Grid(3000), now=1000.0
        )
        pipeline._publish_observations(observations, now=1000.0)
        publisher.drain_once()
        self.assertEqual(find_property_fields(transport.sent[0]), [])

    def test_an_unreachable_wastraq_does_not_raise_into_the_loop(self):
        pipeline, publisher, _transport = build_pipeline(always_fail=True)
        observations = pipeline._build_observations(
            detections(), Grid(3000), now=1000.0
        )
        # This is what the capture thread calls; it must return normally.
        pipeline._publish_observations(observations, now=1000.0)
        publisher.drain_once()
        self.assertEqual(pipeline.track_events_published, 1)

    def test_pipeline_works_with_no_publisher_at_all(self):
        pipeline = VisionPipeline(
            settings=config,
            registry=WorkerRegistry(),
            history=TrackHistory(maxlen=10),
        )
        pipeline.source = MockSource()
        observations = pipeline._build_observations(
            detections(), Grid(3000), now=1000.0
        )
        pipeline._publish_observations(observations, now=1000.0)  # no-op
        self.assertEqual(pipeline.track_events_published, 0)


class EvidenceSideChannelTests(unittest.TestCase):
    def test_frames_reach_the_clip_buffer(self):
        import tempfile

        buffer = RollingClipBuffer(
            directory=tempfile.mkdtemp(prefix="gv-pipe-"),
            capture_hz=10.0,
            encoder=lambda frame: b"JPEG",
        )
        pipeline, _pub, _tr = build_pipeline(clip_buffer=buffer)
        pipeline._buffer_evidence_frame("frame", now=1000.0)
        self.assertEqual(buffer.stats()["buffered_frames"], 1)

    def test_a_broken_evidence_buffer_never_breaks_capture(self):
        class Exploding:
            def add_frame(self, *args, **kwargs):
                raise RuntimeError("disk full")

        pipeline, _pub, _tr = build_pipeline(clip_buffer=Exploding())
        pipeline._buffer_evidence_frame("frame", now=1000.0)  # must not raise


if __name__ == "__main__":
    unittest.main()
