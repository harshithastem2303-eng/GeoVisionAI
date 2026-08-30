"""The demo sequence, driven through the real route handlers.

FastAPI cannot be installed in every environment this repo is developed in,
so ``fastapi`` is stubbed down to the two symbols the routers touch --
``APIRouter`` and ``HTTPException``. Everything else is the repository's own
code, executed unmodified: the real pydantic request models, the real route
function bodies, the real binding service, the real episode mirror, the real
event builders and the real publisher.

What it walks is the demo, end to end::

    collector taps            -> PICKER-01 locked to the closest track
    HOUSE 1 episode opens     -> serviced, no second tap
    HOUSE 1 closes            -> stays SEGREGATED (nothing was emitted)
    HOUSE 2 episode opens     -> collector taps again
                              -> NON_SEGREGATION_TRIGGER + evidence clip

And the two ways it must refuse: a tap with no episode open, and a tap from
a tag nobody owns.
"""

from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Minimal fastapi stand-in, installed before any router import
# ---------------------------------------------------------------------------


class _StubRouter:
    """Records nothing and decorates nothing away: handlers stay callable."""

    def __init__(self, *args, **kwargs):
        self.routes = []

    def _register(self, path, method):
        def decorator(func):
            self.routes.append((method, path, func))
            return func

        return decorator

    def get(self, path, **kwargs):
        return self._register(path, "GET")

    def post(self, path, **kwargs):
        return self._register(path, "POST")

    def delete(self, path, **kwargs):
        return self._register(path, "DELETE")

    def put(self, path, **kwargs):
        return self._register(path, "PUT")

    def patch(self, path, **kwargs):
        return self._register(path, "PATCH")


class _StubHTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _StubStreamingResponse:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _install_fastapi_stub() -> bool:
    """Stub ``fastapi`` only if the real one is genuinely absent."""

    try:
        import fastapi  # noqa: F401

        return False
    except ImportError:
        pass

    stub = types.ModuleType("fastapi")
    stub.__path__ = []  # a package, so submodules can be registered
    stub.APIRouter = _StubRouter
    stub.HTTPException = _StubHTTPException
    sys.modules["fastapi"] = stub

    responses = types.ModuleType("fastapi.responses")
    responses.StreamingResponse = _StubStreamingResponse
    responses.JSONResponse = _StubStreamingResponse
    responses.Response = _StubStreamingResponse
    # routes/evidence.py serves clips with FileResponse.
    responses.FileResponse = _StubStreamingResponse
    stub.responses = responses
    sys.modules["fastapi.responses"] = responses
    return True


_STUBBED_FASTAPI = _install_fastapi_stub()

import config  # noqa: E402
import database  # noqa: E402

# The collector registry is a database lookup in production. Here it is a
# dict, injected the same way the real service injects the real one.
ASSIGNMENTS = {"04A1B24C": "PICKER-01", "9911FFAA": "PICKER-02"}
database.collector_for_rfid = ASSIGNMENTS.get  # noqa: E402

import services  # noqa: E402
from integration.client import WastraqClient  # noqa: E402
from integration.events import (  # noqa: E402
    EVENT_NON_SEGREGATION_TRIGGER,
    EVENT_RFID_TAP,
    EVENT_WORKER_TRACK_BOUND,
    find_property_fields,
)
from integration.publisher import EventPublisher  # noqa: E402
from routes import episodes as episodes_route  # noqa: E402
from routes import rfid as rfid_route  # noqa: E402
from schemas import EpisodeIn, RFIDEventIn  # noqa: E402
from vision.types import BindingStatus, PersonDetection  # noqa: E402

if _STUBBED_FASTAPI:
    # The routers are imported now and never touch fastapi again, so the stub
    # has done its job. Leaving it in ``sys.modules`` would make every other
    # test module believe FastAPI is installed -- and the one that imports
    # ``app`` would fail instead of skipping.
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.responses", None)

READER_ZONE_BOX = (250, 250, 350, 390)
BACKGROUND_BOX = (40, 250, 140, 390)


class RecordingTransport:
    def __init__(self):
        self.sent = []

    def post_json(self, url, payload):
        self.sent.append(payload)
        return 202


