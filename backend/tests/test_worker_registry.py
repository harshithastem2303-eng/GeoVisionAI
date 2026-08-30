"""Binding lifecycle: occlusion grace, expiry, and session scoping."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.worker_registry import WorkerRegistry  # noqa: E402

T0 = 1000.0


def bound(registry, collector="COLLECTOR-001", track=11, now=T0):
    return registry.bind(
        collector_id=collector,
        rfid_id="AB12CD34",
        track_id=track,
        confidence=0.95,
        event_timestamp=now,
        now=now,
    )


class OcclusionTests(unittest.TestCase):
    def test_7_binding_survives_a_short_missing_period(self):
        registry = WorkerRegistry(grace_s=20.0)
        bound(registry)

        # 8 seconds behind the vehicle -- still the same worker.
        self.assertTrue(registry.is_authorized(11, now=T0 + 8))
        registry.expire(now=T0 + 8)
        self.assertEqual(len(registry.bindings()), 1)

        # Reappears; last_seen refreshes.
        registry.touch([11], now=T0 + 9)
        self.assertTrue(registry.is_authorized(11, now=T0 + 25))

    def test_binding_expires_after_the_grace_period(self):
        registry = WorkerRegistry(grace_s=20.0)
        bound(registry)

        self.assertFalse(registry.is_authorized(11, now=T0 + 21))
        removed = registry.expire(now=T0 + 21)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].status, "EXPIRED")
        self.assertEqual(registry.bindings(), [])

    def test_absolute_ceiling_expires_even_a_continuously_seen_track(self):
        registry = WorkerRegistry(grace_s=20.0, max_age_s=100.0)
        bound(registry)
        registry.touch([11], now=T0 + 150)  # still visible
        removed = registry.expire(now=T0 + 150)
        self.assertEqual(len(removed), 1)


class SessionTests(unittest.TestCase):
    def test_8_stale_session_binding_is_not_reused_after_restart(self):
        registry = WorkerRegistry(grace_s=20.0)
        first_session = registry.session_id
        bound(registry)
        self.assertTrue(registry.is_authorized(11, now=T0))

        # Pipeline restarts; BoT-SORT renumbers from scratch.
        second_session = registry.start_session()

        self.assertNotEqual(first_session, second_session)
        self.assertEqual(registry.bindings(), [])
        # Track 11 in the new run is a different human.
        self.assertFalse(registry.is_authorized(11, now=T0 + 1))
        self.assertIsNone(registry.collector_for_track(11, now=T0 + 1))

    def test_bindings_carry_the_session_they_were_made_in(self):
        registry = WorkerRegistry()
        binding = bound(registry)
        self.assertEqual(binding.session_id, registry.session_id)


class MultiWorkerTests(unittest.TestCase):
    def test_two_collectors_hold_two_tracks_concurrently(self):
        registry = WorkerRegistry()
        bound(registry, "COLLECTOR-001", 11)
        bound(registry, "COLLECTOR-002", 14)

        self.assertEqual(registry.collector_for_track(11, now=T0), "COLLECTOR-001")
        self.assertEqual(registry.collector_for_track(14, now=T0), "COLLECTOR-002")
        self.assertEqual(len(registry.bindings()), 2)

    def test_a_track_belongs_to_only_one_collector(self):
        registry = WorkerRegistry()
        bound(registry, "COLLECTOR-001", 11)
        bound(registry, "COLLECTOR-002", 11)  # same track, different person

        self.assertEqual(len(registry.bindings()), 1)
        self.assertEqual(registry.collector_for_track(11, now=T0), "COLLECTOR-002")

    def test_rebinding_a_collector_moves_it_to_the_new_track(self):
        registry = WorkerRegistry()
        bound(registry, "COLLECTOR-001", 11)
        bound(registry, "COLLECTOR-001", 14)

        self.assertEqual(len(registry.bindings()), 1)
        self.assertFalse(registry.is_authorized(11, now=T0))
        self.assertTrue(registry.is_authorized(14, now=T0))

    def test_unbound_track_and_none_are_never_authorised(self):
        registry = WorkerRegistry()
        bound(registry, "COLLECTOR-001", 11)

        self.assertFalse(registry.is_authorized(12, now=T0))
        self.assertFalse(registry.is_authorized(None, now=T0))

    def test_release_removes_a_binding(self):
        registry = WorkerRegistry()
        bound(registry)
        self.assertTrue(registry.release("COLLECTOR-001"))
        self.assertFalse(registry.release("COLLECTOR-001"))
        self.assertEqual(registry.bindings(), [])

    def test_touch_only_refreshes_visible_tracks(self):
        registry = WorkerRegistry(grace_s=20.0)
        bound(registry, "COLLECTOR-001", 11)
        bound(registry, "COLLECTOR-002", 14)

        registry.touch([11], now=T0 + 15)
        registry.expire(now=T0 + 25)

        self.assertTrue(registry.is_authorized(11, now=T0 + 25))
        self.assertFalse(registry.is_authorized(14, now=T0 + 25))


if __name__ == "__main__":
    unittest.main()
