#!/usr/bin/env python3
"""
The Live collection-state panel, proved without a camera, a Windows laptop,
a database or a network socket.

    python3 scripts/test_live_panel.py

What is under test is `backend/app/routes/live_state.py`: the pure assembly
function that turns "what the episode engine knows" plus "what the GeoVision
edge answered" into the single payload the dashboard panel renders, and the
EdgeReader that talks to Windows.

Fourteen claims, in the order they matter on demo day:

     1  a full live state - bound collector, live episode - assembles
     2  the property on the panel comes from WASTRAQ, never from the edge
     3  a property_id planted in an edge payload cannot reach the response
     4  no Windows filesystem path can reach the response
     5  GeoVision offline still answers, with the failure as a FIELD
     6  no picker in frame is a state, not an error
     7  no binding is a state, not an error
     8  no episode is a state, and a dwell candidate is shown instead
     9  a closed episode is reported as CLOSED with its property
    10  the bound track wins over an unbound one with higher confidence
    11  WASTRAQ's own ingested tracks stand in when the edge is unreachable
    12  evidence counts follow the same media states the modal renders
    13  a pending clip is PENDING and offers no play action
    14  the EdgeReader caches, backs off, and never raises

stdlib only. Nothing here writes anything, anywhere.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The module reads settings at import; nothing else is touched.
os.environ.setdefault("GEOVISION_EDGE_BASE_URL", "http://10.0.0.1:8000")


def _load_live_state():
    """Import `app.routes.live_state`, with or without the project venv.

    Inside the venv the ordinary import is used and this is a no-op wrapper.
    On a bare interpreter two things are stood in for, and only two: the
    FastAPI symbols `live_state` decorates its routes with, and dotenv's
    `load_dotenv`. Neither is exercised by anything under test.

    The sibling modules in `app.routes.__init__` need far more than that
    (pydantic, psycopg, the database pool), so on the stub path the module
    is loaded from its own file under a placeholder `app.routes` package.
    `from ..config import settings` still resolves to the real config; what
    is skipped is only the package __init__ that imports five unrelated
    routers.
    """
    try:
        from app.routes import live_state as module
        return module, []
    except (ModuleNotFoundError, ImportError):
        pass

    import importlib.util
    import types

    stubbed = []
    if "fastapi" not in sys.modules:
        m = types.ModuleType("fastapi")

        class _Router:
            def __init__(self, **kw):
                self.kw = kw

            def get(self, *a, **k):
                return lambda fn: fn

        m.APIRouter = _Router
        m.Query = lambda default=None, **kw: default
        sys.modules["fastapi"] = m
        stubbed.append("fastapi")
    try:
        import dotenv  # noqa: F401
    except ModuleNotFoundError:
        d = types.ModuleType("dotenv")
        d.load_dotenv = lambda *a, **k: False
        sys.modules["dotenv"] = d
        stubbed.append("dotenv")

    routes_dir = ROOT / "backend" / "app" / "routes"
    placeholder = types.ModuleType("app.routes")
    placeholder.__path__ = [str(routes_dir)]
    sys.modules["app.routes"] = placeholder
    stubbed.append("app.routes")

    spec = importlib.util.spec_from_file_location(
        "app.routes.live_state", routes_dir / "live_state.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.routes.live_state"] = module
    spec.loader.exec_module(module)
    return module, stubbed


LS, _STUBBED = _load_live_state()
if _STUBBED:
    print(f"(stood in for: {', '.join(_STUBBED)} - no venv on this interpreter)")

PASS = FAIL = 0


def ok(claim: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {claim}")
    else:
        FAIL += 1
        print(f"  FAIL {claim}" + (f"  <- {detail}" if detail else ""))


def head(n: str) -> None:
    print(f"\n{n}\n" + "-" * len(n))


NOW = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)

ENGINE_FULL = {
    "enabled": True,
    "camera_configured": True,
    "camera": {"latitude": 12.2943159, "longitude": 76.6415212, "heading_deg": 0},
    "dwell_s": 3.0,
    "bindings": [{"collector_id": "GC001", "track_id": 214, "source_id": "GEOVISION-D455-01",
                  "session_id": "SES-1", "rfid_uid": "69F04D05", "bound_at": "2026-08-30T05:59:00Z"}],
    "candidates": [],
    "active_episodes": [{"collector_id": "GC001", "episode_id": "EP-0007",
                         "property_id": "PROP-005", "track_id": 214,
                         "segregation_status": "SEGREGATED",
                         "association_status": "AUTO_ASSOCIATED",
                         "started_at": "2026-08-30T05:59:30Z",
                         "last_inside": "2026-08-30T06:00:00Z", "observations": 37}],
    "stats": {},
}

EDGE_TRACKS = {"tracks": [
    {"id": 214, "track_id": 214, "confidence": 0.91, "depth_m": 2.413,
     "depth_valid": True, "depth_status": "OK", "is_authorized_picker": True,
     "collector_id": "GC001", "identity_confidence": 0.88,
     "source_id": "GEOVISION-D455-01", "session_id": "SES-1"},
    {"id": 219, "track_id": 219, "confidence": 0.97, "depth_m": 5.2,
     "depth_valid": True, "is_authorized_picker": False},
]}

EDGE_BINDINGS = {"bindings": [
    {"collector_id": "GC001", "collector_name": "Ramesh", "track_id": 214,
     "rfid_uid": "69F04D05", "session_id": "SES-1", "locked": True,
     "confidence": 0.88, "selection_rule": "DEPTH_IN_ZONE", "status": "BOUND",
     "bound_at": "2026-08-30T05:59:00Z"},
]}

DB_ACTIVE = [{"episode_id": "EP-0007", "property_id": "PROP-005",
              "association_confidence": 0.953, "house_number": "5",
              "collection_event_id": None}]

EVIDENCE_ROWS = [
    {"evidence_id": "EV-1", "media_status": "AVAILABLE", "is_placeholder": False},
    {"evidence_id": "EV-2", "media_status": "AVAILABLE", "is_placeholder": False},
    {"evidence_id": "EV-3", "media_status": "PENDING", "is_placeholder": False},
    {"evidence_id": "EV-4", "media_status": "NONE", "is_placeholder": True},
]


def build(**kw):
    base = dict(engine=ENGINE_FULL, edge_tracks=EDGE_TRACKS,
                edge_bindings=EDGE_BINDINGS, db_active_episodes=DB_ACTIVE,
                collector_names={"GC001": "Ramesh"}, now=NOW)
    base.update(kw)
    return LS.build_live_state(**base)


def strings(obj):
    """Every string anywhere in the payload."""
    out = []
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.keys()); stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        elif isinstance(v, str):
            out.append(v)
    return out


# --- 1-4: the happy path, and what may not be in it --------------------------
head("1-4  a full live state, and the two things that may never be in one")
s = build()
t, b, e, ev, h = s["tracking"], s["binding"], s["episode"], s["evidence"], s["health"]

ok("tracking reports the bound track", t["track_id"] == 214, repr(t["track_id"]))
ok("tracking reports the collector by name", t["collector_name"] == "Ramesh")
ok("tracking reports the RFID uid", t["rfid_uid"] == "69F04D05")
ok("tracking reports depth", t["depth_m"] == 2.413 and t["depth_valid"] is True)
ok("tracking reports AUTHORIZED", t["authorization_state"] == "AUTHORIZED")
ok("binding is BOUND, locked, with a confidence",
   b["bound"] and b["locked"] is True and b["identity_confidence"] == 0.88)
ok("binding names collector -> track", b["collector_id"] == "GC001" and b["track_id"] == 214)
ok("episode is ACTIVE on PROP-005", e["state"] == "ACTIVE" and e["property_id"] == "PROP-005")
ok("episode carries segregation + association",
   e["segregation_status"] == "SEGREGATED" and e["association_status"] == "AUTO_ASSOCIATED")
ok("episode carries association confidence from the DB row",
   e["association_confidence"] == 0.953)
ok("episode carries the observation count", e["observations"] == 37)
ok("health says connected / engine on / camera configured",
   h["geovision_connected"] and h["episode_engine_enabled"] and h["camera_configured"])

poisoned_tracks = json.loads(json.dumps(EDGE_TRACKS))
poisoned_tracks["tracks"][0]["property_id"] = "PROP-999"
poisoned_tracks["tracks"][0]["segregation_status"] = "NOT_SEGREGATED"
poisoned_tracks["tracks"][0]["file_path"] = r"C:\Users\ryura\Desktop\GeoVision\clips\x.mp4"
poisoned_tracks["tracks"][0]["clip"] = r"\\ryu\share\evidence\x.mp4"
clean = LS._sanitize(poisoned_tracks)
s2 = build(edge_tracks=clean)
allstr = strings(s2)

ok("the panel's property is WASTRAQ's, not the edge's",
   s2["episode"]["property_id"] == "PROP-005")
ok("a property_id planted on an edge track never reaches the response",
   "PROP-999" not in allstr)
ok("a segregation verdict planted on an edge track is dropped",
   "property_id" not in json.dumps(clean) and "segregation_status" not in json.dumps(clean))
ok("no drive-letter path reaches the response",
   not any(":\\" in x for x in allstr), [x for x in allstr if ":\\" in x])
ok("no UNC path reaches the response",
   not any(x.startswith("\\\\") for x in allstr))
ok("the response states who decided the property",
   s2["authority"]["property_decided_by"].startswith("WASTRAQ PostGIS"))

# --- 5-9: every degraded state the demo can land in --------------------------
head("5-9  offline, no picker, no binding, no episode, closed episode")

off = build(edge_tracks=None, edge_bindings=None,
            edge_error="URLError: [Errno 61] Connection refused")
ok("GeoVision offline still answers", isinstance(off, dict))
ok("offline is a field, not an exception",
   off["health"]["geovision_connected"] is False)
ok("the reason is carried", "Connection refused" in off["health"]["geovision_error"])
ok("WASTRAQ's own episode survives the edge being down",
   off["episode"]["state"] == "ACTIVE" and off["episode"]["property_id"] == "PROP-005")

empty_engine = {"enabled": True, "camera_configured": True, "dwell_s": 3.0,
                "bindings": [], "candidates": [], "active_episodes": []}
none = build(engine=empty_engine, edge_tracks={"tracks": []},
             edge_bindings={"bindings": []}, db_active_episodes=[],
             collector_names={})
ok("no picker in frame is a state", none["tracking"]["available"] is False)
ok("no picker reports NO_PICKER", none["tracking"]["authorization_state"] == "NO_PICKER")
ok("no binding is a state", none["binding"]["bound"] is False)
ok("no episode is a state", none["episode"]["state"] == "NONE")
ok("no episode has no property", none["episode"]["property_id"] is None)
ok("no episode has no candidate", none["episode"]["candidate"] is None)

dwelling = dict(empty_engine)
dwelling = {**empty_engine,
            "bindings": ENGINE_FULL["bindings"],
            "candidates": [{"collector_id": "GC001", "property_id": "PROP-005",
                            "dwell_s": 1.8, "confidence": 0.94}]}
d = build(engine=dwelling, db_active_episodes=[])
ok("dwelling before the episode opens is shown", d["episode"]["state"] == "NONE")
ok("the dwell candidate names the property", d["episode"]["candidate"]["property_id"] == "PROP-005")
ok("the dwell shows progress toward the threshold",
   d["episode"]["candidate"]["dwell_s"] == 1.8
   and d["episode"]["candidate"]["dwell_required_s"] == 3.0)

closed = build(engine=empty_engine, db_active_episodes=[], last_episode={
    "episode_id": "EP-0006", "property_id": "PROP-004", "state": "CLOSED",
    "segregation_status": "NOT_SEGREGATED", "association_status": "AUTO_ASSOCIATED",
    "association_confidence": 0.912, "observations": 41, "track_id": 211,
    "house_number": "4", "collection_event_id": "EVENT-0031",
    "started_at": "2026-08-30T05:50:00Z", "ended_at": "2026-08-30T05:52:00Z",
})
ok("a closed episode is reported CLOSED", closed["episode"]["state"] == "CLOSED")
ok("a closed episode keeps its property", closed["episode"]["property_id"] == "PROP-004")
ok("a closed episode keeps NOT_SEGREGATED",
   closed["episode"]["segregation_status"] == "NOT_SEGREGATED")

# --- 10-11: which track, and where it came from ------------------------------
head("10-11  the bound track wins; ingest stands in when the edge is down")

s3 = build()
ok("the bound track wins over a higher-confidence stranger",
   s3["tracking"]["track_id"] == 214 and s3["tracking"]["source"] == "GEOVISION_EDGE")

unbound = build(engine={**ENGINE_FULL, "bindings": [], "active_episodes": []},
                edge_bindings={"bindings": []}, db_active_episodes=[])
ok("with no binding the edge's authorised track is chosen",
   unbound["tracking"]["track_id"] == 214)

fallback = build(edge_tracks=None, edge_bindings=None, edge_error="offline",
                 ingest_tracks=[{"track_id": 214, "collector_id": "GC001",
                                 "is_authorized_picker": True, "depth_m": 2.4,
                                 "depth_valid": True, "age_s": 0.6}])
ok("WASTRAQ's own ingested track stands in",
   fallback["tracking"]["available"] and fallback["tracking"]["track_id"] == 214)
ok("and the panel says where that came from",
   fallback["tracking"]["source"] == "WASTRAQ_INGEST")

# --- 12-13: evidence -----------------------------------------------------
head("12-13  evidence counts, and the pending clip")

with_ev = build(fallback_event_id="EVENT-0031",
                evidence_for_event=lambda _e: EVIDENCE_ROWS)
ev = with_ev["evidence"]
ok("the evidence block names the event", ev["event_id"] == "EVENT-0031")
ok("placeholders are excluded from the count", ev["evidence_count"] == 3)
ok("placeholders are counted separately", ev["placeholder_count"] == 1)
ok("playable clips are counted", ev["playable_count"] == 2)
ok("pending clips are counted", ev["pending_count"] == 1)
ok("AVAILABLE wins the aggregate status", ev["media_status"] == "AVAILABLE")
ok("a playable clip offers an evidence id", ev["evidence_id"] == "EV-1")
ok("nothing in the evidence block is a media URL or a path",
   not any(("http" in str(v)) or ("\\" in str(v)) or ("/" in str(v))
           for v in ev.values() if isinstance(v, str)))

pending_only = build(fallback_event_id="EVENT-0032", evidence_for_event=lambda _e: [
    {"evidence_id": "EV-9", "media_status": "PENDING", "is_placeholder": False}])
ok("a clip still being fetched reports PENDING",
   pending_only["evidence"]["media_status"] == "PENDING")
ok("PENDING offers nothing to play", pending_only["evidence"]["playable"] is False)

unavail = build(fallback_event_id="EVENT-0033", evidence_for_event=lambda _e: [
    {"evidence_id": "EV-9", "media_status": "UNAVAILABLE", "is_placeholder": False}])
ok("a clip the edge could not deliver reports UNAVAILABLE",
   unavail["evidence"]["media_status"] == "UNAVAILABLE")

no_ev = build(fallback_event_id=None, evidence_for_event=lambda _e: [])
ok("no event means no evidence claim",
   no_ev["evidence"]["event_id"] is None and no_ev["evidence"]["media_status"] == "NONE")

broken = build(fallback_event_id="EVENT-0034",
               evidence_for_event=lambda _e: (_ for _ in ()).throw(RuntimeError("db down")))
ok("an evidence lookup that raises does not take the panel down",
   broken["evidence"]["media_status"] == "NONE" and "error" in broken["evidence"])

# --- 14: the edge reader -----------------------------------------------------
head("14  the edge reader caches, backs off and never raises")

calls = {"n": 0}
clock = {"t": 100.0}


def counting_transport(url, timeout):
    calls["n"] += 1
    return 200, {"tracks": [{"id": 1}]}


r = LS.EdgeReader("http://10.0.0.1:8000", transport=counting_transport,
                  clock=lambda: clock["t"])
p1, e1 = r.read("/tracks")
p2, e2 = r.read("/tracks")
ok("a second read inside the TTL is served from cache", calls["n"] == 1, str(calls["n"]))
ok("both reads answer the same payload", p1 == p2 and e1 is None and e2 is None)
clock["t"] += 5.0
r.read("/tracks")
ok("a read after the TTL dials again", calls["n"] == 2, str(calls["n"]))
ok("last_ok_age_s is reported", r.last_ok_age_s() == 0.0)


def refusing_transport(url, timeout):
    calls["n"] += 1
    raise ConnectionRefusedError("Connection refused")


calls["n"] = 0
r2 = LS.EdgeReader("http://10.0.0.1:8000", transport=refusing_transport,
                   clock=lambda: clock["t"])
p, err = r2.read("/tracks")
ok("a refused connection does not raise", p is None and err is not None)
ok("the error names the cause", "Connection refused" in err)
r2.read("/tracks")
ok("a failure is not retried inside the back-off", calls["n"] == 1, str(calls["n"]))
clock["t"] += 10.0
r2.read("/tracks")
ok("after the back-off it tries again", calls["n"] == 2, str(calls["n"]))

r3 = LS.EdgeReader("", transport=counting_transport)
p, err = r3.read("/tracks")
ok("no edge configured is a message, not a crash",
   p is None and "GEOVISION_EDGE_BASE_URL" in err)


def path_transport(url, timeout):
    return 200, {"tracks": [{"id": 1, "file_path": r"C:\GeoVision\clips\a.mp4",
                             "property_id": "PROP-999"}]}


r4 = LS.EdgeReader("http://10.0.0.1:8000", transport=path_transport)
p, err = r4.read("/tracks")
ok("the reader scrubs paths and property authority before anyone sees them",
   "PROP-999" not in json.dumps(p) and "C:" not in json.dumps(p), json.dumps(p))

# --- optional: the same claims, over the wire --------------------------------
# Everything above runs with no backend. This runs against a live one, so the
# panel can be proved on demo morning against whatever the lane is actually
# doing:
#
#     python3 scripts/test_live_panel.py --http
#     python3 scripts/test_live_panel.py --http http://10.235.18.184:8000
#
# Read-only. It issues GETs and nothing else.
if "--http" in sys.argv:
    import urllib.request

    i = sys.argv.index("--http")
    base = (sys.argv[i + 1] if len(sys.argv) > i + 1
            and not sys.argv[i + 1].startswith("-") else "http://127.0.0.1:8000")
    head(f"live  GET {base}/live/state")
    try:
        with urllib.request.urlopen(base + "/live/state", timeout=8) as r:
            live = json.loads(r.read())
        ok("the endpoint answers 200", r.status == 200)
        ok("it carries all five blocks",
           all(k in live for k in ("tracking", "binding", "episode",
                                   "evidence", "health")),
           ", ".join(sorted(live)))
        allstr = strings(live)
        ok("nothing in the live answer is a drive-letter path",
           not any(":\\" in x for x in allstr))
        ok("nothing in the live answer is a UNC path",
           not any(x.startswith("\\\\") for x in allstr))
        h = live["health"]
        print(f"     GeoVision: {'connected' if h['geovision_connected'] else 'DISCONNECTED'}"
              f"{'' if h['geovision_connected'] else '  (' + str(h['geovision_error']) + ')'}")
        print(f"     engine: enabled={h['episode_engine_enabled']} "
              f"camera_configured={h['camera_configured']}")
        print(f"     tracking: {live['tracking']['authorization_state']} "
              f"track={live['tracking']['track_id']} "
              f"collector={live['tracking']['collector_name'] or live['tracking']['collector_id']}")
        print(f"     binding: bound={live['binding']['bound']} "
              f"track={live['binding']['track_id']}")
        print(f"     episode: {live['episode']['state']} "
              f"{live['episode']['property_id']} {live['episode']['segregation_status']}")
        print(f"     evidence: {live['evidence']['media_status']} "
              f"event={live['evidence']['event_id']} "
              f"playable={live['evidence']['playable_count']}")
        with urllib.request.urlopen(base + "/dashboard", timeout=8) as r2:
            page = r2.read().decode("utf-8", "replace")
        ok("GET /dashboard still serves the operations dashboard", r2.status == 200)
        ok("the Live panel is on it", 'id="liveCard"' in page)
        ok("the existing map, feed and evidence modal are still on it",
           all(x in page for x in ('id="opsmap"', 'id="feed"', 'id="evm"',
                                   'id="propRows"', 'id="kpis"')))
        ok("the page still points at no Windows host", "10.235.18.118" not in page)
    except Exception as exc:  # noqa: BLE001
        ok(f"the backend at {base} is reachable", False, repr(exc))

print(f"\n{'=' * 60}\n{PASS} passed, {FAIL} failed\n{'=' * 60}")
sys.exit(1 if FAIL else 0)
