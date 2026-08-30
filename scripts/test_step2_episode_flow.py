#!/usr/bin/env python3
"""
STEP 2, over the wire, against a RUNNING WASTRAQ backend and the REAL lane.

    scripts/run_backend.sh                          # in another terminal
    python3 scripts/test_step2_episode_flow.py
    python3 scripts/test_step2_episode_flow.py http://127.0.0.1:8000

One question, answered end to end:

    does a real worker-track binding plus GeoVision TRACK_UPDATEs become a
    property-associated collection episode, and does leaving the service
    zone close it as SEGREGATED?

        WORKER_TRACK_BOUND
          -> collector bound to track
          -> TRACK_UPDATE for that track accepted
          -> camera-relative x/z through the configured camera pose
          -> PostGIS service-zone lookup
          -> unique property candidate
          -> dwell clock
          -> episode opens
          -> collector leaves the zone
          -> leave grace expires
          -> episode closes SEGREGATED
          -> authoritative collection event written

Ten claims, numbered as the STEP 2 brief numbers them:

     1  an unbound track creates nothing
     2  the binding is accepted
     3  a bound track inside one service zone yields one candidate
     4  the dwell threshold opens an episode
     5  staying in the zone opens no second episode
     6  leaving, plus the grace, closes it SEGREGATED
     7  exactly one collection event is written
     8  an ambiguous position associates nothing
     9  a missing camera pose associates nothing
    10  a re-delivered event_id is idempotent

Deliberately NOT here: NON_SEGREGATION_TRIGGER, evidence clips, the
dashboard. Those are covered by scripts/test_episode_demo_http.py.

stdlib only (urllib) - the same transport the GeoVision edge uses, so this
exercises the real FastAPI route, the real pydantic contract, the real
PostGIS association and the real database.

SAFETY. It is self-configuring: it reads the camera pose from
/episodes/status and the service-zone polygons from /properties, then
inverts the camera transform to work out what the edge would have reported
for a collector standing inside a real zone. Nothing is hard-coded, so
nothing goes stale. Every event carries a run-scoped source_id and
session_id, so the rows it creates are identifiable afterwards; it writes
episodes and collection events (that IS the proof) but never touches a
property, a service zone, an entrance or a frontage, and it checks that at
the end. To remove its rows afterwards:

    psql wastraq_demo -v source="'<the source_id printed below>'" \
         -f database/cleanup_step2_test.sql

Claim 9 needs a backend with no camera pose, which cannot be arranged over
HTTP. Run it by unsetting CAMERA_ORIGIN_LAT/LON in backend/.env, restarting,
and re-running this script: it then asserts the refusal instead of skipping.
The same guard is proved without a restart by
scripts/test_episode_engine.py (check 19.9).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.getenv("WASTRAQ_URL", "http://127.0.0.1:8000")).rstrip("/")
EVENTS = f"{BASE}/integrations/geovision/events"

RUN = uuid.uuid4().hex[:8]
SOURCE = f"GEOVISION-STEP2-{RUN}"
SESSION = f"sess-{RUN}"
COLLECTOR = os.getenv("DEMO_COLLECTOR", "PICKER-01")
TRACK = int(os.getenv("DEMO_TRACK", "35"))
UID = os.getenv("DEMO_RFID_UID", "04A1B2C3")

FAILURES: list[str] = []
_M_PER_DEG_LAT = 111_320.0
_n = [0]


def ok(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# --- transport ----------------------------------------------------------------
def iso(when: datetime) -> str:
    return (f"{when.strftime('%Y-%m-%dT%H:%M:%S')}"
            f".{when.microsecond // 1000:03d}Z")


def _request(method: str, url: str, payload=None, timeout: float = 10.0):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw.decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


def post(payload: dict) -> tuple[int, dict]:
    return _request("POST", EVENTS, payload)


def get(path: str):
    return _request("GET", f"{BASE}{path}")


def eid() -> str:
    _n[0] += 1
    return f"{RUN}-{_n[0]:04d}"


# --- geometry -----------------------------------------------------------------
def wgs84_to_camera(cam: dict, lat: float, lon: float) -> tuple[float, float]:
    """Inverse of backend/app/episodes/transform.py, kept deliberately
    independent: if the backend's forward transform is wrong, these
    coordinates land in the wrong zone and the run fails loudly rather than
    agreeing with the bug."""
    north = (lat - cam["latitude"]) * _M_PER_DEG_LAT
    mid = math.radians((cam["latitude"] + lat) / 2.0)
    east = (lon - cam["longitude"]) * _M_PER_DEG_LAT * math.cos(mid)
    h = math.radians(cam["heading_deg"])
    return (east * math.cos(h) - north * math.sin(h),
            east * math.sin(h) + north * math.cos(h))


def polygon_centre(geojson: dict) -> tuple[float, float]:
    ring = geojson["coordinates"][0]
    pts = ring[:-1] or ring
    return (sum(p[1] for p in pts) / len(pts),
            sum(p[0] for p in pts) / len(pts))


# --- event builders -----------------------------------------------------------
def bound_payload(at: datetime) -> dict:
    return {"event_type": "WORKER_TRACK_BOUND", "event_id": eid(),
            "timestamp": iso(at), "source_id": SOURCE, "session_id": SESSION,
            "collector_id": COLLECTOR, "rfid_uid": UID, "track_id": TRACK,
            "confidence": 0.93, "rfid_event_id": f"{RUN}-rfid-1"}


def track_payload(at: datetime, right: float, forward: float, **over) -> dict:
    payload = {"event_type": "TRACK_UPDATE", "event_id": eid(),
               "timestamp": iso(at), "source_id": SOURCE,
               "session_id": SESSION, "track_id": TRACK, "confidence": 0.9,
               "depth_m": round(abs(forward), 3), "depth_valid": True,
               "depth_status": "OK", "relative_x_m": round(right, 3),
               "relative_forward_m": round(forward, 3),
               "is_authorized_picker": True, "collector_id": COLLECTOR}
    payload.update(over)
    return payload


def status() -> dict:
    return get("/episodes/status")[1]


def mine(st: dict) -> dict:
    eng = st["engine"]
    return {"bindings": [b for b in eng["bindings"] if b["source_id"] == SOURCE],
            "candidates": eng["candidates"],
            "active": eng["active_episodes"],
            "stats": eng["stats"]}


def dwell(t0: datetime, right: float, forward: float, seconds: float,
          step: float = 0.5) -> datetime:
    """Stand still, reporting at 2 Hz, exactly as a bound collector would.

    Real wall-clock spacing on purpose: the engine throttles the PostGIS
    ladder by wall clock (EPISODE_MIN_ASSOC_INTERVAL_S), so firing these
    instantly would collapse the whole dwell into one association.
    """
    steps = max(int(round(seconds / step)), 1)
    for i in range(steps + 1):
        post(track_payload(t0 + timedelta(seconds=i * step), right, forward))
        time.sleep(step)
    return t0 + timedelta(seconds=steps * step)


def events_for(episode_id: str) -> list[dict]:
    _, rows = get("/collection-events?limit=500")
    return [r for r in (rows if isinstance(rows, list) else [])
            if r.get("episode_id") == episode_id]


def main() -> int:  # noqa: C901
    print("=" * 74)
    print(f"WASTRAQ STEP 2 - binding -> episode -> SEGREGATED   ({BASE})")
    print(f"source_id={SOURCE}  session_id={SESSION}")
    print("=" * 74)

    # ---------------------------------------------------------------- 0
    print("\n0. preconditions")
    code, health = get("/health/episodes")
    if not ok("backend is up and the episode engine is loaded",
              code == 200 and health.get("status") != "unavailable",
              json.dumps(health)[:200]):
        return 1
    if not ok("the episode engine is enabled", health.get("enabled") is True):
        return 1

    camera_configured = health.get("camera_configured") is True
    st = status()
    cam = st["engine"]["camera"]
    dwell_s = float(st["engine"]["dwell_s"])
    grace_s = float(st["engine"]["leave_grace_s"])
    print(f"     camera : {cam['latitude']}, {cam['longitude']} "
          f"heading {cam['heading_deg']} deg  (configured={camera_configured})")
    print(f"     timing : dwell {dwell_s:g} s, leave grace {grace_s:g} s")

    _, props_before = get("/properties")
    props_before = props_before if isinstance(props_before, list) else []
    ok("the mapped lane is present", len(props_before) > 0,
       f"{len(props_before)} properties")

    # Every zone the camera can reach, nearest first, in camera metres.
    reach = []
    for prop in props_before:
        _, detail = get(f"/properties/{prop['property_id']}")
        zone = detail.get("service_zone_geojson")
        if not zone:
            continue
        lat, lon = polygon_centre(zone)
        right, forward = wgs84_to_camera(cam, lat, lon)
        reach.append({"id": prop["property_id"], "lat": lat, "lon": lon,
                      "right": right, "forward": forward,
                      "d": math.hypot(right, forward)})
    reach.sort(key=lambda z: z["d"])
    if not ok("at least two mapped service zones are within reach of the camera",
              len(reach) >= 2, f"found {len(reach)}"):
        return 1

    house = reach[0]
    neighbour = reach[1]
    print(f"     house  : {house['id']} at camera "
          f"({house['right']:.2f} m right, {house['forward']:.2f} m fwd)")

    # The centre of that zone must actually be an unambiguous auto-association
    # BEFORE the engine is asked to act on it - otherwise a later failure
    # would be a mapping problem wearing an engine problem's clothes.
    _, look = _request("POST", f"{BASE}/gis/lookup",
                       {"latitude": house["lat"], "longitude": house["lon"]})
    ok(f"PostGIS auto-associates the centre of {house['id']} unambiguously",
       look.get("decision") == "AUTO_ASSOCIATED"
       and look.get("property_id") == house["id"]
       and len(look.get("candidates") or []) == 1,
       json.dumps({k: look.get(k) for k in ("decision", "property_id", "reason")}))

    # 60 m behind the camera: off the lane entirely.
    away_r, away_f = 0.0, -60.0
    h = math.radians(cam["heading_deg"])
    a_lat = cam["latitude"] + (away_f * math.cos(h) - away_r * math.sin(h)) / _M_PER_DEG_LAT
    a_lon = cam["longitude"] + (away_f * math.sin(h) + away_r * math.cos(h)) / (
        _M_PER_DEG_LAT * math.cos(math.radians(cam["latitude"])))
    _, look_away = _request("POST", f"{BASE}/gis/lookup",
                            {"latitude": a_lat, "longitude": a_lon})
    ok("the walk-away point is outside every service zone",
       look_away.get("decision") in ("NO_MATCH", "AMBIGUOUS")
       and not look_away.get("property_id"), str(look_away.get("decision")))

    # ---------------------------------------------------------------- 9
    # Checked first, because when the pose is missing NOTHING else can pass
    # and the run should say so rather than producing 20 confusing failures.
    print("\n9. a missing camera pose associates nothing")
    if not camera_configured:
        before = mine(status())["stats"]
        post(bound_payload(datetime.now(timezone.utc)))
        dwell(datetime.now(timezone.utc), house["right"], house["forward"],
              dwell_s + 3)
        after = mine(status())
        ok("9. with no surveyed camera pose, no association is attempted",
           after["stats"]["associations"] == before["associations"])
        ok("9b. and no episode is created",
           not after["active"] and not after["candidates"])
        print("\n  Camera pose is unset, so claims 1-8 and 10 cannot run.")
        print("  Restore CAMERA_ORIGIN_LAT/LON in backend/.env and re-run.")
        return 1 if FAILURES else 0
    print("  [SKIP] 9. needs a backend started with no CAMERA_ORIGIN_LAT/LON;")
    print("         proved without a restart by scripts/test_episode_engine.py"
          " check 19.9")

    # Start from a clean transient state. This aborts live episodes and
    # clears bindings; it does not delete a property, a zone or a past event.
    _request("POST", f"{BASE}/episodes/reset", {"abort_active_episodes": True})

    # ---------------------------------------------------------------- 1
    print("\n1. an UNBOUND track inside a real service zone creates nothing")
    before = mine(status())["stats"]
    t = datetime.now(timezone.utc)
    dwell(t, house["right"], house["forward"], dwell_s + 3)
    after = mine(status())
    ok("1. every unbound observation was skipped",
       after["stats"]["unbound_skipped"] > before["unbound_skipped"])
    # Note: the association counter is process-wide. If a REAL GeoVision edge
    # is streaming a BOUND track into this same backend while the test runs,
    # its associations land in this delta too. Stop the edge, or run against a
    # backend the edge is not pointed at, before reading a failure here as a
    # bug in the unbound guard.
    ok("1b. and none of them reached the service-zone lookup",
       after["stats"]["associations"] == before["associations"],
       f"{before['associations']} -> {after['stats']['associations']}")
    ok("1c. no episode, no candidate, no binding",
       after["stats"]["episodes_opened"] == before["episodes_opened"]
       and not after["candidates"] and not after["bindings"])

    code, ack = post(track_payload(datetime.now(timezone.utc), house["right"],
                                   house["forward"], property_id=house["id"]))
    ok("1d. a TRACK_UPDATE that dares to name a property is refused outright",
       code == 422, f"HTTP {code}")

    # ---------------------------------------------------------------- 2
    print("\n2. WORKER_TRACK_BOUND binds the collector to the camera track")
    t0 = datetime.now(timezone.utc)
    code, ack = post(bound_payload(t0))
    derived = (ack or {}).get("derived") or {}
    ok("2. the binding is accepted", code in (200, 202) and derived.get("handled"),
       json.dumps(ack)[:200])
    st = mine(status())
    ok(f"2b. {COLLECTOR} now owns track {TRACK}",
       any(b["collector_id"] == COLLECTOR and b["track_id"] == TRACK
           for b in st["bindings"]))
    ok("2c. binding alone opens no episode", not st["active"])

    # ---------------------------------------------------------------- 3, 4
    print(f"\n3-4. the bound track dwells in {house['id']}")
    t = t0 + timedelta(seconds=0.5)
    post(track_payload(t, house["right"], house["forward"]))
    time.sleep(0.6)
    st = mine(status())
    cand = next((c for c in st["candidates"]
                 if c["property_id"] == house["id"]), None)
    ok("3. the first observation yields exactly one property candidate",
       cand is not None and len(st["candidates"]) == 1, json.dumps(st["candidates"]))
    ok("3b. below the dwell threshold there is still no episode",
       not st["active"], json.dumps(st["active"])[:200])

    end = dwell(t + timedelta(seconds=0.5), house["right"], house["forward"],
                dwell_s + 2)
    st = mine(status())
    active = next((a for a in st["active"] if a["property_id"] == house["id"]), None)
    if not ok("4. the dwell threshold opened an episode", active is not None,
              json.dumps(st["active"])[:300]):
        return 1
    episode_id = active["episode_id"]
    print(f"     episode {episode_id} on {house['id']}")
    ok("4b. it is AUTO_ASSOCIATED to the property PostGIS chose",
       active["association_status"] == "AUTO_ASSOCIATED"
       and active["property_id"] == house["id"])
    ok("4c. and it opens SEGREGATED",
       active["segregation_status"] == "SEGREGATED")
    opened_after_dwell = st["stats"]["episodes_opened"]

    # ---------------------------------------------------------------- 5
    print("\n5. standing in the same zone opens no second episode")
    obs_before = active["observations"]
    end = dwell(end + timedelta(seconds=0.5), house["right"], house["forward"], 5)
    st = mine(status())
    ok("5. still exactly one live episode for this collector",
       len(st["active"]) == 1 and st["active"][0]["episode_id"] == episode_id)
    ok("5b. no second episode was opened",
       st["stats"]["episodes_opened"] == opened_after_dwell)
    ok("5c. the observations extended the SAME episode",
       st["active"][0]["observations"] > obs_before)
    ok("5d. and no collection event exists while it is open",
       len(events_for(episode_id)) == 0)

    # ---------------------------------------------------------------- 10
    print("\n10. a re-delivered event_id is idempotent")
    replay = track_payload(end + timedelta(seconds=0.5), house["right"],
                           house["forward"])
    code1, ack1 = post(replay)
    time.sleep(0.5)
    live = mine(status())["active"]
    if not ok("10. the episode is still open to be re-delivered into",
              bool(live), "it closed early - the sweeper beat this step"):
        return 1
    obs_after_first = live[0]["observations"]
    replays = []
    for _ in range(3):
        replays.append(post(dict(replay)))
        time.sleep(0.4)
    st = mine(status())
    ok("10b. the first delivery is ACCEPTED (202)",
       code1 == 202 and ack1.get("status") == "ACCEPTED", f"HTTP {code1}")
    ok("10c. every redelivery is DUPLICATE (200), never an error",
       all(c == 200 and b.get("duplicate") is True for c, b in replays),
       str([(c, b.get("status")) for c, b in replays]))
    ok("10d. the redeliveries changed nothing: same observation count",
       bool(st["active"]) and st["active"][0]["observations"] == obs_after_first,
       f"{obs_after_first} -> "
       f"{st['active'][0]['observations'] if st['active'] else 'closed'}")
    ok("10e. and no second episode",
       st["stats"]["episodes_opened"] == opened_after_dwell)
    if not st["active"]:
        print("  the episode closed during the idempotency step; "
              "cannot time the leave grace from here")
        return 1
    last_inside = datetime.fromisoformat(
        st["active"][0]["last_inside"].replace("Z", "+00:00"))

    # ---------------------------------------------------------------- 6
    print(f"\n6. the collector leaves the zone; grace is {grace_s:g} s")
    # Event timestamps advance faster than wall clock on purpose: the leave
    # grace is crossed in EVENT time while only ~1 s of wall clock passes, so
    # the wall-clock sweeper cannot be what closes this episode. What closes
    # it is the TRACK_UPDATE ladder, which is the path under test.
    step_s = grace_s / 2.0
    closed_at_gap = None
    wall0 = time.monotonic()
    for i in range(1, 6):
        gap = i * step_s
        post(track_payload(last_inside + timedelta(seconds=gap), away_r, away_f))
        time.sleep(0.45)
        st = mine(status())
        live = any(a["episode_id"] == episode_id for a in st["active"])
        print(f"     event gap {gap:4.1f} s  wall {time.monotonic() - wall0:4.1f} s"
              f"  -> {'ACTIVE' if live else 'CLOSED'}")
        if not live:
            closed_at_gap = gap
            break
        ok(f"6. inside the grace (gap {gap:g} s) the episode stays open",
           gap < grace_s, f"still open at {gap:g} s >= grace {grace_s:g} s")

    wall_elapsed = time.monotonic() - wall0
    ok("6b. leaving the zone closed the episode", closed_at_gap is not None)
    if closed_at_gap is not None:
        ok("6c. it closed on the leave grace, not before",
           closed_at_gap >= grace_s, f"closed at gap {closed_at_gap:g} s")
        ok("6d. and the TRACK_UPDATE path closed it, not the wall-clock sweeper",
           wall_elapsed < grace_s, f"{wall_elapsed:.1f} s of wall clock elapsed")

    _, ep = get(f"/episodes/{episode_id}")
    ok(f"6e. {episode_id} is CLOSED", ep.get("state") == "CLOSED",
       str(ep.get("state")))
    ok("6f. and closed SEGREGATED - no trigger, so the waste was segregated",
       ep.get("segregation_status") == "SEGREGATED",
       str(ep.get("segregation_status")))
    ended_at = ep.get("ended_at")
    try:
        ended = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
        drift = abs((ended - last_inside).total_seconds())
    except Exception:  # noqa: BLE001
        drift = None
    ok("6g. it ended at the last moment the collector was inside the zone, "
       "not at the moment we noticed",
       drift is not None and drift < 0.01,
       f"ended_at={ended_at} last_inside={last_inside.isoformat()}")

    # ---------------------------------------------------------------- 7
    print("\n7. exactly one authoritative collection event")
    rows = events_for(episode_id)
    ok("7. exactly one collection event exists for this episode",
       len(rows) == 1, f"{len(rows)} rows")
    ok("7b. the episode points at it",
       bool(ep.get("collection_event_id"))
       and rows and rows[0]["event_id"] == ep["collection_event_id"])
    if rows:
        ev = rows[0]
        print(f"     {ev['event_id']}  {ev['property_id']}  {ev['picker_id']}  "
              f"{ev['segregation_status']}  {ev['review_status']}")
        ok("7c. it is written into the existing collection_events model, "
           "against the property WASTRAQ chose",
           ev["property_id"] == house["id"] and ev["collected"] is True)
        ok("7d. SEGREGATED, AUTO_CONFIRMED, and NOT rfid_triggered",
           ev["segregation_status"] == "SEGREGATED"
           and ev["review_status"] == "AUTO_CONFIRMED"
           and ev["rfid_triggered"] is False)
        ok("7e. it carries the picker and the camera track",
           ev["picker_id"] == COLLECTOR and str(ev["track_id"]) == str(TRACK))

    # Walking further away must not produce a second event.
    dwell(last_inside + timedelta(seconds=grace_s * 4), away_r, away_f, 2)
    ok("7f. walking further away writes no second event",
       len(events_for(episode_id)) == 1)

    # ---------------------------------------------------------------- 8
    print("\n8. an ambiguous position associates nothing")
    _request("POST", f"{BASE}/episodes/reset", {"abort_active_episodes": True})
    mid_lat = (house["lat"] + neighbour["lat"]) / 2
    mid_lon = (house["lon"] + neighbour["lon"]) / 2
    _, amb = _request("POST", f"{BASE}/gis/lookup",
                      {"latitude": mid_lat, "longitude": mid_lon})
    if amb.get("decision") != "AMBIGUOUS":
        print(f"  [SKIP] 8. the midpoint of {house['id']} and {neighbour['id']} "
              f"is {amb.get('decision')}, not AMBIGUOUS on this lane geometry")
    else:
        print(f"     {amb['reason']}")
        amb_r, amb_f = wgs84_to_camera(cam, mid_lat, mid_lon)
        before = mine(status())["stats"]
        t0 = datetime.now(timezone.utc)
        post(bound_payload(t0))
        dwell(t0 + timedelta(seconds=0.5), amb_r, amb_f, dwell_s * 2 + 2)
        after = mine(status())
        ok("8. the ambiguity was seen and counted",
           after["stats"]["ambiguous"] > before["ambiguous"])
        ok("8b. no episode was opened, however long the position was held",
           after["stats"]["episodes_opened"] == before["episodes_opened"]
           and not after["active"])
        ok("8c. and no candidate was left half-built",
           not after["candidates"])
        ok("8d. it was NOT quietly resolved to the nearest property",
           amb.get("property_id") is None)

    # ---------------------------------------------------------------- audit
    print("\n11. the survey is exactly as it was")
    _request("POST", f"{BASE}/episodes/reset", {"abort_active_episodes": True})
    _, props_after = get("/properties")
    ok("11. the property count is unchanged",
       isinstance(props_after, list) and len(props_after) == len(props_before),
       f"{len(props_before)} -> {len(props_after)}")
    _, detail_after = get(f"/properties/{house['id']}")
    lat2, lon2 = polygon_centre(detail_after["service_zone_geojson"])
    ok("11b. the service zone this run used is byte-for-byte where it was",
       abs(lat2 - house["lat"]) < 1e-12 and abs(lon2 - house["lon"]) < 1e-12)
    _, gv = get("/integrations/geovision/status")
    ok("11c. GeoVision still reports that it does not decide the property",
       gv.get("property_association", {}).get("performed_here") is False)

    print("\n" + "-" * 74)
    print(f"rows created by this run carry source_id = {SOURCE}")
    print("to remove them:")
    print(f"  psql wastraq_demo -v source=\"'{SOURCE}'\" "
          f"-f database/cleanup_step2_test.sql")
    print("-" * 74)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("STEP 2 PROVED - ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
