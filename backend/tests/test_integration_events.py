"""The GeoVision -> WASTRAQ event vocabulary.

Covers the envelope every event shares, the exact shape of each of the six
event types, and the guard that keeps property associations out of all of
them. Stdlib only: no FastAPI, no camera, no network.
"""

import json
import re
import unittest

from integration import events


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def observation(**overrides):
    """A representative pipeline observation with valid depth."""

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


class TimestampFormatTests(unittest.TestCase):
    """Phase 2: absolute, timezone-safe, millisecond ISO-8601 with a Z."""

    def test_iso_utc_shape(self):
        self.assertTrue(ISO_Z.match(events.iso_utc(1787900412.3415)))

    def test_iso_utc_is_utc_not_local(self):
        # Epoch 0 is 1970-01-01T00:00:00Z regardless of the host timezone.
        self.assertEqual(events.iso_utc(0), "1970-01-01T00:00:00.000Z")

    def test_iso_utc_defaults_to_now(self):
        self.assertTrue(ISO_Z.match(events.iso_utc()))

    def test_event_ids_are_unique(self):
        ids = {events.new_event_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)


class TrackUpdateEventTests(unittest.TestCase):
    """Phase 3 / Phase 6: the TRACK_UPDATE contract."""

    def setUp(self):
        self.event = events.track_update_event(
            source_id="GEOVISION-D455-01",
            observation=observation(),
            timestamp=1787900412.341,
        )

    def test_envelope(self):
        self.assertEqual(self.event["event_type"], "TRACK_UPDATE")
        self.assertEqual(self.event["source_id"], "GEOVISION-D455-01")
        self.assertTrue(ISO_Z.match(self.event["timestamp"]))
        self.assertTrue(self.event["event_id"])

    def test_bbox_is_an_object(self):
        self.assertEqual(
            self.event["bbox"], {"x1": 220, "y1": 90, "x2": 390, "y2": 470}
        )

    def test_depth_and_relative_fields(self):
        self.assertEqual(self.event["depth_m"], 3.54)
        self.assertEqual(self.event["camera_x_m"], -0.82)
        self.assertEqual(self.event["camera_y_m"], 0.14)
        self.assertEqual(self.event["camera_z_m"], 3.44)
        # The WASTRAQ-convenience aliases must mirror x and z exactly.
        self.assertEqual(self.event["relative_x_m"], self.event["camera_x_m"])
        self.assertEqual(self.event["relative_forward_m"], self.event["camera_z_m"])
        self.assertTrue(self.event["depth_valid"])

    def test_identity_is_carried_but_never_invented(self):
        self.assertTrue(self.event["is_authorized_picker"])
        self.assertEqual(self.event["collector_id"], "GC-001")

        anonymous = events.track_update_event(
            source_id="GEOVISION-D455-01",
            observation=observation(
                is_authorized_picker=False,
                collector_id=None,
                identity_confidence=None,
            ),
        )
        self.assertFalse(anonymous["is_authorized_picker"])
        self.assertIsNone(anonymous["collector_id"])

    def test_missing_depth_serialises_as_null_not_zero(self):
        event = events.track_update_event(
            source_id="GEOVISION-D455-01",
            observation=observation(
                depth_m=None,
                camera_x_m=None,
                camera_y_m=None,
                camera_z_m=None,
                relative_x_m=None,
                relative_forward_m=None,
                depth_valid=False,
                depth_status="NO_VALID_SAMPLES",
                camera_position_m=None,
            ),
        )
        self.assertIsNone(event["depth_m"])
        self.assertIsNone(event["relative_forward_m"])
        self.assertFalse(event["depth_valid"])
        self.assertEqual(event["depth_status"], "NO_VALID_SAMPLES")

    def test_gps_is_a_separate_object_and_absent_without_a_fix(self):
        self.assertNotIn("gps", self.event)

        with_gps = events.track_update_event(
            source_id="GEOVISION-D455-01",
            observation=observation(),
            gps={
                "latitude": 12.29,
                "longitude": 76.64,
                "accuracy_m": 8.0,
                "source": "PHONE",
                "timestamp": "2026-08-28T07:10:11.000Z",
                "age_s": 1.3,
                "stale": False,
            },
        )
        self.assertEqual(with_gps["gps"]["latitude"], 12.29)
        # Kept nested: a camera observation and a phone fix are different
        # measurements and must not read as one fused position.
        self.assertNotIn("latitude", with_gps)
        # No GNSS receiver exists, so these are explicitly null.
        for key in ("altitude_m", "speed_mps", "hdop", "satellites", "heading_deg"):
            self.assertIsNone(with_gps["gps"][key])

    def test_json_serialisable(self):
        self.assertIsInstance(json.dumps(self.event), str)


