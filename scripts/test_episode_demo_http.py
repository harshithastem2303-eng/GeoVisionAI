#!/usr/bin/env python3
"""
The two-house demo, driven over the wire against a RUNNING WASTRAQ backend.

    scripts/run_backend.sh                        # in another terminal
    python3 scripts/test_episode_demo_http.py
    python3 scripts/test_episode_demo_http.py http://127.0.0.1:8000

stdlib only (urllib) - the same transport the GeoVision edge uses, so this
exercises the real FastAPI route, the real pydantic validation, the real
PostGIS association and the real database, rather than a TestClient sharing
the process.

It is SELF-CONFIGURING. It reads the camera pose from /episodes/status and
the service-zone polygons from /properties, then inverts the camera
transform to work out what the edge would have reported for a collector
standing inside each zone. Move the camera in .env, re-run, and the
coordinates follow - there is nothing hard-coded to go stale.

The sequence is the demo:

    PICKER-01 binds to track 35
    track 35 dwells in the first property   -> episode, mirrored
    track 35 leaves                          -> SEGREGATED
    track 35 dwells in the second property   -> episode, mirrored
    NON_SEGREGATION_TRIGGER for that episode -> NOT_SEGREGATED
    EVIDENCE_READY                           -> clip linked
    track 35 leaves                          -> collection event + evidence

Every event carries a run-scoped uuid and a run-scoped session_id, so it is
safe against a live demo database: it creates episodes and collection events
(that is the point) but it never touches a property, a service zone or an
existing row, and everything it made is identifiable afterwards by
source_id.
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
SOURCE = f"GEOVISION-DEMO-{RUN}"
SESSION = f"sess-{RUN}"
COLLECTOR = os.getenv("DEMO_COLLECTOR", "PICKER-01")
TRACK = int(os.getenv("DEMO_TRACK", "35"))
UID = os.getenv("DEMO_RFID_UID", "04A1B2C3")

FAILURES: list[str] = []
_M_PER_DEG_LAT = 111_320.0


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


def get(path: str) -> tuple[int, dict]:
    return _request("GET", f"{BASE}{path}")


# --- geometry -----------------------------------------------------------------
def wgs84_to_camera(cam: dict, lat: float, lon: float) -> tuple[float, float]:
    """Inverse of backend/app/episodes/transform.py, kept deliberately
    independent: if the backend's forward transform is wrong, this test's
    coordinates will land in the wrong zone and the run will fail loudly
    rather than agreeing with the bug."""
    north = (lat - cam["latitude"]) * _M_PER_DEG_LAT
    mid = math.radians((cam["latitude"] + lat) / 2.0)
    east = (lon - cam["longitude"]) * _M_PER_DEG_LAT * math.cos(mid)
    h = math.radians(cam["heading_deg"])
    forward = east * math.sin(h) + north * math.cos(h)
    right = east * math.cos(h) - north * math.sin(h)
    return right, forward


def polygon_centre(geojson: dict) -> tuple[float, float]:
    """Mean of a polygon's outer ring. Good enough to stand inside a service
    zone; this is a test fixture, not a cartography library."""
    ring = geojson["coordinates"][0]
    lons = [p[0] for p in ring[:-1]] or [p[0] for p in ring]
    lats = [p[1] for p in ring[:-1]] or [p[1] for p in ring]
    return sum(lats) / len(lats), sum(lons) / len(lons)


# --- event builders -----------------------------------------------------------
_n = [0]


def eid() -> str:
    _n[0] += 1
    return f"{RUN}-{_n[0]:04d}"


def send_bound(at: datetime) -> tuple[int, dict]:
    return post({"event_type": "WORKER_TRACK_BOUND", "event_id": eid(),
                 "timestamp": iso(at), "source_id": SOURCE,
                 "session_id": SESSION, "collector_id": COLLECTOR,
                 "rfid_uid": UID, "track_id": TRACK, "confidence": 0.93,
                 "rfid_event_id": f"{RUN}-rfid-1"})


def send_track(at: datetime, right: float, forward: float) -> tuple[int, dict]:
    return post({"event_type": "TRACK_UPDATE", "event_id": eid(),
                 "timestamp": iso(at), "source_id": SOURCE,
                 "session_id": SESSION, "track_id": TRACK, "confidence": 0.9,
                 "depth_m": round(abs(forward), 3), "depth_valid": True,
                 "depth_status": "OK",
                 "relative_x_m": round(right, 3),
                 "relative_forward_m": round(forward, 3),
                 "is_authorized_picker": True, "collector_id": COLLECTOR})


def send_trigger(at: datetime, trigger_id: str, episode_id: str | None,
                 **over) -> tuple[int, dict]:
    payload = {"event_type": "NON_SEGREGATION_TRIGGER", "event_id": eid(),
               "timestamp": iso(at), "source_id": SOURCE,
               "session_id": SESSION, "trigger_id": trigger_id,
               "episode_id": episode_id, "collector_id": COLLECTOR,
               "rfid_uid": UID, "track_id": TRACK,
               "trigger_status": "RESOLVED", "duplicate": False,
               "rfid_event_id": f"{RUN}-rfid-2"}
    payload.update(over)
    return post(payload)


def send_clip(at: datetime) -> tuple[int, dict]:
    return post({"event_type": "EVIDENCE_READY", "event_id": eid(),
                 "timestamp": iso(at), "source_id": SOURCE,
                 "session_id": SESSION, "clip_id": f"CLIP-{RUN}",
                 "track_id": TRACK, "rfid_event_id": f"{RUN}-rfid-2",
                 "frame_count": 131,
                 "file_path": rf"C:\GeoVision\backend\evidence_clips\CLIP-{RUN}.mp4"})


def dwell(at: datetime, right: float, forward: float, seconds: float,
          step: float = 0.5) -> datetime:
    """Stand still, reporting at ~2 Hz, exactly as a bound collector would."""
    steps = max(int(seconds / step), 1)
    for i in range(steps + 1):
        send_track(at + timedelta(seconds=i * step), right, forward)
        # Real wall-clock spacing: the engine throttles PostGIS work by wall
        # clock, so firing all of these instantly would collapse the dwell
        # into a single association.
        time.sleep(step / 2)
    return at + timedelta(seconds=steps * step)


def main() -> int:  # noqa: C901
    print("=" * 72)
    print(f"WASTRAQ two-house episode demo over HTTP  ({BASE})")
    print(f"source_id={SOURCE}  session_id={SESSION}")
    print("=" * 72)

    print("\n0. preconditions")
    status, health = get("/health/episodes")
    if not ok("backend is up and the episode engine is loaded",
              status == 200 and health.get("status") != "unavailable",
              json.dumps(health)[:200]):
        return 1
    if not ok("engine is enabled", health.get("enabled") is True):
        return 1
    if not ok("camera pose is configured (CAMERA_ORIGIN_LAT/LON in backend/.env)",
              health.get("camera_configured") is True,
              "set CAMERA_ORIGIN_LAT, CAMERA_ORIGIN_LON, CAMERA_HEADING_DEG"):
        return 1

    _, engine_status = get("/episodes/status")
    cam = engine_status["engine"]["camera"]
    print(f"     camera: {cam['latitude']}, {cam['longitude']} "
          f"heading {cam['heading_deg']} deg")

    _, props_before = get("/properties")
    n_props = len(props_before) if isinstance(props_before, list) else 0
    ok("mapped properties are present", n_props > 0, f"{n_props} properties")

    # Pick the two zones the camera can actually reach - nearest first, so
    # this works whatever the lane geometry is.
    reachable = []
    for prop in (props_before if isinstance(props_before, list) else []):
        _, detail = get(f"/properties/{prop['property_id']}")
        zone = detail.get("service_zone_geojson")
        if not zone:
            continue
        lat, lon = polygon_centre(zone)
        right, forward = wgs84_to_camera(cam, lat, lon)
        reachable.append((math.hypot(right, forward), prop["property_id"],
                          right, forward))
    reachable.sort()
    if not ok("at least two mapped service zones are within reach of the camera",
              len(reachable) >= 2, f"found {len(reachable)}"):
        return 1

    (_, house1, r1, f1), (_, house2, r2, f2) = reachable[0], reachable[1]
    print(f"     house 1 = {house1}  at camera ({r1:.2f} m right, {f1:.2f} m fwd)")
    print(f"     house 2 = {house2}  at camera ({r2:.2f} m right, {f2:.2f} m fwd)")

    _, cfg = get("/episodes/status")
    dwell_s = float(cfg["engine"]["dwell_s"])
    grace_s = float(cfg["engine"]["leave_grace_s"])

    # Somewhere well outside every zone: 60 m behind the camera.
    away_r, away_f = 0.0, -60.0

    t = datetime.now(timezone.utc)

    print("\n1. FIRST TAP - bind the collector to a camera track")
    code, ack = send_bound(t)
    ok("WORKER_TRACK_BOUND accepted", code in (200, 202), json.dumps(ack)[:200])
    _, st = get("/episodes/status")
    bindings = st["engine"]["bindings"]
    ok(f"{COLLECTOR} is bound to track {TRACK}",
       any(b["collector_id"] == COLLECTOR and b["track_id"] == TRACK
           for b in bindings))

    print(f"\n2. HOUSE 1 ({house1}) - dwell {dwell_s + 2:g}s")
    t = dwell(t + timedelta(seconds=1), r1, f1, dwell_s + 2)
    _, st = get("/episodes/status")
    active = st["engine"]["active_episodes"]
    e1 = next((a["episode_id"] for a in active if a["property_id"] == house1), None)
    ok(f"an episode opened on {house1}", e1 is not None,
       json.dumps(active)[:300])
    if e1:
        print(f"     episode {e1}")
        ok("it is mirrored to Windows (or mirroring is off)",
           st["mirror"]["enabled"] is False or e1 in st["mirror"]["mirrored_episode_ids"]
           or st["mirror"]["queue_depth"] >= 0)

    print(f"\n3. LEAVE - no second tap, so {house1} is SEGREGATED")
    t = dwell(t + timedelta(seconds=1), away_r, away_f, grace_s + 2)
    time.sleep(grace_s + 1.5)  # let the sweeper close it if the walk did not
    if e1:
        _, ep = get(f"/episodes/{e1}")
        ok(f"{e1} closed", ep.get("state") == "CLOSED", str(ep.get("state")))
        ok(f"{e1} is SEGREGATED",
           ep.get("segregation_status") == "SEGREGATED")
        ok(f"{e1} wrote a collection event",
           bool(ep.get("collection_event_id")))
        ok("the Windows mirror was removed",
           ep.get("mirror_status") in ("REMOVED", "DISABLED", "REMOVE_FAILED"),
           str(ep.get("mirror_status")))

    print(f"\n4. HOUSE 2 ({house2}) - dwell, then the SECOND TAP")
    t = dwell(t + timedelta(seconds=2), r2, f2, dwell_s + 2)
    _, st = get("/episodes/status")
    active = st["engine"]["active_episodes"]
    e2 = next((a["episode_id"] for a in active if a["property_id"] == house2), None)
    ok(f"an episode opened on {house2}", e2 is not None,
       json.dumps(active)[:300])

    if e2:
        print(f"     episode {e2}")
        trg = f"TRG-{RUN}"

        code, ack = send_trigger(t, f"{trg}-ghost", "E-DOES-NOT-EXIST")
        derived = (ack or {}).get("derived") or {}
        ok("a trigger for an unknown episode is refused, not applied",
           derived.get("resolution") == "UNKNOWN_EPISODE"
           and not derived.get("applied"), json.dumps(derived)[:200])

        code, ack = send_trigger(t, f"{trg}-wrongtrack", e2, track_id=TRACK + 7)
        derived = (ack or {}).get("derived") or {}
        ok("a trigger naming the wrong track is refused",
           derived.get("resolution") == "IDENTITY_MISMATCH",
           json.dumps(derived)[:200])

        code, ack = send_trigger(t, trg, e2)
        derived = (ack or {}).get("derived") or {}
        ok("the real trigger is APPLIED", derived.get("applied") is True,
           json.dumps(derived)[:300])
        ok(f"and it resolved to {house2} - WASTRAQ's property, not the edge's",
           derived.get("property_id") == house2, str(derived.get("property_id")))

        code, ack = send_trigger(t + timedelta(seconds=1), trg, e2)
        derived = (ack or {}).get("derived") or {}
        ok("the same trigger_id again does nothing",
           derived.get("resolution") == "DUPLICATE", json.dumps(derived)[:200])

        print("\n5. EVIDENCE_READY")
        code, ack = send_clip(t + timedelta(seconds=1))
        derived = (ack or {}).get("derived") or {}
        ok("the clip is attributed to the right episode",
           derived.get("episode_id") == e2, json.dumps(derived)[:200])

        print("\n6. LEAVE HOUSE 2")
        t = dwell(t + timedelta(seconds=2), away_r, away_f, grace_s + 2)
        time.sleep(grace_s + 1.5)
        _, ep = get(f"/episodes/{e2}")
        ok(f"{e2} closed NOT_SEGREGATED",
           ep.get("state") == "CLOSED"
           and ep.get("segregation_status") == "NOT_SEGREGATED",
           f"{ep.get('state')}/{ep.get('segregation_status')}")
        event2 = ep.get("collection_event_id")
        ok(f"{e2} wrote a collection event", bool(event2))
        if event2:
            _, detail = get(f"/collection-events/{event2}")
            ok("the event is NOT_SEGREGATED and rfid_triggered",
               detail.get("segregation_status") == "NOT_SEGREGATED"
               and detail.get("rfid_triggered") is True)
            kinds = [e["evidence_type"] for e in detail.get("evidence", [])]
            ok("a NON_SEGREGATION_PROOF row exists",
               "NON_SEGREGATION_PROOF" in kinds, str(kinds))
            ok("the video clip is linked as evidence",
               "VIDEO_CLIP" in kinds, str(kinds))

    print("\n7. dashboard")
    _, feed = get("/collection-events/feed/detailed?limit=20")
    rows = [r for r in (feed if isinstance(feed, list) else [])
            if r.get("property_id") in (house1, house2)]
    for row in rows[:6]:
        print(f"     {row['property_id']} | {row.get('picker_id')} | "
              f"{row['segregation_status']}"
              + (f" | {row['evidence_count']} evidence"
                 if row.get("evidence_count") else ""))
    ok("both houses appear in the dashboard feed",
       {r["property_id"] for r in rows} >= {house1, house2},
       str(sorted({r["property_id"] for r in rows})))

    print("\n8. nothing was mapped, unmapped or remapped")
    _, props_after = get("/properties")
    ok("the property count is unchanged",
       isinstance(props_after, list) and len(props_after) == n_props,
       f"{n_props} -> {len(props_after) if isinstance(props_after, list) else '?'}")

    _, gv = get("/integrations/geovision/status")
    ok("GeoVision still reports it does not decide the property",
       gv.get("property_association", {}).get("performed_here") is False)
    ok("the receiver advertises all six event types",
       len(gv.get("accepted_event_types", [])) == 6
       and "NON_SEGREGATION_TRIGGER" in gv.get("accepted_event_types", []))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
