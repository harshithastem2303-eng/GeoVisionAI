"""Outbound publishing: rate limiting, failure handling, retry, bounding.

No network and no threads are needed for any of this -- the transport is
injected and the queue can be drained synchronously -- so these tests are
deterministic rather than timing-dependent.
"""

import unittest

from integration.client import TransportError, WastraqClient
from integration.events import heartbeat_event, track_update_event
from integration.publisher import EventPublisher


class RecordingTransport:
    """Collects payloads. Optionally fails the first ``fail_times`` calls."""

    def __init__(self, fail_times=0, always_fail=False, status=202):
        self.sent = []
        self.attempts = 0
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.status = status

    def post_json(self, url, payload):
        self.attempts += 1
        if self.always_fail or self.attempts <= self.fail_times:
            raise TransportError("Connection refused")
        self.sent.append((url, payload))
        return self.status


def build(transport=None, **kwargs):
    client = WastraqClient(
        base_url="http://192.0.2.10:8000",
        enabled=True,
        transport=transport or RecordingTransport(),
    )
    options = {
        "queue_max": 5,
        "retry_backoff_s": 0.0,
        "max_attempts": 3,
        "track_publish_hz": 5.0,
    }
    options.update(kwargs)
    return client, EventPublisher(client=client, **options)


def event(track_id=1):
    return track_update_event(
        source_id="GEOVISION-D455-01",
        observation={
            "track_id": track_id,
            "bbox": [0, 0, 10, 10],
            "detection_confidence": 0.9,
            "depth_valid": False,
        },
    )


class EndpointTests(unittest.TestCase):
    def test_endpoint_is_built_from_config_not_hardcoded(self):
        client = WastraqClient(base_url="http://192.0.2.10:8000/", enabled=True)
        self.assertEqual(
            client.endpoint, "http://192.0.2.10:8000/integrations/geovision/events"
        )

    def test_disabled_client_is_not_configured(self):
        self.assertFalse(WastraqClient(base_url="http://x:8000").configured)

    def test_enabled_without_a_url_is_still_not_configured(self):
        self.assertFalse(WastraqClient(base_url="", enabled=True).configured)

    def test_reachability_is_unknown_until_something_is_tried(self):
        client = WastraqClient(base_url="http://x:8000", enabled=True)
        self.assertIsNone(client.reachable)


class RateLimitingTests(unittest.TestCase):
    """Phase 7: about 5 Hz per track, not one request per camera frame."""

    def test_five_hz_admits_about_five_events_per_second(self):
        _client, publisher = build(track_publish_hz=5.0)

        admitted_at = []
        for frame in range(31):  # one second of 30 FPS capture
            now = 100.0 + frame / 30.0
            if publisher.should_publish_track(7, now=now):
                admitted_at.append(now)

        # 30 frames in, about 5 events out -- and never two inside 200 ms.
        self.assertGreaterEqual(len(admitted_at), 5)
        self.assertLessEqual(len(admitted_at), 6)
        for earlier, later in zip(admitted_at, admitted_at[1:]):
            self.assertGreaterEqual(round(later - earlier, 6), 0.2)

    def test_each_track_is_throttled_independently(self):
        _client, publisher = build(track_publish_hz=5.0)
        self.assertTrue(publisher.should_publish_track(1, now=100.0))
        self.assertTrue(publisher.should_publish_track(2, now=100.0))
        self.assertFalse(publisher.should_publish_track(1, now=100.05))
        self.assertFalse(publisher.should_publish_track(2, now=100.05))

    def test_detections_without_a_track_id_are_never_published(self):
        _client, publisher = build()
        self.assertFalse(publisher.should_publish_track(None, now=100.0))

    def test_zero_hz_disables_track_publishing(self):
        _client, publisher = build(track_publish_hz=0.0)
        self.assertFalse(publisher.should_publish_track(1, now=100.0))

    def test_session_restart_clears_the_throttle(self):
        _client, publisher = build()
        publisher.should_publish_track(1, now=100.0)
        publisher.reset_rate_limiter()
        self.assertTrue(publisher.should_publish_track(1, now=100.01))

    def test_publish_track_update_applies_the_limit(self):
        transport = RecordingTransport()
        _client, publisher = build(transport)
        self.assertTrue(publisher.publish_track_update(event(), 1, now=100.0))
        self.assertFalse(publisher.publish_track_update(event(), 1, now=100.05))
        publisher.drain_once()
        self.assertEqual(len(transport.sent), 1)


