"""End-to-end observation shape, and importability with no hardware attached.

Everything here runs with no RealSense plugged in, no RFID reader, no
database and no YOLO weights on disk. If any of these fail on a developer
machine, the "runs dry" property has been lost.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from vision.pipeline import VisionPipeline  # noqa: E402
from vision.track_history import TrackHistory  # noqa: E402
from vision.types import PersonDetection  # noqa: E402
from vision.worker_registry import WorkerRegistry  # noqa: E402


class ImportTests(unittest.TestCase):
    """Test 13: the application imports without RealSense hardware."""

    def test_pyrealsense2_is_not_installed_or_not_needed_at_import(self):
        # The point is that vision.camera imports either way.
        module = importlib.import_module("vision.camera")
        self.assertTrue(hasattr(module, "RealSenseSource"))
        self.assertTrue(hasattr(module, "MockSource"))

    def test_every_logic_module_imports_with_no_vision_stack(self):
        for name in (
            "config",
            "vision",
            "vision.types",
            "vision.camera",
            "vision.detector",
            "vision.depth_position",
            "vision.rfid_binding",
            "vision.worker_registry",
            "vision.track_history",
            "vision.pipeline",
            "location",
            "location.base",
            "location.mock",
            "location.pushed",
            "location.service",
        ):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_database_module_imports_without_a_database(self):
        # It used to connect at import time, which made this impossible.
        database = importlib.import_module("database")
        self.assertTrue(hasattr(database, "get_db_connection"))

    def test_removed_gps_module_explains_itself(self):
        gps = importlib.import_module("gps")
        with self.assertRaises(ImportError) as caught:
            gps.gps_manager  # noqa: B018 - deliberate attribute access
        self.assertIn("location", str(caught.exception))

    def test_fastapi_app_imports_when_fastapi_is_available(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi not installed in this environment")

        app_module = importlib.import_module("app")
        routes = {getattr(r, "path", None) for r in app_module.app.routes}
        for path in (
            "/people",
            "/rfid/events",
            "/worker-bindings",
            "/location",
            "/observations",
            "/health",
        ):
            self.assertIn(path, routes)


class _FakeSource:
    """A frame source that yields nothing; detections are injected directly."""

    backend = "fake"
    is_open = True
    intrinsics = None
    depth_scale = None

    def read(self):
        return None, None

    def describe(self):
        return {"backend": self.backend, "open": True}


class ObservationTests(unittest.TestCase):
    """Test 6, restated at the observation layer: pedestrians stay visible."""

    def setUp(self):
        self.registry = WorkerRegistry(grace_s=20.0)
        self.history = TrackHistory()
        self.pipeline = VisionPipeline(
            settings=config,
            registry=self.registry,
            history=self.history,
            location_service=None,
        )
        self.pipeline.source = _FakeSource()

    def test_authorised_and_unauthorised_people_both_appear(self):
        self.registry.bind(
            collector_id="COLLECTOR-002",
            rfid_id="AB12CD34",
            track_id=14,
            confidence=0.96,
            event_timestamp=1000.0,
            now=1000.0,
        )

        detections = [
            PersonDetection(track_id=12, bbox=(10, 10, 60, 200), confidence=0.88),
            PersonDetection(track_id=14, bbox=(420, 150, 540, 460), confidence=0.91),
        ]
        observations = self.pipeline._build_observations(detections, None, 1000.0)

        by_track = {o["track_id"]: o for o in observations}
        self.assertEqual(len(observations), 2, "pedestrians must not be hidden")

        pedestrian = by_track[12]
        self.assertFalse(pedestrian["is_authorized_picker"])
        self.assertIsNone(pedestrian["collector_id"])

        picker = by_track[14]
        self.assertTrue(picker["is_authorized_picker"])
        self.assertEqual(picker["collector_id"], "COLLECTOR-002")
        self.assertEqual(picker["rfid_id"], "AB12CD34")
        self.assertEqual(picker["identity_confidence"], 0.96)

    def test_observation_carries_the_session_id(self):
        detections = [
            PersonDetection(track_id=12, bbox=(10, 10, 60, 200), confidence=0.88)
        ]
        observation = self.pipeline._build_observations(detections, None, 1000.0)[0]
        self.assertEqual(observation["session_id"], self.registry.session_id)

    def test_camera_position_is_none_without_depth(self):
        detections = [
            PersonDetection(track_id=12, bbox=(10, 10, 60, 200), confidence=0.88)
        ]
        observation = self.pipeline._build_observations(detections, None, 1000.0)[0]
        self.assertIsNone(observation["camera_position_m"])

    def test_people_view_exposes_the_authorisation_flag(self):
        self.registry.bind(
            collector_id="COLLECTOR-001",
            rfid_id="EF56GH78",
            track_id=11,
            confidence=0.9,
            event_timestamp=1000.0,
            now=1000.0,
        )
        self.pipeline._observations = self.pipeline._build_observations(
            [
                PersonDetection(track_id=11, bbox=(0, 0, 50, 100), confidence=0.9),
                PersonDetection(track_id=99, bbox=(0, 0, 50, 100), confidence=0.9),
            ],
            None,
            1000.0,
        )
        people = self.pipeline.people()
        flags = {p["track_id"]: p["is_authorized_picker"] for p in people}
        self.assertEqual(flags, {11: True, 99: False})


class DiscoveryTests(unittest.TestCase):
    """discover_device() against a fake SDK -- no camera required."""

    def _fake_rs(self, devices):
        rs = types.SimpleNamespace()

        class _Info:
            name = "name"
            serial_number = "serial_number"
            firmware_version = "firmware_version"
            usb_type_descriptor = "usb_type_descriptor"

        class _Device:
            def __init__(self, values):
                self._values = values

            def get_info(self, key):
                return self._values[key]

        class _Context:
            def query_devices(self):
                return [_Device(d) for d in devices]

        rs.camera_info = _Info()
        rs.context = _Context
        return rs

    def _device(self, serial):
        return {
            "name": "Intel RealSense D455",
            "serial_number": serial,
            "firmware_version": "5.16.0.1",
            "usb_type_descriptor": "3.2",
        }

    def test_single_device_is_bound(self):
        from vision.camera import discover_device

        rs = self._fake_rs([self._device("ABC123")])
        self.assertEqual(discover_device(rs, "")["serial"], "ABC123")

    def test_configured_serial_is_matched(self):
        from vision.camera import discover_device

        rs = self._fake_rs([self._device("AAA"), self._device("BBB")])
        self.assertEqual(discover_device(rs, "BBB")["serial"], "BBB")

    def test_no_device_raises(self):
        from vision.camera import CameraUnavailable, discover_device

        with self.assertRaises(CameraUnavailable):
            discover_device(self._fake_rs([]), "")

    def test_wrong_serial_raises_rather_than_substituting(self):
        from vision.camera import CameraUnavailable, discover_device

        rs = self._fake_rs([self._device("AAA")])
        with self.assertRaises(CameraUnavailable):
            discover_device(rs, "ZZZ")

    def test_two_devices_with_no_serial_refuses_to_guess(self):
        from vision.camera import CameraUnavailable, discover_device

        rs = self._fake_rs([self._device("AAA"), self._device("BBB")])
        with self.assertRaises(CameraUnavailable):
            discover_device(rs, "")


if __name__ == "__main__":
    unittest.main()
