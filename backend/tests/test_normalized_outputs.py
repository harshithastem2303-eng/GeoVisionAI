"""The two normalised payloads GeoVision exposes: tracks and GPS.

Phase 3 (track output) and Phase 4 (GPS output). Neither test needs a camera,
a browser or FastAPI -- the shaping lives in pure functions so it can be
pinned down exactly.
"""

import time
import unittest

from location.base import LocationFix
from location.normalized import normalized_gps
from vision.pipeline import VisionPipeline


def observation(**overrides):
    base = {
        "session_id": "sess123456ab",
        "source_id": "GEOVISION-D455-01",
        "timestamp": "2026-08-28T07:10:12.341Z",
        "timestamp_epoch": 1787900412.341,
        "track_id": 17,
        "bbox": [220, 90, 390, 470],
        "detection_confidence": 0.94,
        "is_authorized_picker": True,
        "collector_id": "GC-001",
        "rfid_id": "RFID-01",
        "identity_confidence": 0.88,
        "camera_position_m": {"x": -0.82, "y": 0.14, "z": 3.44},
        "location": None,
        "depth_m": 3.54,
        "camera_x_m": -0.82,
        "camera_y_m": 0.14,
        "camera_z_m": 3.44,
        "relative_x_m": -0.82,
        "relative_forward_m": 3.44,
        "depth_valid": True,
        "depth_status": "OK",
    }
    base.update(overrides)
    return base


class TrackPayloadSchemaTests(unittest.TestCase):
    """Phase 12 item 1: the /people and /tracks entry schema."""

    def setUp(self):
        self.person = VisionPipeline.normalize(observation())

    def test_required_fields_present(self):
        for key in (
            "track_id",
            "confidence",
            "bbox",
            "depth_m",
            "camera_x_m",
            "camera_y_m",
            "camera_z_m",
            "relative_x_m",
            "relative_forward_m",
            "depth_valid",
        ):
            self.assertIn(key, self.person)

    def test_bbox_is_an_object_with_four_named_corners(self):
        self.assertEqual(
            self.person["bbox"], {"x1": 220, "y1": 90, "x2": 390, "y2": 470}
        )

    def test_backward_compatible_aliases_are_retained(self):
        # The pre-upgrade frontend read id / confidence / bbox-as-a-list.
        self.assertEqual(self.person["id"], self.person["track_id"])
        self.assertEqual(
            self.person["confidence"], self.person["detection_confidence"]
        )
        self.assertEqual(self.person["bbox_xyxy"], [220, 90, 390, 470])
        # The current dashboard reads camera_position_m.{x,y,z}.
        self.assertEqual(self.person["camera_position_m"]["z"], 3.44)

    def test_no_property_association_in_a_track_entry(self):
        from integration.events import find_property_fields

        self.assertEqual(find_property_fields(self.person), [])

    def test_unidentified_person_is_reported_not_hidden(self):
        pedestrian = VisionPipeline.normalize(
            observation(
                is_authorized_picker=False,
                collector_id=None,
                identity_confidence=None,
            )
        )
        self.assertFalse(pedestrian["is_authorized_picker"])
        self.assertIsNone(pedestrian["collector_id"])

    def test_depth_absent_keeps_the_same_keys(self):
        without_depth = VisionPipeline.normalize(
            observation(
                depth_m=None,
                camera_x_m=None,
                camera_y_m=None,
                camera_z_m=None,
                relative_x_m=None,
                relative_forward_m=None,
                depth_valid=False,
                depth_status="NO_DEPTH_FRAME",
                camera_position_m=None,
            )
        )
        self.assertEqual(set(without_depth), set(self.person))
        self.assertFalse(without_depth["depth_valid"])
        self.assertIsNone(without_depth["depth_m"])


class GPSNormalizationTests(unittest.TestCase):
    """Phase 12 item 9: honest GPS, with the unmeasured fields explicit."""

    def fix(self, age_s=0.0, accuracy_m=8.0):
        return LocationFix(
            latitude=12.294209,
            longitude=76.641702,
            accuracy_m=accuracy_m,
            source="PHONE",
            timestamp=time.time() - age_s,
        )

    def test_fresh_fix_is_valid(self):
        payload = normalized_gps(self.fix(), "GEOVISION-GPS-01", max_age_s=30.0)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["source_id"], "GEOVISION-GPS-01")
        self.assertAlmostEqual(payload["latitude"], 12.294209)
        self.assertAlmostEqual(payload["longitude"], 76.641702)
        self.assertEqual(payload["accuracy_m"], 8.0)
        self.assertFalse(payload["stale"])

    def test_unmeasured_fields_are_present_and_null(self):
        payload = normalized_gps(self.fix(), "GEOVISION-GPS-01", max_age_s=30.0)
        for key in ("altitude_m", "speed_mps", "hdop", "satellites", "heading_deg"):
            self.assertIn(key, payload)
            self.assertIsNone(payload[key])
        self.assertFalse(payload["imu_available"])
        self.assertFalse(payload["gnss_receiver"])

    def test_stale_fix_is_returned_but_not_valid(self):
        payload = normalized_gps(self.fix(age_s=120.0), "GPS", max_age_s=30.0)
        self.assertTrue(payload["stale"])
        self.assertFalse(payload["valid"])
        # Still reported: "two minutes old" is information.
        self.assertIsNotNone(payload["latitude"])
        self.assertGreater(payload["age_s"], 100)

    def test_no_fix_at_all(self):
        payload = normalized_gps(None, "GPS", max_age_s=30.0)
        self.assertFalse(payload["valid"])
        self.assertIsNone(payload["latitude"])
        self.assertIsNone(payload["longitude"])
        self.assertIsNone(payload["provider"])
        self.assertTrue(payload["timestamp"].endswith("Z"))

    def test_gps_payload_never_names_a_property(self):
        from integration.events import find_property_fields

        self.assertEqual(
            find_property_fields(normalized_gps(self.fix(), "GPS", 30.0)), []
        )


if __name__ == "__main__":
    unittest.main()