class RFIDEventTests(unittest.TestCase):
    """Phase 5: normalised RFID events, including the unresolved ones."""

    def test_bound_tap(self):
        event = events.rfid_tap_event(
            source_id="GEOVISION-RFID-01",
            rfid_uid="04A1B2C3",
            collector_id="PICKER-01",
            track_id=17,
            status="BOUND",
            confidence=0.91,
            candidate_track_ids=[17],
            session_id="sess123456ab",
        )
        self.assertEqual(event["event_type"], "RFID_TAP")
        self.assertEqual(event["source_id"], "GEOVISION-RFID-01")
        self.assertEqual(event["rfid_uid"], "04A1B2C3")
        self.assertEqual(event["collector_id"], "PICKER-01")
        self.assertEqual(event["track_id"], 17)
        self.assertEqual(event["binding_status"], "BOUND")

    def test_ambiguous_tap_carries_null_track_and_all_candidates(self):
        event = events.rfid_tap_event(
            source_id="GEOVISION-RFID-01",
            rfid_uid="04A1B2C3",
            collector_id="PICKER-01",
            track_id=None,
            status="AMBIGUOUS",
            candidate_track_ids=[17, 18],
            reason="two people in the reader zone",
        )
        # Never guess. Ambiguity travels to WASTRAQ intact.
        self.assertIsNone(event["track_id"])
        self.assertEqual(event["candidate_track_ids"], [17, 18])
        self.assertEqual(event["binding_status"], "AMBIGUOUS")
        self.assertIn("reason", event)

    def test_unknown_tag_has_no_collector(self):
        event = events.rfid_tap_event(
            source_id="GEOVISION-RFID-01",
            rfid_uid="DEADBEEF",
            collector_id=None,
            track_id=None,
            status="UNKNOWN_RFID",
        )
        self.assertIsNone(event["collector_id"])
        self.assertIsNone(event["track_id"])

    def test_worker_track_bound(self):
        event = events.worker_track_bound_event(
            source_id="GEOVISION-D455-01",
            collector_id="PICKER-01",
            rfid_uid="04A1B2C3",
            track_id=17,
            confidence=0.91,
            session_id="sess123456ab",
            rfid_event_id="abc-123",
        )
        self.assertEqual(event["event_type"], "WORKER_TRACK_BOUND")
        self.assertEqual(event["track_id"], 17)
        self.assertEqual(event["rfid_event_id"], "abc-123")


class EvidenceAndHeartbeatTests(unittest.TestCase):
    def test_evidence_ready_carries_a_reference_not_bytes(self):
        event = events.evidence_ready_event(
            source_id="GEOVISION-D455-01",
            clip_id="CLIP-abc123",
            file_path="/x/evidence_clips/CLIP-abc123.mp4",
            start_time=1787900402.0,
            end_time=1787900415.0,
            track_id=17,
            rfid_event_id="abc-123",
            frame_count=130,
        )
        self.assertEqual(event["event_type"], "EVIDENCE_READY")
        self.assertTrue(ISO_Z.match(event["start_time"]))
        self.assertTrue(ISO_Z.match(event["end_time"]))
        self.assertEqual(event["file_path"], "/x/evidence_clips/CLIP-abc123.mp4")
        # A reference only. No frame payload of any kind.
        serialised = json.dumps(event)
        self.assertNotIn("base64", serialised)
        self.assertLess(len(serialised), 1024)

    def test_heartbeat(self):
        event = events.heartbeat_event(
            "GEOVISION-D455-01", status={"camera_running": True}
        )
        self.assertEqual(event["event_type"], "HEARTBEAT")
        self.assertTrue(event["status"]["camera_running"])


