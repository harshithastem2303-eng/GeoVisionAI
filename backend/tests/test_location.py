"""Location validation, storage, provider preference and the mock provider."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from location import (  # noqa: E402
    InvalidLocation,
    LocationService,
    MockLocationProvider,
    validate_fix,
)
from location.base import parse_timestamp  # noqa: E402


class ValidationTests(unittest.TestCase):
    def test_10_valid_phone_location_is_accepted(self):
        fix = validate_fix(
            latitude=12.9716,
            longitude=77.5946,
            accuracy_m=8.2,
            source="PHONE",
            timestamp="2026-08-26T14:52:03.421+05:30",
        )
        self.assertAlmostEqual(fix.latitude, 12.9716)
        self.assertAlmostEqual(fix.longitude, 77.5946)
        self.assertEqual(fix.accuracy_m, 8.2)
        self.assertEqual(fix.source, "PHONE")

    def test_9_out_of_range_latitude_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude=95.0, longitude=77.0)

    def test_9_out_of_range_longitude_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude=12.0, longitude=200.0)

    def test_9_non_numeric_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude="somewhere", longitude=77.0)

    def test_9_nan_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude=float("nan"), longitude=77.0)

    def test_9_negative_accuracy_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude=12.0, longitude=77.0, accuracy_m=-5)

    def test_9_accuracy_worse_than_the_limit_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(
                latitude=12.0,
                longitude=77.0,
                accuracy_m=500,
                max_accuracy_m=100,
            )

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(InvalidLocation):
            validate_fix(latitude=12.0, longitude=77.0, source="SATELLITE")

    def test_no_heading_is_ever_reported(self):
        fix = validate_fix(latitude=12.0, longitude=77.0, accuracy_m=5)
        self.assertIsNone(fix.to_dict()["heading_deg"])


class TimestampTests(unittest.TestCase):
    def test_epoch_milliseconds_are_converted(self):
        self.assertAlmostEqual(parse_timestamp(1787738900000), 1787738900.0)

    def test_epoch_seconds_pass_through(self):
        self.assertAlmostEqual(parse_timestamp(1787738900.0), 1787738900.0)

    def test_iso_with_offset_is_parsed(self):
        self.assertIsInstance(parse_timestamp("2026-08-26T14:52:03.421+05:30"), float)

    def test_missing_timestamp_defaults_to_now(self):
        self.assertAlmostEqual(parse_timestamp(None), time.time(), delta=2)

    def test_garbage_timestamp_raises(self):
        with self.assertRaises(InvalidLocation):
            parse_timestamp("not-a-time")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = LocationService(
            preferred="phone",
            max_age_s=30.0,
            max_accuracy_m=100.0,
        )

    def test_10_valid_phone_location_is_stored_and_read_back(self):
        self.service.submit(
            latitude=12.9716,
            longitude=77.5946,
            accuracy_m=8.2,
            source="PHONE",
        )
        fix = self.service.current_fix()

        self.assertIsNotNone(fix)
        self.assertEqual(fix.source, "PHONE")
        self.assertAlmostEqual(fix.latitude, 12.9716)
        self.assertEqual(self.service.phone.updates, 1)

    def test_9_invalid_location_is_rejected_and_not_stored(self):
        with self.assertRaises(InvalidLocation):
            self.service.submit(latitude=999, longitude=77.0, source="PHONE")

        self.assertIsNone(self.service.phone.get_fix())
        self.assertEqual(self.service.phone.updates, 0)

    def test_phone_is_preferred_over_laptop(self):
        self.service.submit(1.0, 1.0, 30.0, source="LAPTOP")
        self.service.submit(2.0, 2.0, 8.0, source="PHONE")

        fix = self.service.current_fix()
        self.assertEqual(fix.source, "PHONE")

    def test_laptop_is_used_when_no_phone_fix_exists(self):
        self.service.submit(1.0, 1.0, 30.0, source="LAPTOP")
        self.assertEqual(self.service.current_fix().source, "LAPTOP")

    def test_mock_fills_in_when_nothing_has_been_pushed(self):
        # Falls through phone -> laptop -> mock rather than returning nothing.
        self.assertEqual(self.service.current_fix().source, "MOCK")

    def test_mock_cannot_be_submitted_over_http(self):
        with self.assertRaises(InvalidLocation):
            self.service.submit(1.0, 1.0, source="MOCK")

    def test_stale_fix_is_flagged_rather_than_hidden(self):
        self.service.submit(
            latitude=12.0,
            longitude=77.0,
            accuracy_m=5.0,
            source="PHONE",
            timestamp=time.time() - 120,
        )
        payload = self.service.phone.get_fix().to_dict(self.service.max_age_s)
        self.assertTrue(payload["stale"])

    def test_describe_states_no_imu_and_no_gnss(self):
        described = self.service.describe()
        self.assertFalse(described["heading_available"])
        self.assertFalse(described["imu_available"])
        self.assertIsNone(described["gnss_receiver"])


class MockProviderTests(unittest.TestCase):
    def test_11_mock_provider_returns_a_deterministic_fix(self):
        provider = MockLocationProvider(latitude=12.5, longitude=76.5)
        first = provider.get_fix(now=1000.0)

        self.assertEqual(first.source, "MOCK")
        self.assertAlmostEqual(first.latitude, 12.5)
        self.assertAlmostEqual(first.longitude, 76.5)

        # Same index, same coordinates -- repeatable.
        again = provider.get_fix(now=2000.0)
        self.assertEqual((again.latitude, again.longitude),
                         (first.latitude, first.longitude))

    def test_11_mock_provider_advances_along_a_track(self):
        provider = MockLocationProvider(latitude=12.5, longitude=76.5)
        start = provider.get_fix(now=1000.0)
        provider.advance()
        moved = provider.get_fix(now=1001.0)

        self.assertNotEqual(start.latitude, moved.latitude)

        provider.reset()
        self.assertAlmostEqual(provider.get_fix(now=1002.0).latitude, start.latitude)

    def test_11_explicit_track_is_followed_in_order(self):
        provider = MockLocationProvider(track=[(1.0, 2.0), (3.0, 4.0)])
        self.assertEqual(provider.get_fix(now=0).latitude, 1.0)
        provider.advance()
        self.assertEqual(provider.get_fix(now=0).latitude, 3.0)
        provider.advance()  # wraps
        self.assertEqual(provider.get_fix(now=0).latitude, 1.0)


if __name__ == "__main__":
    unittest.main()
