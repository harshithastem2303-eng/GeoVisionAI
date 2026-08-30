"""Collector identification by proximity, track lock, and what a re-tap means.

Three things are under test, and each is a rule the earlier design got wrong
or did not have:

1. **The first tap identifies.** Among the people the camera was tracking,
   the tapper is the one closest to the camera -- not the first detected, not
   the largest box. Zone-overlapping tracks are preferred; proximity decides
   within them.
2. **The binding is a lock.** Once a collector holds a track, that track is
   followed exclusively. Someone else walking nearer does not steal it, and
   the collector's own second tap does not re-run the choice.
3. **A second tap means non-segregation** -- but only against an episode
   WASTRAQ has actually opened. With no episode, nothing is marked.

Stdlib only: no camera, no reader, no database, no network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.episode_registry import EpisodeRegistry  # noqa: E402
from vision.rfid_binding import (  # noqa: E402
    RFIDBindingService,
    RFIDEvidenceZone,
    resolve_tap,
    summarize_tracks,
)
from vision.track_history import TrackHistory  # noqa: E402
from vision.types import (  # noqa: E402
    BindingStatus,
    PersonDetection,
    SelectionRule,
    TapIntent,
)
from vision.worker_registry import WorkerRegistry  # noqa: E402

ZONE = RFIDEvidenceZone(x1=200, y1=200, x2=400, y2=400)
MIN_OVERLAP = 0.15
MARGIN = 0.20
DEPTH_MARGIN = 0.5

IN_ZONE = (250, 250, 350, 390)
IN_ZONE_LEFT = (205, 250, 300, 390)
IN_ZONE_RIGHT = (300, 250, 395, 390)
OUTSIDE = (0, 0, 80, 200)
OUTSIDE_RIGHT = (500, 0, 600, 200)


def person(track_id, bbox, depth_m=None, confidence=0.9):
    return PersonDetection(
        track_id=track_id,
        bbox=bbox,
        confidence=confidence,
        depth_m=depth_m,
        depth_valid=depth_m is not None,
    )


def window(detections, timestamp=1000.0, frames=3):
    history = TrackHistory()
    for i in range(frames):
        history.add(timestamp + i * 0.1, detections)
    return history.snapshots()


class DepthSummaryTests(unittest.TestCase):
    def test_closest_approach_in_the_window_is_what_counts(self):
        history = TrackHistory()
        history.add(1000.0, [person(7, IN_ZONE, depth_m=3.4)])
        history.add(1000.1, [person(7, IN_ZONE, depth_m=1.1)])  # reaches in
        history.add(1000.2, [person(7, IN_ZONE, depth_m=2.9)])  # steps back

        summary = summarize_tracks(ZONE, history.snapshots())
        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary[0].depth_m, 1.1)

    def test_a_track_with_no_valid_depth_is_summarised_without_one(self):
        summary = summarize_tracks(ZONE, window([person(7, IN_ZONE)]))
        self.assertFalse(summary[0].depth_valid)
        self.assertIsNone(summary[0].depth_m)
        # The zone reading still happened.
        self.assertGreater(summary[0].overlap, 0.9)


class ClosestPersonTests(unittest.TestCase):
    def test_closest_person_in_the_zone_is_the_tapper(self):
        # Track 35 is nearer the camera than track 12, both at the reader.
        snapshots = window(
            [
                person(12, IN_ZONE_LEFT, depth_m=2.8),
                person(35, IN_ZONE_RIGHT, depth_m=1.2),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 35)
        self.assertEqual(result.selection_rule, SelectionRule.DEPTH_IN_ZONE)

    def test_first_detected_person_does_not_win_by_being_first(self):
        # Track 3 appears first in every frame but stands further back.
        snapshots = window(
            [
                person(3, IN_ZONE_LEFT, depth_m=3.0),
                person(35, IN_ZONE_RIGHT, depth_m=1.0),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)
        self.assertEqual(result.track_id, 35)

    def test_bigger_box_further_away_does_not_win(self):
        # A large box (someone close to the lens optically) that depth says is
        # far must lose to a small box that depth says is near.
        snapshots = window(
            [
                person(4, (200, 200, 400, 400), depth_m=4.5),
                person(9, (330, 330, 360, 395), depth_m=0.9),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)
        self.assertEqual(result.track_id, 9)

    def test_two_people_equally_close_stay_ambiguous(self):
        snapshots = window(
            [
                person(12, IN_ZONE_LEFT, depth_m=1.30),
                person(35, IN_ZONE_RIGHT, depth_m=1.35),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.status, BindingStatus.AMBIGUOUS)
        self.assertIsNone(result.track_id)
        self.assertCountEqual(result.candidate_track_ids, [12, 35])

    def test_tracks_without_depth_are_not_considered_when_depth_exists(self):
        # 12 has no depth at all; 35 does. The measured one decides, and the
        # unmeasured one is not silently ranked as "very far".
        snapshots = window(
            [
                person(12, IN_ZONE_LEFT),
                person(35, IN_ZONE_RIGHT, depth_m=2.4),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 35)
        self.assertEqual(result.candidate_track_ids, [35])


class ZonePreferenceTests(unittest.TestCase):
    def test_zone_is_preferred_over_a_closer_person_outside_it(self):
        # Track 2 is nearer the camera but nowhere near the reader; track 35
        # is at the reader. The surveyed zone is the stronger evidence.
        snapshots = window(
            [
                person(2, OUTSIDE, depth_m=0.6),
                person(35, IN_ZONE, depth_m=1.9),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.track_id, 35)
        self.assertEqual(result.selection_rule, SelectionRule.DEPTH_IN_ZONE)

    def test_when_the_zone_singles_nobody_out_proximity_decides(self):
        # The zone rectangle is still an unsurveyed guess; both people miss
        # it. Rather than fail every tap, closest-to-camera decides -- and the
        # result says so, at lower confidence.
        snapshots = window(
            [
                person(2, OUTSIDE, depth_m=3.1),
                person(35, OUTSIDE_RIGHT, depth_m=1.0),
            ]
        )
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 35)
        self.assertEqual(result.selection_rule, SelectionRule.DEPTH_ANY)
        self.assertLess(result.confidence, 0.7)
        self.assertIn("closest person", result.reason)


class DepthOutageTests(unittest.TestCase):
    def test_no_depth_anywhere_falls_back_to_the_zone_rule(self):
        snapshots = window([person(11, IN_ZONE)])
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)

        self.assertEqual(result.status, BindingStatus.BOUND)
        self.assertEqual(result.track_id, 11)
        self.assertEqual(result.selection_rule, SelectionRule.ZONE_OVERLAP)

    def test_no_depth_and_nobody_in_zone_is_still_unresolved(self):
        snapshots = window([person(12, OUTSIDE), person(13, OUTSIDE_RIGHT)])
        result = resolve_tap(ZONE, snapshots, MIN_OVERLAP, MARGIN, DEPTH_MARGIN)
        self.assertEqual(result.status, BindingStatus.NO_TRACK_IN_READER_ZONE)


# ---------------------------------------------------------------------------
# The service: lock, and the two meanings of a tap
# ---------------------------------------------------------------------------


class ContextualTapTests(unittest.TestCase):
    def setUp(self):
        self.registry = WorkerRegistry(grace_s=20.0)
        self.history = TrackHistory()
        self.episodes = EpisodeRegistry(max_age_s=180.0)
        self.service = RFIDBindingService(
            zone=ZONE,
            history=self.history,
            registry=self.registry,
            collector_lookup={"04A1B24C": "PICKER-01", "9911FFAA": "PICKER-02"}.get,
            match_window_s=2.0,
            min_overlap=MIN_OVERLAP,
            ambiguity_margin=MARGIN,
            depth_margin_m=DEPTH_MARGIN,
            episodes=self.episodes,
            retap_debounce_s=10.0,
        )

    def bind_picker_one(self, timestamp=1000.0):
        self.history.add(
            timestamp,
            [
                person(12, IN_ZONE_LEFT, depth_m=3.0),
                person(35, IN_ZONE_RIGHT, depth_m=1.1),
            ],
        )
        return self.service.handle_tap("04A1B24C", timestamp)

    def open_episode(self, episode_id, track_id=35, status="AUTO_ASSOCIATED", now=1005.0):
        return self.episodes.open(
            episode_id=episode_id,
            track_id=track_id,
            session_id=self.registry.session_id,
            association_status=status,
            collector_id="PICKER-01",
            now=now,
        )

    # -- Case 1 ----------------------------------------------------------

    def test_first_tap_binds_the_closest_valid_depth_track(self):
        result = self.bind_picker_one()

        self.assertEqual(result["intent"], TapIntent.BIND)
        self.assertEqual(result["status"], BindingStatus.BOUND)
        self.assertEqual(result["collector_id"], "PICKER-01")
        self.assertEqual(result["track_id"], 35)
        self.assertTrue(result["locked"])

    def test_unknown_tag_binds_nothing_and_flags_nothing(self):
        self.history.add(1000.0, [person(35, IN_ZONE, depth_m=1.0)])
        result = self.service.handle_tap("DEADBEEF", 1000.0)

        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertIsNone(result["intent"])
        self.assertEqual(self.registry.to_list(), [])

    # -- The lock ---------------------------------------------------------

    def test_a_nearer_stranger_does_not_steal_the_locked_track(self):
        self.bind_picker_one()

        # A pedestrian now walks much closer to the camera than the picker.
        self.history.add(1004.0, [person(77, IN_ZONE, depth_m=0.4)])
        self.registry.touch([35], now=1004.0)

        binding = self.registry.binding_for_collector("PICKER-01", now=1004.0)
        self.assertEqual(binding.track_id, 35)
        self.assertFalse(self.registry.is_authorized(77, now=1004.0))
        self.assertEqual(self.registry.active_track_ids(now=1004.0), [35])

    def test_second_tap_never_rebinds_even_with_a_closer_candidate(self):
        self.bind_picker_one()
        self.open_episode("EP-1")

        self.history.add(1005.0, [person(88, IN_ZONE, depth_m=0.3)])
        self.registry.touch([35], now=1005.0)
        result = self.service.handle_tap("04A1B24C", 1005.0)

        self.assertEqual(result["intent"], TapIntent.NON_SEGREGATION)
        self.assertEqual(result["track_id"], 35)
        self.assertEqual(
            self.registry.binding_for_collector("PICKER-01", now=1005.0).track_id, 35
        )

    def test_a_second_collector_binds_to_their_own_track(self):
        self.bind_picker_one()
        self.history.add(1006.0, [person(41, IN_ZONE, depth_m=1.0)])
        self.registry.touch([35], now=1006.0)

        result = self.service.handle_tap("9911FFAA", 1006.0)

        self.assertEqual(result["intent"], TapIntent.BIND)
        self.assertEqual(result["collector_id"], "PICKER-02")
        self.assertEqual(result["track_id"], 41)
        self.assertEqual(len(self.registry.bindings()), 2)

    def test_binding_released_after_grace_allows_reidentification(self):
        self.bind_picker_one()
        # Track lost well beyond the grace period.
        late = 1000.0 + 60.0
        self.assertIsNone(self.registry.binding_for_collector("PICKER-01", now=late))

        self.history.add(late, [person(52, IN_ZONE, depth_m=1.4)])
        result = self.service.handle_tap("04A1B24C", late)
        self.assertEqual(result["intent"], TapIntent.BIND)
        self.assertEqual(result["track_id"], 52)

    # -- Case 2 -----------------------------------------------------------

    def test_retap_during_an_episode_flags_non_segregation(self):
        self.bind_picker_one()
        self.open_episode("EP-HOUSE-2")
        self.registry.touch([35], now=1005.0)

        result = self.service.handle_tap("04A1B24C", 1005.0)

        self.assertEqual(result["status"], BindingStatus.NON_SEGREGATION)
        self.assertEqual(result["episode_id"], "EP-HOUSE-2")
        self.assertIsNotNone(result["trigger_id"])
        self.assertTrue(self.episodes.active_for_track(35, now=1005.0).non_segregated)

    def test_repeat_taps_are_idempotent(self):
        self.bind_picker_one()
        self.open_episode("EP-HOUSE-2")
        self.registry.touch([35], now=1005.0)

        first = self.service.handle_tap("04A1B24C", 1005.0)
        # A bounced read, then a deliberate re-tap well after the debounce.
        second = self.service.handle_tap("04A1B24C", 1005.4)
        self.registry.touch([35], now=1030.0)
        third = self.service.handle_tap("04A1B24C", 1030.0)

        self.assertEqual(first["status"], BindingStatus.NON_SEGREGATION)
        self.assertEqual(second["status"], BindingStatus.DUPLICATE_TRIGGER)
        self.assertEqual(third["status"], BindingStatus.DUPLICATE_TRIGGER)
        self.assertEqual(first["trigger_id"], second["trigger_id"])
        self.assertEqual(first["trigger_id"], third["trigger_id"])

    def test_review_episodes_may_be_flagged(self):
        self.bind_picker_one()
        self.open_episode("EP-REVIEW", status="REVIEW")
        self.registry.touch([35], now=1005.0)

        result = self.service.handle_tap("04A1B24C", 1005.0)
        self.assertEqual(result["status"], BindingStatus.NON_SEGREGATION)

    def test_unassociated_episodes_may_not_be_flagged(self):
        self.bind_picker_one()
        self.open_episode("EP-AMBIG", status="AMBIGUOUS")
        self.registry.touch([35], now=1005.0)

        result = self.service.handle_tap("04A1B24C", 1005.0)

        self.assertEqual(result["status"], BindingStatus.EPISODE_NOT_ACTIONABLE)
        self.assertIsNone(result["trigger_id"])
        self.assertFalse(self.episodes.active_for_track(35, now=1005.0).non_segregated)

    # -- Case 3 -----------------------------------------------------------

    def test_retap_with_no_open_episode_marks_nothing(self):
        self.bind_picker_one()
        self.registry.touch([35], now=1005.0)

        result = self.service.handle_tap("04A1B24C", 1005.0)

        self.assertEqual(result["status"], BindingStatus.NO_ACTIVE_EPISODE)
        self.assertIsNone(result["trigger_id"])
        self.assertIsNone(result["episode_id"])
        self.assertEqual(self.episodes.to_list(), [])

    def test_a_closed_episode_cannot_absorb_a_later_tap(self):
        """House 1 is finished; a tap in the gap must not re-open it."""

        self.bind_picker_one()
        self.open_episode("EP-HOUSE-1")
        self.episodes.close(episode_id="EP-HOUSE-1")
        self.registry.touch([35], now=1006.0)

        result = self.service.handle_tap("04A1B24C", 1006.0)
        self.assertEqual(result["status"], BindingStatus.NO_ACTIVE_EPISODE)

    def test_a_stale_episode_expires_rather_than_waiting_to_be_flagged(self):
        self.bind_picker_one()
        self.open_episode("EP-OLD", now=1005.0)
        self.registry.touch([35], now=1005.0 + 400.0)

        result = self.service.handle_tap("04A1B24C", 1005.0 + 400.0)
        self.assertEqual(result["status"], BindingStatus.NO_ACTIVE_EPISODE)

    def test_service_without_an_episode_registry_degrades_safely(self):
        service = RFIDBindingService(
            zone=ZONE,
            history=self.history,
            registry=self.registry,
            collector_lookup={"04A1B24C": "PICKER-01"}.get,
            match_window_s=2.0,
            min_overlap=MIN_OVERLAP,
            ambiguity_margin=MARGIN,
        )
        self.history.add(1000.0, [person(35, IN_ZONE, depth_m=1.0)])
        service.handle_tap("04A1B24C", 1000.0)
        # Well clear of the bind-echo window: this is a deliberate second tap,
        # not the identifying tap arriving twice.
        self.registry.touch([35], now=1005.0)

        result = service.handle_tap("04A1B24C", 1005.0)
        self.assertEqual(result["status"], BindingStatus.NO_ACTIVE_EPISODE)


class EpisodeMirrorTests(unittest.TestCase):
    """The mirror stores what WASTRAQ said, and nothing that names a house."""

    def setUp(self):
        self.episodes = EpisodeRegistry(max_age_s=180.0)

    def test_property_fields_are_dropped_before_storage(self):
        clean = EpisodeRegistry.sanitize(
            {
                "episode_id": "EP-1",
                "track_id": 35,
                "property_id": "PROP-004",
                "house_number": "12A",
                "segregation_status": "NOT_SEGREGATED",
                "association_status": "AUTO_ASSOCIATED",
            }
        )
        self.assertEqual(
            sorted(clean), ["association_status", "episode_id", "track_id"]
        )

    def test_a_stored_episode_names_no_property(self):
        episode = self.episodes.open(
            episode_id="EP-1",
            track_id=35,
            session_id="sess",
            association_status="AUTO_ASSOCIATED",
            now=1000.0,
        )
        self.assertNotIn("property_id", episode.to_dict())
        self.assertNotIn("house_number", episode.to_dict())

    def test_moving_to_the_next_house_replaces_the_episode(self):
        self.episodes.open("EP-1", 35, "sess", "AUTO_ASSOCIATED", now=1000.0)
        self.episodes.mark_non_segregated(
            self.episodes.active_for_track(35, now=1000.0), now=1000.0
        )
        self.episodes.open("EP-2", 35, "sess", "AUTO_ASSOCIATED", now=1050.0)

        live = self.episodes.active_for_track(35, now=1050.0)
        self.assertEqual(live.episode_id, "EP-2")
        # The new house starts clean; it does not inherit House 1's flag.
        self.assertFalse(live.non_segregated)

    def test_reannouncing_the_same_episode_keeps_a_spent_trigger(self):
        self.episodes.open("EP-1", 35, "sess", "AUTO_ASSOCIATED", now=1000.0)
        trigger, _ = self.episodes.mark_non_segregated(
            self.episodes.active_for_track(35, now=1000.0), now=1000.0
        )
        self.episodes.open("EP-1", 35, "sess", "AUTO_ASSOCIATED", now=1010.0)

        live = self.episodes.active_for_track(35, now=1010.0)
        self.assertEqual(live.non_segregation_trigger_id, trigger)

    def test_an_episode_from_a_previous_session_never_matches(self):
        self.episodes.open("EP-1", 35, "old-session", "AUTO_ASSOCIATED", now=1000.0)
        self.assertIsNone(
            self.episodes.active_for_track(35, session_id="new-session", now=1000.0)
        )


if __name__ == "__main__":
    unittest.main()