def person(track_id, bbox, depth_m):
    return PersonDetection(
        track_id=track_id,
        bbox=bbox,
        confidence=0.93,
        depth_m=depth_m,
        depth_valid=depth_m is not None,
    )


class DemoSequenceTests(unittest.TestCase):
    """Reset every shared singleton, then walk the lane."""

    def setUp(self):
        services.rfid_service.collector_lookup = ASSIGNMENTS.get
        services.rfid_service.zone = services.RFIDEvidenceZone(200, 200, 400, 400)
        services.rfid_service.episodes = services.episode_registry

        services.worker_registry.clear()
        services.episode_registry.clear()
        services.track_history.clear()

        self.transport = RecordingTransport()
        services.publisher.client = WastraqClient(
            base_url="http://192.0.2.10:8000",
            enabled=True,
            transport=self.transport,
        )
        # Publish inline so assertions do not race the sender thread.
        services.publisher.publish = lambda event: bool(
            services.publisher.client.send(event)
        )

        self.clip_requests = []
        services.clip_buffer.request_clip = self._record_clip

    def _record_clip(self, **kwargs):
        self.clip_requests.append(kwargs)

        class _Clip:
            clip_id = "CLIP-TEST"
            status = "PENDING"

            @staticmethod
            def to_dict():
                return {"clip_id": "CLIP-TEST", "status": "PENDING"}

        return _Clip()

    # -- helpers ---------------------------------------------------------

    # Timestamps are real epoch seconds, as a reader bridge sends them. The
    # binding's lifetime, the frame history and the episode mirror all measure
    # against the same clock, and these tests would catch it if they did not.

    def see(self, detections, at):
        services.track_history.add(at, detections)
        services.worker_registry.touch(
            [d.track_id for d in detections if d.track_id is not None], now=at
        )

    def tap(self, uid, at):
        return rfid_route.rfid_event(RFIDEventIn(rfid_id=uid, timestamp=at))

    def open_episode(self, episode_id, track_id, at, status="AUTO_ASSOCIATED"):
        return episodes_route.open_episode(
            EpisodeIn(
                episode_id=episode_id,
                track_id=track_id,
                association_status=status,
                collector_id="PICKER-01",
                opened_at=at,
            )
        )

    def events_of(self, event_type):
        return [e for e in self.transport.sent if e["event_type"] == event_type]

    # -- the walk --------------------------------------------------------

    def test_full_demo_sequence(self):
        t = time.time()

        # 1-3. Two people in frame. The collector is nearer the camera when
        #      they reach out and tap; the other is further back.
        self.see(
            [
                person(35, READER_ZONE_BOX, 1.0),
                person(12, BACKGROUND_BOX, 3.2),
            ],
            at=t,
        )
        bind = self.tap("04A1B24C", t)

        # 4-7. PICKER-01 is locked to track 35.
        self.assertEqual(bind["status"], BindingStatus.BOUND)
        self.assertEqual(bind["collector_id"], "PICKER-01")
        self.assertEqual(bind["track_id"], 35)
        self.assertTrue(bind["locked"])
        self.assertEqual(len(self.events_of(EVENT_WORKER_TRACK_BOUND)), 1)

        # 8-13. HOUSE 1. Episode opens, is serviced, and closes with no
        #       second tap: it stays SEGREGATED by saying nothing.
        t += 5
        self.see([person(35, READER_ZONE_BOX, 1.4)], at=t)
        opened = self.open_episode("EP-HOUSE-1", 35, at=t)
        self.assertTrue(opened["accepted"])
        self.assertTrue(opened["actionable"])

        t += 10
        self.see([person(35, READER_ZONE_BOX, 1.6)], at=t)
        episodes_route.close_episode("EP-HOUSE-1")

        self.assertEqual(self.events_of(EVENT_NON_SEGREGATION_TRIGGER), [])

        # 14-21. HOUSE 2. Same locked track, new episode, and this time the
        #        collector taps again.
        t += 8
        self.see([person(35, READER_ZONE_BOX, 1.2)], at=t)
        self.open_episode("EP-HOUSE-2", 35, at=t)

        t += 6
        # A pedestrian is now nearer the camera than the picker. The lock
        # must hold: this tap belongs to track 35 regardless.
        self.see(
            [person(35, READER_ZONE_BOX, 1.3), person(77, READER_ZONE_BOX, 0.4)],
            at=t,
        )
        flag = self.tap("04A1B24C", t)

        self.assertEqual(flag["status"], BindingStatus.NON_SEGREGATION)
        self.assertEqual(flag["intent"], "NON_SEGREGATION")
        self.assertEqual(flag["track_id"], 35)
        self.assertEqual(flag["episode_id"], "EP-HOUSE-2")
        self.assertTrue(flag["resolved"])

        triggers = self.events_of(EVENT_NON_SEGREGATION_TRIGGER)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["episode_id"], "EP-HOUSE-2")
        self.assertEqual(triggers[0]["collector_id"], "PICKER-01")
        self.assertFalse(triggers[0]["duplicate"])

        # 21. Evidence was requested for the flag, and for the bind.
        self.assertEqual(len(self.clip_requests), 2)
        self.assertEqual(self.clip_requests[-1]["track_id"], 35)

        # 23. Episode closes.
        episodes_route.close_episode("EP-HOUSE-2")
        self.assertEqual(episodes_route.list_episodes()["count"], 0)

        # No event in the whole run named a property.
        for event in self.transport.sent:
            self.assertEqual(find_property_fields(event), [])

    def test_tap_with_no_open_episode_marks_nothing(self):
        t = time.time()
        self.see([person(35, READER_ZONE_BOX, 1.0)], at=t)
        self.tap("04A1B24C", t)

        t += 4
        self.see([person(35, READER_ZONE_BOX, 1.1)], at=t)
        result = self.tap("04A1B24C", t)

        self.assertEqual(result["status"], BindingStatus.NO_ACTIVE_EPISODE)
        self.assertIsNone(result["episode_id"])
        self.assertIsNone(result["trigger_id"])
        self.assertNotIn("evidence", result)

        # Still reported to WASTRAQ, which owns the authoritative episodes.
        triggers = self.events_of(EVENT_NON_SEGREGATION_TRIGGER)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["trigger_status"], "NO_ACTIVE_EPISODE")
        self.assertIsNone(triggers[0]["episode_id"])

    def test_unknown_tag_neither_binds_nor_flags(self):
        t = time.time()
        self.see([person(35, READER_ZONE_BOX, 1.0)], at=t)
        result = self.tap("FFFFFFFF", t)

        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertEqual(self.events_of(EVENT_WORKER_TRACK_BOUND), [])
        self.assertEqual(self.events_of(EVENT_NON_SEGREGATION_TRIGGER), [])
        self.assertEqual(len(self.events_of(EVENT_RFID_TAP)), 1)

    def test_episode_for_a_track_locked_to_someone_else_is_rejected(self):
        t = time.time()
        self.see([person(35, READER_ZONE_BOX, 1.0)], at=t)
        self.tap("04A1B24C", t)

        rejected = episodes_route.open_episode(
            EpisodeIn(
                episode_id="EP-WRONG",
                track_id=35,
                association_status="AUTO_ASSOCIATED",
                collector_id="PICKER-02",
            )
        )
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "COLLECTOR_TRACK_MISMATCH")

    def test_episode_from_a_dead_session_is_rejected(self):
        rejected = episodes_route.open_episode(
            EpisodeIn(
                episode_id="EP-STALE",
                track_id=35,
                association_status="AUTO_ASSOCIATED",
                session_id="a-session-that-ended",
            )
        )
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "STALE_SESSION")

    def test_releasing_a_binding_drops_its_episode(self):
        t = time.time()
        self.see([person(35, READER_ZONE_BOX, 1.0)], at=t)
        self.tap("04A1B24C", t)
        self.open_episode("EP-OPEN", 35, at=t)

        released = rfid_route.release_binding("PICKER-01")
        self.assertTrue(released["released"])
        self.assertEqual(released["track_id"], 35)
        self.assertEqual(episodes_route.list_episodes()["count"], 0)


if __name__ == "__main__":
    unittest.main()