class NoPropertyAssociationTests(unittest.TestCase):
    """ABSOLUTE RULE 2: GeoVision never names the serviced property."""

    def test_builders_emit_no_property_fields(self):
        built = [
            events.track_update_event("S", observation()),
            events.rfid_tap_event("S", "UID", "GC-1", 17, "BOUND"),
            events.worker_track_bound_event("S", "GC-1", "UID", 17, 0.9, "sess"),
            events.evidence_ready_event("S", "CLIP-1", "/tmp/x.mp4", 1.0, 2.0),
            events.heartbeat_event("S"),
        ]
        for event in built:
            with self.subTest(event=event["event_type"]):
                self.assertEqual(events.find_property_fields(event), [])

    def test_property_fields_leaking_through_an_observation_are_stripped(self):
        leaky = observation()
        leaky["property_id"] = "PROP-003"
        event = events.track_update_event("S", leaky)
        self.assertNotIn("property_id", event)
        self.assertEqual(events.find_property_fields(event), [])

    def test_guard_finds_nested_and_listed_property_fields(self):
        payload = {
            "a": {"property_id": "PROP-001"},
            "b": [{"ok": 1}, {"service_zone_id": "SZ-2"}],
        }
        found = events.find_property_fields(payload)
        self.assertIn("a.property_id", found)
        self.assertIn("b[1].service_zone_id", found)

    def test_guard_strips_recursively(self):
        cleaned = events.reject_property_fields(
            {
                "keep": 1,
                "nested": {"property_id": "PROP-001", "keep": 2},
                "listed": [{"segregation_status": "NOT_SEGREGATED", "keep": 3}],
            }
        )
        self.assertEqual(
            cleaned, {"keep": 1, "nested": {"keep": 2}, "listed": [{"keep": 3}]}
        )

    def test_heartbeat_status_is_also_guarded(self):
        event = events.heartbeat_event("S", status={"property_id": "PROP-9", "up": 1})
        self.assertEqual(event["status"], {"up": 1})


if __name__ == "__main__":
    unittest.main()


from integration.events import find_property_fields  # noqa: E402


class NonSegregationTriggerTests(unittest.TestCase):
    """The trigger signals *that* waste was flagged, never *which house*."""

    def build(self, **overrides):
        from integration.events import non_segregation_trigger_event

        kwargs = dict(
            source_id="GEOVISION-RFID-01",
            trigger_id="TRG-1",
            episode_id="EP-HOUSE-2",
            collector_id="PICKER-01",
            rfid_uid="04A1B24C",
            track_id=35,
            status="NON_SEGREGATION",
            timestamp=1756450000.0,
            session_id="sess-1",
            rfid_event_id="EVT-1",
        )
        kwargs.update(overrides)
        return non_segregation_trigger_event(**kwargs)

    def test_shape(self):
        from integration.events import EVENT_NON_SEGREGATION_TRIGGER

        event = self.build()
        self.assertEqual(event["event_type"], EVENT_NON_SEGREGATION_TRIGGER)
        self.assertEqual(event["trigger_id"], "TRG-1")
        self.assertEqual(event["episode_id"], "EP-HOUSE-2")
        self.assertEqual(event["track_id"], 35)
        self.assertFalse(event["duplicate"])
        self.assertTrue(event["timestamp"].endswith("Z"))

    def test_it_cannot_assert_a_segregation_status_or_a_property(self):
        # segregation_status is a PROPERTY_FIELD precisely so this builder
        # cannot set one. WASTRAQ owns the verdict; this is only the signal.
        event = self.build()
        self.assertNotIn("segregation_status", event)
        self.assertNotIn("property_id", event)
        self.assertEqual(find_property_fields(event), [])

    def test_a_planted_property_field_never_reaches_the_wire(self):
        event = self.build(episode_id="EP-1")
        event_with_leak = dict(event)
        event_with_leak["property_id"] = "PROP-004"
        from integration.events import reject_property_fields

        self.assertEqual(find_property_fields(reject_property_fields(event_with_leak)), [])

    def test_unresolved_outcomes_still_build(self):
        event = self.build(
            status="NO_ACTIVE_EPISODE",
            episode_id=None,
            reason="No open collection episode for that track.",
        )
        self.assertEqual(event["trigger_status"], "NO_ACTIVE_EPISODE")
        self.assertIsNone(event["episode_id"])
        self.assertIn("No open collection episode", event["reason"])

    def test_duplicate_carries_the_same_trigger_id(self):
        first = self.build()
        repeat = self.build(status="DUPLICATE_TRIGGER", duplicate=True)
        self.assertEqual(first["trigger_id"], repeat["trigger_id"])
        self.assertTrue(repeat["duplicate"])
        # The envelope id still differs: two deliveries, one trigger.
        self.assertNotEqual(first["event_id"], repeat["event_id"])
