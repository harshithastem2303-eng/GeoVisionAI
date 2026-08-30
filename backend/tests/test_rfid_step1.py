"""STEP 1: the first runtime tap identifies the collector and locks a track.

Everything here is about the *first* tap and only the first tap. The question
it has to answer correctly, every time, with a physical card and a live D455:

    A card was presented. Which collector is that, and which of the people the
    camera is tracking is holding it?

The six behaviours below are the ones that decide whether that answer can be
trusted on demo day:

===========================================  =================================
one known card, one valid-depth track        BOUND, track locked
a card nobody owns                           UNKNOWN_RFID, nothing touched
nobody in frame                              NO_TRACK_DATA, nothing touched
two people equally close                     AMBIGUOUS, nothing bound
someone walks nearer afterwards              the binding does not move
the same tap read twice                      the same answer, nothing repeats
===========================================  =================================

The first four are the outcomes the API must be able to tell apart; the last
two are the two ways a binding gets silently corrupted in the field -- a
pedestrian stepping in front of the camera, and an RC522 returning the same
UID for as long as the card sits on it.

Stdlib only for the service-level tests: no camera, no reader, no database,
no network. The route-level tests stub ``fastapi`` down to the two symbols the
routers touch and run the real handler bodies, the real event builders and the
real publisher against a recording transport.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from vision.episode_registry import EpisodeRegistry  # noqa: E402
from vision.rfid_binding import (  # noqa: E402
    RFIDBindingService,
    RFIDEvidenceZone,
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
AMBIGUITY_MARGIN = 0.20
DEPTH_MARGIN_M = 0.5
BIND_ECHO_S = 2.0

AT_READER = (250, 250, 350, 390)
AT_READER_LEFT = (205, 250, 300, 390)
AT_READER_RIGHT = (300, 250, 395, 390)

#: The card that is actually registered on the demo laptop, and the collector
#: it resolves to. Hard-coded on purpose: if the enrolment row is ever lost,
#: the physical demo fails and this name is the first place to look.
KNOWN_CARD = "69F04D05"
KNOWN_COLLECTOR = "GC001"
UNKNOWN_CARD = "DEADBEEF"

T0 = 1_700_000_000.0


def person(track_id, bbox, depth_m=None, confidence=0.92):
    return PersonDetection(
        track_id=track_id,
        bbox=bbox,
        confidence=confidence,
        depth_m=depth_m,
        depth_valid=depth_m is not None,
    )


class Step1BindingTests(unittest.TestCase):
    """The first tap, driven through the real binding service."""

    def setUp(self):
        self.registry = WorkerRegistry(grace_s=20.0)
        self.history = TrackHistory()
        self.episodes = EpisodeRegistry(max_age_s=180.0)
        self.service = RFIDBindingService(
            zone=ZONE,
            history=self.history,
            registry=self.registry,
            # Injected exactly as services.py injects database.collector_for_rfid.
            collector_lookup={KNOWN_CARD: KNOWN_COLLECTOR}.get,
            match_window_s=2.0,
            min_overlap=MIN_OVERLAP,
            ambiguity_margin=AMBIGUITY_MARGIN,
            depth_margin_m=DEPTH_MARGIN_M,
            episodes=self.episodes,
            retap_debounce_s=10.0,
            bind_echo_s=BIND_ECHO_S,
        )

    def see(self, detections, at):
        self.history.add(at, detections)
        self.registry.touch(
            [d.track_id for d in detections if d.track_id is not None], now=at
        )

    # -- 1. the happy path -------------------------------------------------

    def test_known_card_and_one_valid_depth_track_binds(self):
        """One collector visible, one card: BOUND, and the track is locked."""

        self.see([person(35, AT_READER, depth_m=1.2)], at=T0)

        result = self.service.handle_tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.BOUND)
        self.assertEqual(result["intent"], TapIntent.BIND)
        self.assertEqual(result["collector_id"], KNOWN_COLLECTOR)
        self.assertEqual(result["track_id"], 35)
        self.assertTrue(result["locked"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["selection_rule"], SelectionRule.DEPTH_IN_ZONE)
        self.assertGreater(result["confidence"], 0.0)

        # The registry -- not just the response -- now says so.
        self.assertTrue(self.registry.is_authorized(35, now=T0))
        self.assertEqual(self.registry.collector_for_track(35, now=T0), KNOWN_COLLECTOR)
        self.assertEqual(self.registry.active_track_ids(now=T0), [35])

    def test_the_further_person_is_not_the_tapper(self):
        """Two people at the reader, one clearly nearer: the nearer one taps."""

        self.see(
            [
                person(12, AT_READER_LEFT, depth_m=3.4),
                person(35, AT_READER_RIGHT, depth_m=1.1),
            ],
            at=T0,
        )

        result = self.service.handle_tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.BOUND)
        self.assertEqual(result["track_id"], 35)

    # -- 2. unknown card ---------------------------------------------------

    def test_unknown_card_fails_safely(self):
        """A tag nobody owns identifies nobody -- and never reaches the camera."""

        self.see([person(35, AT_READER, depth_m=1.2)], at=T0)

        result = self.service.handle_tap(UNKNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertIsNone(result["intent"])
        self.assertIsNone(result["collector_id"])
        self.assertIsNone(result["track_id"])
        self.assertEqual(self.registry.bindings(), [])
        self.assertFalse(self.registry.is_authorized(35, now=T0))

    def test_empty_card_fails_safely(self):
        result = self.service.handle_tap("", T0)
        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertEqual(self.registry.bindings(), [])

    # -- 3. no tracks ------------------------------------------------------

    def test_no_tracks_at_all_fails_safely(self):
        """Camera not running, or nobody in frame: identify nobody."""

        result = self.service.handle_tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.NO_TRACK_DATA)
        self.assertEqual(result["intent"], TapIntent.BIND)
        # The collector is known -- the card resolved. There is simply nobody
        # to attribute the tap to, and that distinction is what the dashboard
        # needs to show a useful message.
        self.assertEqual(result["collector_id"], KNOWN_COLLECTOR)
        self.assertIsNone(result["track_id"])
        self.assertFalse(result["locked"])
        self.assertEqual(self.registry.bindings(), [])

    def test_tracks_only_outside_the_match_window_fail_safely(self):
        """Somebody was seen, but nowhere near the tap instant."""

        self.see([person(35, AT_READER, depth_m=1.2)], at=T0 - 60.0)

        result = self.service.handle_tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.NO_TRACK_DATA)
        self.assertEqual(self.registry.bindings(), [])

    # -- 4. ambiguity ------------------------------------------------------

    def test_two_equally_close_tracks_are_ambiguous(self):
        """Shoulder to shoulder at the reader: refuse, do not guess."""

        self.see(
            [
                person(12, AT_READER_LEFT, depth_m=1.20),
                person(35, AT_READER_RIGHT, depth_m=1.35),  # 0.15 m < 0.5 m
            ],
            at=T0,
        )

        result = self.service.handle_tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.AMBIGUOUS)
        self.assertIsNone(result["track_id"])
        self.assertFalse(result["locked"])
        self.assertEqual(sorted(result["candidate_track_ids"]), [12, 35])
        self.assertEqual(self.registry.bindings(), [])
        # Neither of them is authorised. Guessing here is the exact failure
        # this subsystem exists to prevent.
        self.assertFalse(self.registry.is_authorized(12, now=T0))
        self.assertFalse(self.registry.is_authorized(35, now=T0))

    def test_the_configured_margin_is_what_decides_ambiguity(self):
        """Just over the margin binds; just under it does not."""

        service = self.service
        service.depth_margin_m = 0.5

        self.see(
            [
                person(12, AT_READER_LEFT, depth_m=1.9),
                person(35, AT_READER_RIGHT, depth_m=1.2),  # 0.7 m clear
            ],
            at=T0,
        )
        self.assertEqual(
            service.handle_tap(KNOWN_CARD, T0)["status"], BindingStatus.BOUND
        )

        # Same geometry, a wider margin: the same evidence is no longer enough.
        self.registry.clear()
        self.history.clear()
        service.depth_margin_m = 1.0
        self.see(
            [
                person(12, AT_READER_LEFT, depth_m=1.9),
                person(35, AT_READER_RIGHT, depth_m=1.2),
            ],
            at=T0,
        )
        self.assertEqual(
            service.handle_tap(KNOWN_CARD, T0)["status"], BindingStatus.AMBIGUOUS
        )

    # -- 5. the lock -------------------------------------------------------

    def test_a_nearer_person_later_does_not_take_the_binding(self):
        """The lock is the point. Proximity chooses once, not continuously."""

        self.see([person(35, AT_READER, depth_m=1.4)], at=T0)
        self.assertEqual(self.service.handle_tap(KNOWN_CARD, T0)["track_id"], 35)

        # A pedestrian now stands much closer to the camera, in the zone.
        later = T0 + 4.0
        self.see(
            [
                person(35, AT_READER_LEFT, depth_m=1.6),
                person(77, AT_READER_RIGHT, depth_m=0.4),
            ],
            at=later,
        )

        binding = self.registry.binding_for_collector(KNOWN_COLLECTOR, now=later)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.track_id, 35)
        self.assertFalse(self.registry.is_authorized(77, now=later))
        self.assertEqual(self.registry.active_track_ids(now=later), [35])

    def test_occlusion_grace_is_preserved(self):
        """Walking behind the vehicle for a few seconds is not a new identity."""

        self.see([person(35, AT_READER, depth_m=1.4)], at=T0)
        self.service.handle_tap(KNOWN_CARD, T0)

        # Unseen for less than the grace period: still bound.
        hidden = T0 + 15.0
        self.assertIsNotNone(
            self.registry.binding_for_collector(KNOWN_COLLECTOR, now=hidden)
        )
        self.assertEqual(self.registry.active_track_ids(now=hidden), [35])

        # Beyond it: released, and the collector may identify again.
        gone = T0 + 25.0
        self.assertIsNone(
            self.registry.binding_for_collector(KNOWN_COLLECTOR, now=gone)
        )

    # -- 6. the same tap, twice -------------------------------------------

    def test_duplicate_first_tap_is_idempotent(self):
        """The reader holds the UID; the bridge polls. One tap, several reads."""

        self.see([person(35, AT_READER, depth_m=1.3)], at=T0)
        first = self.service.handle_tap(KNOWN_CARD, T0)

        # A second read 400 ms later, with a nearer person now in frame -- the
        # worst case, because a re-selection would visibly move the binding.
        echo_at = T0 + 0.4
        self.see(
            [
                person(35, AT_READER_LEFT, depth_m=1.5),
                person(88, AT_READER_RIGHT, depth_m=0.3),
            ],
            at=echo_at,
        )
        second = self.service.handle_tap(KNOWN_CARD, echo_at)

        self.assertEqual(second["status"], BindingStatus.BOUND)
        self.assertEqual(second["intent"], TapIntent.BIND)
        self.assertTrue(second["duplicate"])
        self.assertTrue(second["locked"])

        # Same answer as the tap it echoes, field for field.
        for field in ("collector_id", "track_id", "session_id", "bound_at"):
            self.assertEqual(second[field], first[field], field)

        # And one binding, on the original track, not the nearer stranger.
        self.assertEqual(len(self.registry.bindings()), 1)
        self.assertEqual(
            self.registry.binding_for_collector(KNOWN_COLLECTOR, now=echo_at).track_id,
            35,
        )
        self.assertFalse(self.registry.is_authorized(88, now=echo_at))

    def test_an_out_of_order_duplicate_is_still_the_same_tap(self):
        """Two reads of one tap can reach the API in either order."""

        self.see([person(35, AT_READER, depth_m=1.3)], at=T0)
        self.service.handle_tap(KNOWN_CARD, T0)

        early = self.service.handle_tap(KNOWN_CARD, T0 - 0.3)

        self.assertEqual(early["status"], BindingStatus.BOUND)
        self.assertTrue(early["duplicate"])
        self.assertEqual(early["track_id"], 35)

    def test_a_duplicate_read_never_triggers_non_segregation(self):
        """Even with a house open, an echo of the binding tap flags nothing.

        This is the failure the echo window exists to prevent: WASTRAQ pushes
        an episode the moment the collector reaches a bin, and a reader that
        bounces would otherwise mark that house NOT_SEGREGATED on its own.
        """

        self.see([person(35, AT_READER, depth_m=1.3)], at=T0)
        self.service.handle_tap(KNOWN_CARD, T0)

        self.episodes.open(
            episode_id="EP-HOUSE-1",
            track_id=35,
            session_id=self.registry.session_id,
            association_status="AUTO_ASSOCIATED",
            collector_id=KNOWN_COLLECTOR,
            now=T0 + 0.2,
        )

        echo = self.service.handle_tap(KNOWN_CARD, T0 + 0.5)

        self.assertEqual(echo["status"], BindingStatus.BOUND)
        self.assertIsNone(echo["trigger_id"])
        self.assertFalse(self.episodes.active_for_track(35, now=T0 + 0.5).non_segregated)

    def test_a_deliberate_second_tap_still_means_non_segregation(self):
        """STEP 2 behaviour is preserved, not disabled -- only debounced.

        Included here to prove the echo window narrows nothing beyond a
        bouncing reader: past it, the existing meaning of a second tap is
        exactly as it was.
        """

        self.see([person(35, AT_READER, depth_m=1.3)], at=T0)
        self.service.handle_tap(KNOWN_CARD, T0)

        self.episodes.open(
            episode_id="EP-HOUSE-1",
            track_id=35,
            session_id=self.registry.session_id,
            association_status="AUTO_ASSOCIATED",
            collector_id=KNOWN_COLLECTOR,
            now=T0 + 5.0,
        )
        self.registry.touch([35], now=T0 + 6.0)

        result = self.service.handle_tap(KNOWN_CARD, T0 + 6.0)

        self.assertEqual(result["intent"], TapIntent.NON_SEGREGATION)
        self.assertEqual(result["status"], BindingStatus.NON_SEGREGATION)
        self.assertEqual(result["track_id"], 35)


# ---------------------------------------------------------------------------
# Route level: the two events STEP 1 owes WASTRAQ
# ---------------------------------------------------------------------------


class _StubRouter:
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


class _StubResponse:
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
    stub.__path__ = []
    stub.APIRouter = _StubRouter
    stub.HTTPException = _StubHTTPException
    sys.modules["fastapi"] = stub

    responses = types.ModuleType("fastapi.responses")
    responses.StreamingResponse = _StubResponse
    # routes/evidence.py serves clips with FileResponse.
    responses.FileResponse = _StubResponse
    responses.JSONResponse = _StubResponse
    responses.Response = _StubResponse
    stub.responses = responses
    sys.modules["fastapi.responses"] = responses
    return True


_STUBBED_FASTAPI = _install_fastapi_stub()

import database  # noqa: E402

ASSIGNMENTS = {KNOWN_CARD: KNOWN_COLLECTOR}
database.collector_for_rfid = ASSIGNMENTS.get

import services  # noqa: E402
from integration.client import WastraqClient  # noqa: E402
from integration.events import (  # noqa: E402
    EVENT_RFID_TAP,
    EVENT_WORKER_TRACK_BOUND,
)
from routes import rfid as rfid_route  # noqa: E402
from schemas import RFIDEventIn  # noqa: E402

if _STUBBED_FASTAPI:
    # The routers never touch fastapi again. Leaving the stub in sys.modules
    # would make other test modules believe FastAPI is installed.
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.responses", None)


class _RecordingTransport:
    def __init__(self):
        self.sent = []

    def post_json(self, url, payload):
        self.sent.append(payload)
        return 202


class Step1EventTests(unittest.TestCase):
    """RFID_TAP and WORKER_TRACK_BOUND, through the real route handler."""

    def setUp(self):
        services.rfid_service.collector_lookup = ASSIGNMENTS.get
        services.rfid_service.zone = services.RFIDEvidenceZone(200, 200, 400, 400)
        services.rfid_service.bind_echo_s = BIND_ECHO_S
        services.rfid_service.episodes = services.episode_registry

        services.worker_registry.clear()
        services.episode_registry.clear()
        services.track_history.clear()

        self.transport = _RecordingTransport()
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

    def see(self, detections, at):
        services.track_history.add(at, detections)
        services.worker_registry.touch(
            [d.track_id for d in detections if d.track_id is not None], now=at
        )

    def tap(self, uid, at):
        return rfid_route.rfid_event(RFIDEventIn(rfid_id=uid, timestamp=at))

    def events_of(self, event_type):
        return [e for e in self.transport.sent if e["event_type"] == event_type]

    # ---------------------------------------------------------------------

    def test_a_binding_tap_emits_both_events(self):
        self.see([person(35, AT_READER, depth_m=1.2)], at=T0)

        result = self.tap(KNOWN_CARD, T0)
        self.assertEqual(result["status"], BindingStatus.BOUND)

        taps = self.events_of(EVENT_RFID_TAP)
        bounds = self.events_of(EVENT_WORKER_TRACK_BOUND)
        self.assertEqual(len(taps), 1)
        self.assertEqual(len(bounds), 1)

        self.assertEqual(taps[0]["rfid_uid"], KNOWN_CARD)
        self.assertEqual(taps[0]["collector_id"], KNOWN_COLLECTOR)
        self.assertEqual(taps[0]["track_id"], 35)
        self.assertEqual(taps[0]["binding_status"], BindingStatus.BOUND)

        self.assertEqual(bounds[0]["collector_id"], KNOWN_COLLECTOR)
        self.assertEqual(bounds[0]["track_id"], 35)
        self.assertEqual(bounds[0]["rfid_event_id"], taps[0]["event_id"])
        self.assertEqual(bounds[0]["session_id"], services.worker_registry.session_id)

    def test_an_unresolved_tap_still_reaches_wastraq(self):
        """Ambiguity is data WASTRAQ needs, not a problem to hide locally."""

        self.see(
            [
                person(12, AT_READER_LEFT, depth_m=1.20),
                person(35, AT_READER_RIGHT, depth_m=1.30),
            ],
            at=T0,
        )

        result = self.tap(KNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.AMBIGUOUS)
        taps = self.events_of(EVENT_RFID_TAP)
        self.assertEqual(len(taps), 1)
        self.assertIsNone(taps[0]["track_id"])
        self.assertEqual(sorted(taps[0]["candidate_track_ids"]), [12, 35])
        # Nothing was bound, so nothing announces a binding.
        self.assertEqual(self.events_of(EVENT_WORKER_TRACK_BOUND), [])

    def test_an_unknown_card_announces_no_binding(self):
        self.see([person(35, AT_READER, depth_m=1.2)], at=T0)

        result = self.tap(UNKNOWN_CARD, T0)

        self.assertEqual(result["status"], BindingStatus.UNKNOWN_RFID)
        self.assertEqual(len(self.events_of(EVENT_RFID_TAP)), 1)
        self.assertEqual(self.events_of(EVENT_WORKER_TRACK_BOUND), [])
        self.assertEqual(self.clip_requests, [])

    def test_a_duplicate_first_tap_announces_one_binding_and_one_clip(self):
        """The echo is reported, but nothing downstream of it runs twice."""

        self.see([person(35, AT_READER, depth_m=1.2)], at=T0)
        first = self.tap(KNOWN_CARD, T0)
        second = self.tap(KNOWN_CARD, T0 + 0.4)

        self.assertEqual(first["status"], BindingStatus.BOUND)
        self.assertEqual(second["status"], BindingStatus.BOUND)
        self.assertTrue(second["duplicate"])

        # Both reads are reported -- the reader really did fire twice.
        self.assertEqual(len(self.events_of(EVENT_RFID_TAP)), 2)
        # But the binding was made once, and evidenced once.
        self.assertEqual(len(self.events_of(EVENT_WORKER_TRACK_BOUND)), 1)
        self.assertLessEqual(len(self.clip_requests), 1)
        self.assertEqual(len(services.worker_registry.bindings()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
