"""RFID tap -> camera track attribution.

The central rule under test: nobody is bound on weak or contested evidence.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.rfid_binding import (  # noqa: E402
    RFIDBindingService,
    RFIDEvidenceZone,
    resolve_tap,
)
from vision.track_history import TrackHistory  # noqa: E402
from vision.types import BindingStatus, PersonDetection  # noqa: E402
from vision.worker_registry import WorkerRegistry  # noqa: E402

ZONE = RFIDEvidenceZone(x1=200, y1=200, x2=400, y2=400)
MIN_OVERLAP = 0.15
MARGIN = 0.20


def person(track_id, bbox, confidence=0.9):
    return PersonDetection(track_id=track_id, bbox=bbox, confidence=confidence)


def history_with(detections, timestamp=1000.0, frames=3):
    history = TrackHistory()
    for i in range(frames):
        history.add(timestamp + i * 0.1, detections)
    return history


class EvidenceZoneTests(unittest.TestCase):
    def test_overlap_is_fraction_of_person_box(self):
        # Box entirely inside the zone.
        self.assertAlmostEqual(ZONE.overlap_ratio((250, 250, 350, 350)), 1.0)
        # Box entirely outside.
        self.assertAlmostEqual(ZONE.overlap_ratio((0, 0, 50, 50)), 0.0)
        # Exactly half inside horizontally.
        self.assertAlmostEqual(ZONE.overlap_ratio((300, 250, 500, 350)), 0.5)

    def test_degenerate_box_does_not_divide_by_zero(self):
        self.assertEqual(ZONE.overlap_ratio((300, 300, 300, 300)), 0.0)

    def test_zone_normalises_reversed_corners(self):
        zone = RFIDEvidenceZone.from_tuple((400, 400, 200, 200))
        self.assertEqual((zone.x1, zone.y1, zone.x2, zone.y2), (200, 200, 400, 400))


class ResolveTapTests(unittest.TestCase):
    def test_1_single_person_in_zone_binds(self):
        snapshots = history_with([person(11, (250, 250, 350, 390))]).snapshots()
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 11)
        self.assertGreater(result.confidence, 0.8)

    def test_2_nobody_in_zone_is_unresolved(self):
        # Two pedestrians, both far from the reader.
        snapshots = history_with(
            [person(12, (0, 0, 80, 200)), person(13, (500, 0, 600, 200))]
        ).snapshots()
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN)

        self.assertEqual(result.status, BindingStatus.NO_TRACK_IN_READER_ZONE)
        self.assertIsNone(result.track_id)
        self.assertEqual(result.candidate_track_ids, [])

    def test_3_two_people_in_zone_is_ambiguous_and_binds_nobody(self):
        snapshots = history_with(
            [
                person(14, (210, 250, 300, 390)),
                person(18, (300, 250, 390, 390)),
            ]
        ).snapshots()
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN)

        self.assertEqual(result.status, BindingStatus.AMBIGUOUS)
        self.assertIsNone(result.track_id)
        self.assertCountEqual(result.candidate_track_ids, [14, 18])

    def test_clear_winner_among_two_still_binds(self):
        snapshots = history_with(
            [
                person(14, (250, 250, 350, 390)),   # fully inside
                person(18, (380, 250, 600, 390)),   # barely clipping
            ]
        ).snapshots()
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 14)

    def test_no_frames_at_all_reports_no_track_data(self):
        result = resolve_tap(ZONE, [], MIN_OVERLAP, MARGIN)
        self.assertEqual(result.status, BindingStatus.NO_TRACK_DATA)

    def test_unconfigured_zone_reports_no_track_data(self):
        empty = RFIDEvidenceZone(0, 0, 0, 0)
        snapshots = history_with([person(11, (250, 250, 350, 390))]).snapshots()
        result = resolve_tap(empty, snapshots, MIN_OVERLAP, MARGIN)
        self.assertEqual(result.status, BindingStatus.NO_TRACK_DATA)

    def test_untracked_detection_is_ignored(self):
        snapshots = history_with([person(None, (250, 250, 350, 390))]).snapshots()
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN)
        self.assertEqual(result.status, BindingStatus.NO_TRACK_IN_READER_ZONE)

    def test_best_frame_in_window_wins_not_the_average(self):
        # Track steps into the zone for one frame only.
        history = TrackHistory()
        history.add(1000.0, [person(11, (0, 0, 80, 200))])
        history.add(1000.1, [person(11, (250, 250, 350, 390))])
        history.add(1000.2, [person(11, (0, 0, 80, 200))])

        result = resolve_tap(ZONE, history.snapshots(), MIN_OVERLAP, MARGIN)
        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 11)


class BindingServiceTests(unittest.TestCase):
    def setUp(self):
        self.registry = WorkerRegistry(grace_s=20.0)
        self.history = TrackHistory()
        self.assignments = {
            "AB12CD34": "COLLECTOR-001",
            "EF56GH78": "COLLECTOR-002",
        }
        self.service = RFIDBindingService(
            zone=ZONE,
            history=self.history,
            registry=self.registry,
            collector_lookup=self.assignments.get,
            match_window_s=2.0,
            min_overlap=MIN_OVERLAP,
            ambiguity_margin=MARGIN,
        )

    def test_4_unknown_rfid_is_rejected_before_any_camera_work(self):
        self.history.add(1000.0, [person(11, (250, 250, 350, 390))])
        result = self.service.handle_tap("NOTATAG", 1000.0)

        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertIsNone(result["collector_id"])
        self.assertIsNone(result["track_id"])
        self.assertEqual(self.registry.to_list(), [])

    def test_empty_rfid_is_rejected(self):
        result = self.service.handle_tap("", 1000.0)
        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)

    def test_5_two_workers_bind_to_two_different_tracks(self):
        # Worker one taps while standing at the reader.
        self.history.add(1000.0, [person(11, (250, 250, 350, 390))])
        first = self.service.handle_tap("AB12CD34", 1000.0)

        # Worker two taps a few seconds later, a different track id.
        self.history.add(1010.0, [person(14, (250, 250, 350, 390))])
        second = self.service.handle_tap("EF56GH78", 1010.0)

        self.assertEqual(first["status"], BindingStatus.BOUND)
        self.assertEqual(first["track_id"], 11)
        self.assertEqual(first["collector_id"], "COLLECTOR-001")

        self.assertEqual(second["status"], BindingStatus.BOUND)
        self.assertEqual(second["track_id"], 14)
        self.assertEqual(second["collector_id"], "COLLECTOR-002")

        # Both live simultaneously -- no single global target.
        self.assertEqual(len(self.registry.bindings()), 2)
        self.assertTrue(self.registry.is_authorized(11, now=1010.0))
        self.assertTrue(self.registry.is_authorized(14, now=1010.0))

    def test_6_pedestrian_remains_unauthorised(self):
        self.history.add(1000.0, [person(11, (250, 250, 350, 390))])
        self.service.handle_tap("AB12CD34", 1000.0)

        # Track 12 never tapped anything.
        self.assertFalse(self.registry.is_authorized(12, now=1000.0))
        self.assertIsNone(self.registry.collector_for_track(12, now=1000.0))

    def test_ambiguous_tap_reports_candidates_and_binds_nothing(self):
        self.history.add(
            1000.0,
            [
                person(14, (210, 250, 300, 390)),
                person(18, (300, 250, 390, 390)),
            ],
        )
        result = self.service.handle_tap("AB12CD34", 1000.0)

        self.assertEqual(result["status"], BindingStatus.AMBIGUOUS)
        self.assertIsNone(result["track_id"])
        self.assertCountEqual(result["candidate_track_ids"], [14, 18])
        # The collector is still identified -- only the *track* is unresolved.
        self.assertEqual(result["collector_id"], "COLLECTOR-001")
        self.assertEqual(self.registry.to_list(), [])

    def test_tap_outside_the_match_window_sees_no_frames(self):
        self.history.add(900.0, [person(11, (250, 250, 350, 390))])
        result = self.service.handle_tap("AB12CD34", 1000.0)
        self.assertEqual(result["status"], BindingStatus.NO_TRACK_DATA)


if __name__ == "__main__":
    unittest.main()
