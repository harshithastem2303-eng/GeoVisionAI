#!/usr/bin/env python3
"""
STEP 3 over the wire - the synthetic HTTP sequence that proves it.

    scripts/run_backend.sh                  # in another terminal
    python3 scripts/test_step3_http.py
    python3 scripts/test_step3_http.py http://127.0.0.1:8000

stdlib only (urllib), against a RUNNING backend and the real database, so
this exercises the real FastAPI route, the real pydantic envelope, the real
PostGIS association and the real SQL - not a fake store.

It reuses the transport, the inverse camera transform and the dwell pump
from test_episode_demo_http.py, so there is one definition of "what the
GeoVision edge would have sent".

What it proves that test_episode_demo_http.py does not
------------------------------------------------------
That suite walks two houses and flags the second one. This one is about
the sixth event specifically, and about the DASHBOARD:

  * every refusal path over the wire, including the two the receiver must
    reject with 422 before the engine ever sees them;
  * the just-closed-episode correction (safety requirement 7) - the tap
    that lost its race with the collector walking away. This is the only
    path that UPDATEs a collection event the dashboard has already shown;
  * the dashboard's own APIs, as DELTAS: /summary KPI cards,
    v_collection_summary (which is the map fill and the property table),
    the filtered event feed, and /analytics/operations.

Safe against a live demo database. Every event carries a run-scoped
source_id and session_id; every count is asserted as a delta; it creates
episodes, collection events and evidence (that is the point) and touches no
property, service zone or pre-existing row. Everything it made is removable
afterwards with the printed cleanup command.

THE SEQUENCE  (each line is one HTTP request; E = the episode WASTRAQ minted)

   0  GET  /health/episodes                          engine up, camera surveyed
      GET  /episodes/status                          camera pose, dwell, grace
      GET  /properties  + /properties/{id}           pick two reachable zones
      GET  /summary?route_id=...                     BASELINE KPIs
   1  POST /integrations/geovision/events  WORKER_TRACK_BOUND
   2  POST ... TRACK_UPDATE  x N   inside zone A     -> episode E1 opens
   3  POST ... NON_SEGREGATION_TRIGGER   property_id     -> 422 refused
      POST ... NON_SEGREGATION_TRIGGER   segregation_status -> 422 refused
      POST ... NON_SEGREGATION_TRIGGER   episode E-NOPE  -> UNKNOWN_EPISODE
      POST ... NON_SEGREGATION_TRIGGER   wrong collector -> IDENTITY_MISMATCH
      POST ... NON_SEGREGATION_TRIGGER   wrong track     -> IDENTITY_MISMATCH
      POST ... NON_SEGREGATION_TRIGGER   NO_ACTIVE_EPISODE -> EDGE_UNRESOLVED
      GET  /episodes/{E1}                            still SEGREGATED
   4  POST ... NON_SEGREGATION_TRIGGER   valid, E1    -> APPLIED, our property
   5  POST ... NON_SEGREGATION_TRIGGER   same trigger_id, new event_id -> DUPLICATE
   6  POST ... TRACK_UPDATE  x N   outside            -> E1 CLOSED NOT_SEGREGATED
   7  GET  /collection-events/{id}                   NOT_SEGREGATED + evidence
      GET  /summary?route_id=...                     not_segregated +1, events +1
      GET  /collection-events/feed/detailed?...      both filters find it
      GET  /analytics/operations?route_id=...        credited to the property
   8  POST ... TRACK_UPDATE  x N   inside zone B      -> episode E2 opens
      POST ... TRACK_UPDATE  x N   outside            -> E2 CLOSED SEGREGATED
      POST ... NON_SEGREGATION_TRIGGER   valid, E2    -> APPLIED to a CLOSED episode
      GET  /collection-events/{id}                   corrected IN PLACE
      GET  /summary                                  no extra event, +1 not_segregated
   9  GET  /properties                               count unchanged
      GET  /integrations/geovision/status            still decides no property
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "demo_http", os.path.join(HERE, "test_episode_demo_http.py"))
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["demo_http"] = demo
_spec.loader.exec_module(demo)

BASE = demo.BASE
SOURCE = demo.SOURCE.replace("DEMO", "STEP3")
SESSION = demo.SESSION
COLLECTOR = demo.COLLECTOR
TRACK = demo.TRACK
UID = demo.UID
RUN = demo.RUN

get = demo.get
post = demo.post
iso = demo.iso
eid = demo.eid
polygon_centre = demo.polygon_centre
wgs84_to_camera = demo.wgs84_to_camera

FAILURES: list[str] = []
SENT: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return bool(condition)


# --- builders, all carrying THIS run's source_id ------------------------------
def send(payload: dict) -> tuple[int, dict]:
    payload.setdefault("event_id", eid())
    payload.setdefault("source_id", SOURCE)
    payload.setdefault("session_id", SESSION)
    SENT.append(f"POST /integrations/geovision/events  {payload['event_type']}"
                + (f"  trigger_id={payload['trigger_id']}"
                   if "trigger_id" in payload else ""))
    return post(payload)


def bind(at: datetime) -> tuple[int, dict]:
    return send({"event_type": "WORKER_TRACK_BOUND", "timestamp": iso(at),
                 "collector_id": COLLECTOR, "rfid_uid": UID,
                 "track_id": TRACK, "confidence": 0.93,
                 "rfid_event_id": f"{RUN}-rfid-1"})


def track(at: datetime, right: float, forward: float) -> tuple[int, dict]:
    return send({"event_type": "TRACK_UPDATE", "timestamp": iso(at),
                 "track_id": TRACK, "confidence": 0.9,
                 "depth_m": round(abs(forward), 3), "depth_valid": True,
                 "depth_status": "OK",
                 "relative_x_m": round(right, 3),
                 "relative_forward_m": round(forward, 3),
                 "is_authorized_picker": True, "collector_id": COLLECTOR})


def trigger(at: datetime, trigger_id: str, episode_id: str | None,
            **over) -> tuple[int, dict]:
    payload = {"event_type": "NON_SEGREGATION_TRIGGER", "timestamp": iso(at),
               "trigger_id": trigger_id, "episode_id": episode_id,
               "collector_id": COLLECTOR, "rfid_uid": UID, "track_id": TRACK,
               "trigger_status": "RESOLVED", "duplicate": False,
               "rfid_event_id": f"{RUN}-rfid-2"}
    payload.update(over)
    return send(payload)


def walk_to(t: datetime, right: float, forward: float, seconds: float,
            step: float = 0.5) -> datetime:
    """Feed TRACK_UPDATEs at ~2 Hz, in real wall-clock time.

    Deliberately NOT demo.dwell(): that helper builds its own envelope with
    the demo suite's source_id, and this run's triggers must come from the
    same camera as this run's episodes or the identity check would - quite
    correctly - refuse every one of them.
    """
    steps = max(int(seconds / step), 1)
    for i in range(steps + 1):
        track(t + timedelta(seconds=i * step), right, forward)
        time.sleep(step / 2)
    return t + timedelta(seconds=steps * step)


def derived_of(ack: dict) -> dict:
    return (ack or {}).get("derived") or {}


def totals(rid: str) -> dict:
    _, s = get(f"/summary?route_id={rid}")
    return dict(s.get("totals") or {})


def summary_row(rid: str, property_id: str) -> dict | None:
    """The v_collection_summary row for a property - this IS the map fill."""
    _, s = get(f"/summary?route_id={rid}")
    rows = [r for r in (s.get("events") or [])
            if r.get("property_id") == property_id]
    return rows[0] if rows else None


# =============================================================================
def main() -> int:  # noqa: C901
    print("=" * 74)
    print(f"STEP 3 - NON_SEGREGATION_TRIGGER over HTTP   ({BASE})")
    print(f"source_id={SOURCE}  session_id={SESSION}")
    print("=" * 74)

    # -- 0. preconditions ---------------------------------------------------
    print("\n0. preconditions and baseline")
    status, health = get("/health/episodes")
    if not ok("backend up and the episode engine loaded",
              status == 200 and health.get("status") != "unavailable",
              json.dumps(health)[:200]):
        return 1
    if not ok("engine enabled", health.get("enabled") is True):
        return 1
    if not ok("camera pose surveyed (CAMERA_ORIGIN_LAT/LON in backend/.env)",
              health.get("camera_configured") is True):
        return 1

    _, st = get("/episodes/status")
    cam = st["engine"]["camera"]
    dwell_s = float(st["engine"]["dwell_s"])
    grace_s = float(st["engine"]["leave_grace_s"])
    late_grace_s = float(st["engine"].get("trigger_late_grace_s") or 30.0)
    # /summary with no route_id answers for the operations route the
    # dashboard itself defaults to, and echoes which one that was.
    _, s0 = get("/summary")
    rid = s0.get("route_id") or "ROUTE-DEMO-01"
    print(f"     camera {cam['latitude']}, {cam['longitude']} "
          f"heading {cam['heading_deg']} deg | dwell {dwell_s}s "
          f"grace {grace_s}s late-grace {late_grace_s}s | route {rid}")

    _, props = get("/properties")
    n_props = len(props) if isinstance(props, list) else 0
    reachable = []
    for prop in (props if isinstance(props, list) else []):
        _, detail = get(f"/properties/{prop['property_id']}")
        zone = detail.get("service_zone_geojson")
        if not zone:
            continue
        lat, lon = polygon_centre(zone)
        right, forward = wgs84_to_camera(cam, lat, lon)
        reachable.append(((right ** 2 + forward ** 2) ** 0.5,
                          prop["property_id"], right, forward))
    reachable.sort()
    if not ok("at least two mapped service zones are reachable from the camera",
              len(reachable) >= 2, f"found {len(reachable)}"):
        return 1
    (_, houseA, rA, fA), (_, houseB, rB, fB) = reachable[0], reachable[1]
    away_r, away_f = 0.0, -60.0
    print(f"     house A = {houseA} ({rA:.2f} right, {fA:.2f} fwd)")
    print(f"     house B = {houseB} ({rB:.2f} right, {fB:.2f} fwd)")

    base = totals(rid)
    print(f"     BASELINE  events={base.get('events')} "
          f"segregated={base.get('segregated')} "
          f"not_segregated={base.get('not_segregated')} "
          f"needs_review={base.get('needs_review')} "
          f"evidence={base.get('evidence')} "
          f"properties={base.get('properties')}")

    t = datetime.now(timezone.utc)

    # -- 1-2. bind and open an episode --------------------------------------
    print(f"\n1-2. bind {COLLECTOR} to track {TRACK}, then dwell in {houseA}")
    code, ack = bind(t)
    ok("WORKER_TRACK_BOUND accepted", code in (200, 202), json.dumps(ack)[:200])
    t = walk_to(t + timedelta(seconds=1), rA, fA, dwell_s + 2)
    _, st = get("/episodes/status")
    e1 = next((a["episode_id"] for a in st["engine"]["active_episodes"]
               if a["property_id"] == houseA), None)
    if not ok(f"an episode opened on {houseA}", e1 is not None,
              json.dumps(st["engine"]["active_episodes"])[:300]):
        return 1
    print(f"     episode {e1}")

    # -- 3. every refusal path, over the wire -------------------------------
    print("\n3. the refusal paths - none of these may change a property")

    code, body = trigger(t, f"TRG-{RUN}-inject", e1, property_id=houseB)
    ok("3a. a trigger carrying property_id is refused at the door (422)",
       code == 422, f"{code} {json.dumps(body)[:160]}")
    code, body = trigger(t, f"TRG-{RUN}-verdict", e1,
                         segregation_status="NOT_SEGREGATED")
    ok("3b. a trigger carrying segregation_status is refused (422)",
       code == 422, f"{code} {json.dumps(body)[:160]}")
    code, body = trigger(t, f"TRG-{RUN}-nested", e1,
                         edge_debug={"assoc": {"property_id": houseB}})
    ok("3c. a property_id nested in an unknown object is refused (422)",
       code == 422, f"{code} {json.dumps(body)[:160]}")

    d = derived_of(trigger(t, f"TRG-{RUN}-ghost", "E-DOES-NOT-EXIST")[1])
    ok("3d. an unknown episode_id is refused, not guessed at",
       d.get("resolution") == "UNKNOWN_EPISODE" and not d.get("applied"),
       json.dumps(d)[:200])
    ok("3e. and it is preserved for review", d.get("needs_review") is True)

    d = derived_of(trigger(t, f"TRG-{RUN}-collector", e1,
                           collector_id="PICKER-99")[1])
    ok("3f. the wrong collector is refused",
       d.get("resolution") == "IDENTITY_MISMATCH" and not d.get("applied"),
       json.dumps(d)[:200])

    d = derived_of(trigger(t, f"TRG-{RUN}-track", e1, track_id=TRACK + 7)[1])
    ok("3g. the wrong track is refused",
       d.get("resolution") == "IDENTITY_MISMATCH" and not d.get("applied"),
       json.dumps(d)[:200])

    d = derived_of(trigger(t, f"TRG-{RUN}-veto", None,
                           trigger_status="NO_ACTIVE_EPISODE")[1])
    ok("3h. the edge's own NO_ACTIVE_EPISODE is believed, not overridden",
       d.get("resolution") == "EDGE_UNRESOLVED" and not d.get("applied"),
       json.dumps(d)[:200])

    _, ep = get(f"/episodes/{e1}")
    ok("3i. after six bad triggers the episode is still SEGREGATED",
       ep.get("segregation_status") == "SEGREGATED",
       str(ep.get("segregation_status")))
    mid = totals(rid)
    ok("3j. and no KPI moved",
       mid.get("not_segregated") == base.get("not_segregated")
       and mid.get("events") == base.get("events"),
       f"{base.get('not_segregated')}->{mid.get('not_segregated')}")

    # -- 4-5. the real trigger, then its redelivery -------------------------
    print("\n4-5. the real second tap, and the same decision re-announced")
    trg1 = f"TRG-{RUN}-A"
    d = derived_of(trigger(t, trg1, e1)[1])
    ok("4a. the valid trigger is APPLIED",
       d.get("applied") is True and d.get("resolution") == "APPLIED",
       json.dumps(d)[:300])
    ok(f"4b. it resolved to {houseA} - WASTRAQ's property, not the edge's",
       d.get("property_id") == houseA, str(d.get("property_id")))

    d = derived_of(trigger(t + timedelta(seconds=1), trg1, e1)[1])
    ok("5a. the same trigger_id under a NEW event_id is a no-op",
       d.get("resolution") == "DUPLICATE" and not d.get("applied"),
       json.dumps(d)[:200])
    d = derived_of(trigger(t + timedelta(seconds=1), f"TRG-{RUN}-A2", e1)[1])
    ok("5b. a different trigger on the already-flagged episode is refused",
       d.get("resolution") == "EPISODE_NOT_ACTIONABLE" and not d.get("applied"),
       json.dumps(d)[:200])

    # -- 6. leave, and the event that carries the verdict -------------------
    print(f"\n6. leave {houseA} - the episode closes and writes ONE event")
    t = walk_to(t + timedelta(seconds=1), away_r, away_f, grace_s + 2)
    time.sleep(grace_s + 1.5)
    _, ep = get(f"/episodes/{e1}")
    ok("6a. the episode closed NOT_SEGREGATED",
       ep.get("state") == "CLOSED"
       and ep.get("segregation_status") == "NOT_SEGREGATED",
       f"{ep.get('state')}/{ep.get('segregation_status')}")
    event1 = ep.get("collection_event_id")
    if not ok("6b. it wrote a collection event", bool(event1)):
        return 1
    print(f"     collection event {event1}")

    # -- 7. the dashboard's own APIs ----------------------------------------
    print("\n7. the dashboard data path")
    _, detail = get(f"/collection-events/{event1}")
    ok("7a. GET /collection-events/{id} says NOT_SEGREGATED",
       detail.get("segregation_status") == "NOT_SEGREGATED",
       str(detail.get("segregation_status")))
    ok("7b. rfid_triggered is true", detail.get("rfid_triggered") is True)
    ok("7c. review_status is NEEDS_REVIEW",
       detail.get("review_status") == "NEEDS_REVIEW",
       str(detail.get("review_status")))
    ok(f"7d. and it names {houseA}", detail.get("property_id") == houseA)
    kinds = [e["evidence_type"] for e in (detail.get("evidence") or [])]
    ok("7e. a NON_SEGREGATION_PROOF row is attached",
       kinds.count("NON_SEGREGATION_PROOF") == 1, str(kinds))

    after = totals(rid)
    ok("7f. /summary not_segregated +1",
       after.get("not_segregated", 0) - base.get("not_segregated", 0) == 1,
       f"{base.get('not_segregated')} -> {after.get('not_segregated')}")
    ok("7g. /summary needs_review +1",
       after.get("needs_review", 0) - base.get("needs_review", 0) == 1,
       f"{base.get('needs_review')} -> {after.get('needs_review')}")
    ok("7h. /summary events +1 - one episode, one event",
       after.get("events", 0) - base.get("events", 0) == 1,
       f"{base.get('events')} -> {after.get('events')}")
    ok("7i. /summary segregated unchanged",
       after.get("segregated") == base.get("segregated"),
       f"{base.get('segregated')} -> {after.get('segregated')}")
    ok("7j. /summary evidence +1",
       after.get("evidence", 0) - base.get("evidence", 0) == 1,
       f"{base.get('evidence')} -> {after.get('evidence')}")
    ok("7k. mapped properties unchanged - nothing was surveyed",
       after.get("properties") == base.get("properties")
       and after.get("service_zones") == base.get("service_zones"))

    row = summary_row(rid, houseA)
    ok("7l. v_collection_summary (the map fill) carries the new event",
       bool(row) and row.get("event_id") == event1, json.dumps(row)[:200])
    ok("7m. and it reads NOT_SEGREGATED there too",
       bool(row) and row.get("segregation_status") == "NOT_SEGREGATED")

    _, feed = get("/collection-events/feed/detailed"
                  f"?route_id={rid}&segregation_status=NOT_SEGREGATED&limit=50")
    ids = {r.get("event_id") for r in (feed if isinstance(feed, list) else [])}
    ok("7n. the NOT_SEGREGATED feed filter finds it", event1 in ids)
    _, feed = get("/collection-events/feed/detailed"
                  f"?route_id={rid}&review_status=NEEDS_REVIEW&limit=50")
    ids = {r.get("event_id") for r in (feed if isinstance(feed, list) else [])}
    ok("7o. the NEEDS_REVIEW feed filter finds it", event1 in ids)
    _, feed = get("/collection-events/feed/detailed"
                  f"?route_id={rid}&segregation_status=SEGREGATED&limit=50")
    ids = {r.get("event_id") for r in (feed if isinstance(feed, list) else [])}
    ok("7p. and the SEGREGATED filter correctly does NOT", event1 not in ids)

    _, an = get(f"/analytics/operations?route_id={rid}")
    by_prop = {r["property_id"]: r for r in (an.get("by_property") or [])}
    ok(f"7q. analytics credits the not-segregated event to {houseA}",
       int((by_prop.get(houseA) or {}).get("not_segregated") or 0) >= 1,
       json.dumps(by_prop.get(houseA))[:200])
    ok("7r. analytics rfid_triggered count moved",
       int((an.get("totals") or {}).get("rfid_triggered") or 0) >= 1)

    # -- 8. the tap that lost its race (safety requirement 7) ---------------
    print(f"\n8. house B ({houseB}) - the trigger arrives AFTER the episode closed")
    t = walk_to(t + timedelta(seconds=2), rB, fB, dwell_s + 2)
    _, st = get("/episodes/status")
    e2 = next((a["episode_id"] for a in st["engine"]["active_episodes"]
               if a["property_id"] == houseB), None)
    if not ok(f"8a. an episode opened on {houseB}", e2 is not None,
              json.dumps(st["engine"]["active_episodes"])[:300]):
        return 1
    print(f"     episode {e2}")

    t = walk_to(t + timedelta(seconds=1), away_r, away_f, grace_s + 2)
    time.sleep(grace_s + 1.5)
    _, ep2 = get(f"/episodes/{e2}")
    ok("8b. it closed SEGREGATED, with its event already written",
       ep2.get("state") == "CLOSED"
       and ep2.get("segregation_status") == "SEGREGATED"
       and bool(ep2.get("collection_event_id")),
       f"{ep2.get('state')}/{ep2.get('segregation_status')}")
    event2 = ep2.get("collection_event_id")
    before_close = totals(rid)
    _, d2 = get(f"/collection-events/{event2}")
    ok("8c. the dashboard has already shown it as SEGREGATED",
       d2.get("segregation_status") == "SEGREGATED")

    d = derived_of(trigger(datetime.now(timezone.utc),
                           f"TRG-{RUN}-B", e2)[1])
    ok("8d. inside the late grace the trigger is still APPLIED",
       d.get("applied") is True and d.get("resolution") == "APPLIED",
       json.dumps(d)[:300])
    ok(f"8e. to {houseB} - the property that episode already named",
       d.get("property_id") == houseB, str(d.get("property_id")))

    _, ep2 = get(f"/episodes/{e2}")
    ok("8f. the closed episode is now NOT_SEGREGATED",
       ep2.get("segregation_status") == "NOT_SEGREGATED")
    ok("8g. and it still points at the SAME collection event",
       ep2.get("collection_event_id") == event2,
       f"{event2} -> {ep2.get('collection_event_id')}")

    _, d2 = get(f"/collection-events/{event2}")
    ok("8h. that event was corrected IN PLACE to NOT_SEGREGATED",
       d2.get("segregation_status") == "NOT_SEGREGATED",
       str(d2.get("segregation_status")))
    ok("8i. it is now rfid_triggered", d2.get("rfid_triggered") is True)
    ok("8j. and NEEDS_REVIEW", d2.get("review_status") == "NEEDS_REVIEW")
    kinds = [e["evidence_type"] for e in (d2.get("evidence") or [])]
    ok("8k. exactly one NON_SEGREGATION_PROOF was added, not two",
       kinds.count("NON_SEGREGATION_PROOF") == 1, str(kinds))

    final = totals(rid)
    ok("8l. NO second event was created - the correction is an UPDATE",
       final.get("events") == before_close.get("events"),
       f"{before_close.get('events')} -> {final.get('events')}")
    ok("8m. /summary not_segregated +1 and segregated -1",
       final.get("not_segregated", 0) - before_close.get("not_segregated", 0) == 1
       and before_close.get("segregated", 0) - final.get("segregated", 0) == 1,
       f"seg {before_close.get('segregated')}->{final.get('segregated')}, "
       f"not {before_close.get('not_segregated')}->{final.get('not_segregated')}")

    row = summary_row(rid, houseB)
    ok("8n. v_collection_summary now paints house B NOT_SEGREGATED",
       bool(row) and row.get("segregation_status") == "NOT_SEGREGATED",
       json.dumps(row)[:200])

    d = derived_of(trigger(datetime.now(timezone.utc), f"TRG-{RUN}-B", e2)[1])
    ok("8o. re-announcing the correction is a no-op",
       d.get("resolution") == "DUPLICATE", json.dumps(d)[:200])
    _, d2 = get(f"/collection-events/{event2}")
    kinds = [e["evidence_type"] for e in (d2.get("evidence") or [])]
    ok("8p. and still exactly one proof row",
       kinds.count("NON_SEGREGATION_PROOF") == 1, str(kinds))

    # -- 9. nothing was mapped, unmapped or remapped ------------------------
    print("\n9. the map is exactly as it was")
    _, props_after = get("/properties")
    ok("9a. the property count is unchanged",
       isinstance(props_after, list) and len(props_after) == n_props,
       f"{n_props} -> {len(props_after) if isinstance(props_after, list) else '?'}")
    _, gv = get("/integrations/geovision/status")
    ok("9b. GeoVision still reports it does not decide the property",
       (gv.get("property_association") or {}).get("performed_here") is False)
    _, trg = get("/episodes/triggers?limit=50")
    mine = [r for r in (trg.get("triggers") or trg if isinstance(trg, list)
                        else trg.get("triggers") or [])
            if str(r.get("trigger_id", "")).startswith(f"TRG-{RUN}")]
    applied = [r for r in mine if r.get("applied")]
    review = [r for r in mine if r.get("needs_review")]
    ok("9c. exactly two triggers were applied - one per house",
       len(applied) == 2, str([r["trigger_id"] for r in applied]))
    ok("9d. every refused trigger is preserved for review, none dropped",
       len(review) >= 5, str(len(review)))
    ok("9e. no applied trigger carries a property of its own",
       all("property_id" not in r or r.get("property_id") is None for r in mine))

    print("\n" + "=" * 74)
    print("HTTP SEQUENCE ACTUALLY SENT")
    print("=" * 74)
    for i, line in enumerate(SENT, 1):
        print(f"  {i:3d}  {line}")

    print(f"\nCLEANUP (removes only this run's rows):\n"
          f"  psql wastraq_demo -v source=\"'{SOURCE}'\" "
          f"-f database/cleanup_step2_test.sql")

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