class DeliveryTests(unittest.TestCase):
    def test_successful_delivery(self):
        transport = RecordingTransport()
        client, publisher = build(transport)
        publisher.publish(event())
        self.assertEqual(publisher.drain_once(), 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(client.reachable)
        self.assertEqual(publisher.stats()["pending_events"], 0)

    def test_publishing_while_disabled_drops_instead_of_queueing(self):
        client = WastraqClient(base_url="", enabled=False)
        publisher = EventPublisher(client=client)
        self.assertFalse(publisher.publish(event()))
        self.assertEqual(publisher.pending(), 0)
        self.assertEqual(publisher.stats()["dropped_disabled"], 1)


class UnavailableWastraqTests(unittest.TestCase):
    """Phase 8: an unreachable WASTRAQ must not break GeoVision."""

    def test_failure_does_not_raise_into_the_caller(self):
        transport = RecordingTransport(always_fail=True)
        _client, publisher = build(transport)
        # publish() is what the camera thread calls; it must never raise.
        self.assertTrue(publisher.publish(event()))
        publisher.drain_once()  # also must not raise
        self.assertEqual(len(transport.sent), 0)

    def test_failed_event_is_requeued_for_retry(self):
        transport = RecordingTransport(always_fail=True)
        _client, publisher = build(transport, max_attempts=3)
        publisher.publish(event())
        publisher.drain_once()
        self.assertEqual(publisher.pending(), 1)

    def test_retry_eventually_succeeds_and_preserves_the_event_id(self):
        transport = RecordingTransport(fail_times=1)
        _client, publisher = build(transport)
        payload = event()
        publisher.publish(payload)
        publisher.drain_once()          # attempt 1 fails, requeued
        self.assertEqual(publisher.drain_once(), 1)  # attempt 2 succeeds
        # Same event_id both times: WASTRAQ deduplicates on it.
        self.assertEqual(transport.sent[0][1]["event_id"], payload["event_id"])

    def test_event_is_abandoned_after_max_attempts(self):
        transport = RecordingTransport(always_fail=True)
        _client, publisher = build(transport, max_attempts=2)
        publisher.publish(event())
        publisher.drain_once()
        publisher.drain_once()
        self.assertEqual(publisher.pending(), 0)
        self.assertEqual(publisher.stats()["dropped_exhausted"], 1)

    def test_reachability_reports_false_then_recovers(self):
        transport = RecordingTransport(fail_times=1)
        client, publisher = build(transport)
        publisher.publish(event())
        publisher.drain_once()
        self.assertFalse(client.reachable)
        publisher.drain_once()
        self.assertTrue(client.reachable)


class BoundedQueueTests(unittest.TestCase):
    """Phase 8: bounded buffer, oldest dropped first."""

    def test_queue_never_exceeds_its_cap(self):
        transport = RecordingTransport(always_fail=True)
        _client, publisher = build(transport, queue_max=3)
        for _ in range(10):
            publisher.publish(event())
        self.assertEqual(publisher.pending(), 3)
        self.assertEqual(publisher.stats()["dropped_overflow"], 7)

    def test_the_oldest_event_is_the_one_dropped(self):
        transport = RecordingTransport()
        _client, publisher = build(transport, queue_max=2)
        first, second, third = event(1), event(2), event(3)
        for payload in (first, second, third):
            publisher.publish(payload)
        publisher.drain_once()
        sent_ids = [payload["event_id"] for _url, payload in transport.sent]
        self.assertNotIn(first["event_id"], sent_ids)
        self.assertEqual(sent_ids, [second["event_id"], third["event_id"]])


class ThreadLifecycleTests(unittest.TestCase):
    def test_start_and_stop_are_idempotent(self):
        transport = RecordingTransport()
        _client, publisher = build(transport)
        publisher.start()
        publisher.start()
        self.assertTrue(publisher.running)
        publisher.stop()
        publisher.stop()
        self.assertFalse(publisher.running)

    def test_background_thread_delivers(self):
        import time

        transport = RecordingTransport()
        _client, publisher = build(transport)
        publisher.start()
        try:
            publisher.publish(heartbeat_event("GEOVISION-D455-01"))
            deadline = time.time() + 3.0
            while not transport.sent and time.time() < deadline:
                time.sleep(0.02)
        finally:
            publisher.stop()
        self.assertEqual(len(transport.sent), 1)


class NoPropertyLeakTests(unittest.TestCase):
    """ABSOLUTE RULE 2, enforced at the point of transmission."""

    def test_nothing_on_the_wire_carries_a_property_id(self):
        from integration.events import find_property_fields

        transport = RecordingTransport()
        _client, publisher = build(transport)
        leaky = {
            "track_id": 1,
            "bbox": [0, 0, 1, 1],
            "detection_confidence": 0.5,
            "property_id": "PROP-003",
            "segregation_status": "NOT_SEGREGATED",
        }
        publisher.publish(track_update_event("S", leaky))
        publisher.drain_once()
        _url, payload = transport.sent[0]
        self.assertEqual(find_property_fields(payload), [])


if __name__ == "__main__":
    unittest.main()
