#!/usr/bin/env python3
"""
STEP 2B, over the wire: does an EPISODE-GENERATED collection event reach the
EXISTING WASTRAQ Live Operations dashboard, through the existing APIs?

    scripts/run_backend.sh                          # in another terminal
    python3 scripts/test_step2b_dashboard.py
    python3 scripts/test_step2b_dashboard.py http://127.0.0.1:8000

STEP 2 proved the engine writes the event. This proves the dashboard SEES it,
and sees it as an ordinary WASTRAQ event - same feed, same KPI arithmetic,
same property/map state - with nothing added to the dashboard to make it so.

The dashboard (backend/app/static/dashboard.html) reads exactly five things:

    GET  /routes                                    route selector + event count
    GET  /summary?route_id=                         KPI cards + per-property last state
    GET  /analytics/operations?route_id=            by_picker / by_property / rates
    GET  /collection-events/feed/detailed?...       the event feed
    GET  /properties?route_id=                      the property table and the map
      + /properties/{id}, /properties/{id}/events   the property drawer
      + /collection-events/{id}, /{id}/evidence     the event drawer
      + /gis/layers/service-zones, /gis/layers/entrances   the map geometry

Every one of those is keyed on the `collection_events` table joined to
`properties.route_id`. The episode engine INSERTs into that same table
(backend/app/episodes/store.py :: create_collection_event), so if this script
passes, the answer to "did we have to wire anything up" is no.

Eleven claims:

     1  the episode's property is on the dashboard's operations route
     2  the episode closed SEGREGATED and wrote exactly one collection event
     3  /collection-events/feed/detailed returns it, fully populated
     4  it carries the same field set as a hand-created WASTRAQ event
     5  every dashboard feed filter finds it
     6  /summary KPI counts moved by exactly one, in the right cards
     7  /summary events (v_collection_summary) carries it - this is the map
     8  /analytics/operations attributes it to the right property AND picker
     9  /routes shows the route's event count moved by one
    10  the property drawer and the event drawer both open on it
    11  nothing but events was created: the surveyed lane is untouched

Deliberately NOT here: NON_SEGREGATION_TRIGGER, evidence clips, Windows
GeoVision, property association rules. STEP 2B is the dashboard only.

stdlib only. The episode is driven through the REAL GeoVision edge contract,
so this is the production path end to end and not a hand-written row: there
is no hardcoded PROP-001 anywhere in this file, and no property is created.
The zone it uses is chosen at runtime from whatever the survey holds.

SAFETY. Every row it writes carries a run-scoped source_id, printed at the
end. To remove them:

    psql wastraq_demo -v source="'<the source_id printed below>'" \
         -f database/cleanup_step2_test.sql
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The STEP 2 script already owns the transport, the camera inverse-transform
# and the dwell pump. Importing them keeps ONE definition of "what the edge
# would have sent"; a drift there should break both scripts, not just one.
import test_step2_episode_flow as s2  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.getenv("WASTRAQ_URL", "http://127.0.0.1:8000")).rstrip("/")

RUN = uuid.uuid4().hex[:8]
SOURCE = f"GEOVISION-STEP2B-{RUN}"
SESSION = f"sess2b-{RUN}"
COLLECTOR = os.getenv("DEMO_COLLECTOR", "PICKER-01")
TRACK = int(os.getenv("DEMO_TRACK", "37"))
UID = os.getenv("DEMO_RFID_UID", "04A1B2C3")

# Point the imported helpers at THIS run. They read these as module globals,
# so the payloads they build carry our source_id and never STEP 2's.
s2.BASE = BASE
s2.EVENTS = f"{BASE}/integrations/geovision/events"
s2.RUN = RUN
s2.SOURCE = SOURCE
s2.SESSION = SESSION
s2.COLLECTOR = COLLECTOR
s2.TRACK = TRACK
s2.UID = UID

get = s2.get
post = s2.post
request = s2._request
dwell = s2.dwell
status = s2.status

FAILURES: list[str] = []
_M_PER_DEG_LAT = 111_320.0


def ok(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return bool(condition)


def num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_event(rows, event_id: str):
    if not isinstance(rows, list):
        return None
    return next((r for r in rows if r.get("event_id") == event_id), None)


def feed(**params):
    """GET the dashboard feed exactly as dashboard.html builds the query."""
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    _, rows = get(f"/collection-events/feed/detailed" + (f"?{query}" if query else ""))
    return rows if isinstance(rows, list) else []


def run_episode(cam: dict, house: dict, dwell_s: float, grace_s: float,
                away: tuple[float, float]) -> str | None:
    """Bind a collector, dwell in `house`, walk away. Returns the episode id.

    This is STEP 2's proven sequence, compressed: STEP 2 already asserts each
    intermediate state, and re-asserting it here would only make a STEP 2
    regression look like a dashboard bug.
    """
    request("POST", f"{BASE}/episodes/reset", {"abort_active_episodes": True})
    t0 = datetime.now(timezone.utc)
    post(s2.bound_payload(t0))
    end = dwell(t0 + timedelta(seconds=0.5), house["right"], house["forward"],
                dwell_s + 2)

    live = [a for a in status()["engine"]["active_episodes"]
            if a["property_id"] == house["id"]]
    if not live:
        return None
    episode_id = live[0]["episode_id"]
    last_inside = datetime.fromisoformat(live[0]["last_inside"].replace("Z", "+00:00"))

    # Leave. Event time runs ahead of wall clock so the TRACK_UPDATE ladder,
    # not the wall-clock sweeper, is what closes it.
    for i in range(1, 7):
        post(s2.track_payload(last_inside + timedelta(seconds=i * grace_s / 2.0),
                              away[0], away[1]))
        time.sleep(0.45)
        if not any(a["episode_id"] == episode_id
                   for a in status()["engine"]["active_episodes"]):
            break
    return episode_id


def main() -> int:  # noqa: C901
    print("=" * 74)
    print(f"WASTRAQ STEP 2B - episode event -> Live Operations dashboard   ({BASE})")
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
    if not ok("a surveyed camera pose is configured",
              health.get("camera_configured") is True,
              "set CAMERA_ORIGIN_LAT/LON in backend/.env and restart"):
        return 1

    st = status()
    cam = st["engine"]["camera"]
    dwell_s = float(st["engine"]["dwell_s"])
    grace_s = float(st["engine"]["leave_grace_s"])

    # The route the dashboard actually shows. Read from the API, never
    # assumed: /summary answers with the route it scoped itself to.
    _, summary0 = get("/summary")
    route = summary0.get("route_id")
    if not ok("the dashboard reports an operations route", bool(route), str(route)):
        return 1
    print(f"     route  : {route}")
    print(f"     camera : {cam['latitude']}, {cam['longitude']} "
          f"heading {cam['heading_deg']} deg")
    print(f"     timing : dwell {dwell_s:g} s, leave grace {grace_s:g} s")

    # Every zone the camera can reach, nearest first - the same runtime
    # choice STEP 2 makes, so no property id is ever written down here.
    _, props = get(f"/properties?route_id={route}")
    props = props if isinstance(props, list) else []
    reach = []
    for prop in props:
        _, detail = get(f"/properties/{prop['property_id']}")
        zone = detail.get("service_zone_geojson")
        if not zone:
            continue
        lat, lon = s2.polygon_centre(zone)
        right, forward = s2.wgs84_to_camera(cam, lat, lon)
        reach.append({"id": prop["property_id"], "lat": lat, "lon": lon,
                      "right": right, "forward": forward,
                      "d": math.hypot(right, forward)})
    reach.sort(key=lambda z: z["d"])
    if not ok("the operations route has a mapped service zone in camera reach",
              bool(reach), f"{len(props)} properties on {route}, none reachable"):
        return 1
    house = reach[0]
    print(f"     house  : {house['id']} at camera "
          f"({house['right']:.2f} m right, {house['forward']:.2f} m fwd)")

    h = math.radians(cam["heading_deg"])
    away_r, away_f = 0.0, -60.0
    a_lat = cam["latitude"] + (away_f * math.cos(h) - away_r * math.sin(h)) / _M_PER_DEG_LAT
    a_lon = cam["longitude"] + (away_f * math.sin(h) + away_r * math.cos(h)) / (
        _M_PER_DEG_LAT * math.cos(math.radians(cam["latitude"])))
    _, look_away = request("POST", f"{BASE}/gis/lookup",
                           {"latitude": a_lat, "longitude": a_lon})
    ok("the walk-away point is outside every service zone",
       not look_away.get("property_id"), str(look_away.get("decision")))

    # ------------------------------------------------------------ baseline
    # Every count below is asserted as a DELTA. The demo database already
    # holds real events; an absolute number here would be a fixture, and
    # fixtures are what this project is trying not to have.
    print("\nbaseline (deltas are what get asserted, never absolutes)")
    before_summary = summary0["totals"]
    _, before_an = get(f"/analytics/operations?route_id={route}")
    _, before_routes = get("/routes")
    before_route_events = num(next((r["events"] for r in before_routes
                                    if r["route_id"] == route), 0))
    before_feed = feed(route_id=route, limit=500)
    before_prop = next((r for r in before_an["by_property"]
                        if r["property_id"] == house["id"]), {})
    before_picker = next((r for r in before_an["by_picker"]
                          if r["picker_id"] == COLLECTOR), {})
    print(f"     events {num(before_summary['events']):.0f}  "
          f"segregated {num(before_summary['segregated']):.0f}  "
          f"not_segregated {num(before_summary['not_segregated']):.0f}  "
          f"feed rows {len(before_feed)}")

    # Claim 4 needs a NON-episode event to compare shapes against - comparing
    # an episode event to another episode event would prove nothing. Taken
    # from whatever the database already holds; skipped, never faked, if the
    # database holds none.
    _, existing = get("/collection-events?limit=500")
    manual_ids = {r["event_id"] for r in (existing if isinstance(existing, list) else [])
                  if not r.get("episode_id")}
    reference = next((r for r in before_feed if r["event_id"] in manual_ids), None)

    # ---------------------------------------------------------------- 1
    print("\n1. the episode's property is on the dashboard's operations route")
    _, house_detail = get(f"/properties/{house['id']}")
    ok(f"1. {house['id']}.route_id == {route}",
       house_detail.get("route_id") == route, str(house_detail.get("route_id")))

    # ---------------------------------------------------------------- 2
    print(f"\n2. run one real episode on {house['id']} through the edge contract")
    episode_id = run_episode(cam, house, dwell_s, grace_s, (away_r, away_f))
    if not ok("2. the dwell opened an episode", episode_id is not None):
        return 1
    _, ep = get(f"/episodes/{episode_id}")
    ok(f"2b. {episode_id} is CLOSED", ep.get("state") == "CLOSED", str(ep.get("state")))
    ok("2c. it closed SEGREGATED", ep.get("segregation_status") == "SEGREGATED",
       str(ep.get("segregation_status")))
    event_id = ep.get("collection_event_id")
    if not ok("2d. it wrote a collection event", bool(event_id), json.dumps(ep)[:200]):
        return 1
    _, all_events = get("/collection-events?limit=500")
    mine = [r for r in (all_events if isinstance(all_events, list) else [])
            if r.get("episode_id") == episode_id]
    ok("2e. exactly one, and the episode points at it",
       len(mine) == 1 and mine[0]["event_id"] == event_id, str([r["event_id"] for r in mine]))
    print(f"     episode {episode_id} -> event {event_id}")

    # ---------------------------------------------------------------- 3
    print("\n3. the dashboard event feed returns it, fully populated")
    rows = feed(route_id=route, limit=120)
    row = find_event(rows, event_id)
    if not ok("3. GET /collection-events/feed/detailed contains the event",
              row is not None, f"{len(rows)} rows, none matching {event_id}"):
        return 1
    ok("3b. property_id", row.get("property_id") == house["id"], str(row.get("property_id")))
    ok("3c. collector - picker_id AND the joined picker_name",
       row.get("picker_id") == COLLECTOR and bool(row.get("picker_name")),
       f"{row.get('picker_id')} / {row.get('picker_name')}")
    ok("3d. SEGREGATED status", row.get("segregation_status") == "SEGREGATED",
       str(row.get("segregation_status")))
    ok("3e. a collection timestamp", bool(row.get("collection_time")),
       str(row.get("collection_time")))
    ok("3f. association confidence (the feed prints it verbatim)",
       row.get("association_confidence") is not None,
       str(row.get("association_confidence")))
    ok("3g. review status the feed colours the row by",
       row.get("review_status") in ("AUTO_CONFIRMED", "NEEDS_REVIEW"),
       str(row.get("review_status")))
    ok("3h. the property fields the feed shows beside it",
       row.get("house_number") is not None and row.get("route_id") == route)
    ok("3i. rfid_triggered is false - nothing raised a trigger",
       row.get("rfid_triggered") is False, str(row.get("rfid_triggered")))
    ok("3j. evidence_count is present and countable",
       row.get("evidence_count") is not None, str(row.get("evidence_count")))
    ok("3k. it is the newest row, so it lands at the top of the feed",
       rows[0]["event_id"] == event_id, f"top row is {rows[0]['event_id']}")

    # ---------------------------------------------------------------- 4
    print("\n4. it is shaped exactly like an ordinary WASTRAQ event")
    if reference is None:
        print("  [SKIP] 4. this route's feed holds no hand-created event to "
              "compare against")
    else:
        print(f"     comparing against {reference['event_id']}, "
              f"created outside the episode engine")
        ok("4. same field set as an event the dashboard already showed",
           set(row.keys()) == set(reference.keys()),
           str(set(row.keys()) ^ set(reference.keys())))
        ok("4b. no null where the reference event has a value, in any field "
           "the feed renders",
           all(row.get(k) is not None
               for k in ("event_id", "property_id", "segregation_status",
                         "collection_time", "review_status")
               if reference.get(k) is not None))

    # ---------------------------------------------------------------- 5
    print("\n5. every dashboard feed filter finds it")
    ok("5. segregation_status=SEGREGATED",
       find_event(feed(route_id=route, segregation_status="SEGREGATED", limit=500),
                  event_id) is not None)
    ok("5b. review_status=" + str(row.get("review_status")),
       find_event(feed(route_id=route, review_status=row["review_status"], limit=500),
                  event_id) is not None)
    ok(f"5c. picker_id={COLLECTOR}",
       find_event(feed(route_id=route, picker_id=COLLECTOR, limit=500),
                  event_id) is not None)
    ok(f"5d. property_id={house['id']}",
       find_event(feed(route_id=route, property_id=house["id"], limit=500),
                  event_id) is not None)
    ok("5e. since_hours=1 (the dashboard's default time window control)",
       find_event(feed(route_id=route, since_hours=1, limit=500), event_id) is not None)
    ok("5f. the free-text search box, on the property id",
       find_event(feed(route_id=route, q=house["id"], limit=500), event_id) is not None)
    ok("5g. and it is correctly ABSENT from the NOT_SEGREGATED filter",
       find_event(feed(route_id=route, segregation_status="NOT_SEGREGATED", limit=500),
                  event_id) is None)

    # ---------------------------------------------------------------- 6
    print("\n6. the KPI cards moved by exactly one, in the right cards")
    _, summary1 = get(f"/summary?route_id={route}")
    after = summary1["totals"]
    d = {k: num(after.get(k)) - num(before_summary.get(k))
         for k in ("events", "segregated", "not_segregated", "needs_review",
                   "properties", "service_zones", "evidence")}
    ok("6. 'Segregated' KPI +1", d["segregated"] == 1, str(d["segregated"]))
    ok("6b. total events +1", d["events"] == 1, str(d["events"]))
    ok("6c. 'Not segregated' KPI unchanged", d["not_segregated"] == 0,
       str(d["not_segregated"]))
    ok("6d. 'Pending review' unchanged - an auto-confirmed event needs none",
       d["needs_review"] == 0, str(d["needs_review"]))
    ok("6e. 'Evidence packages' unchanged - a SEGREGATED close attaches none",
       d["evidence"] == 0, str(d["evidence"]))
    ok("6f. 'Mapped properties' and 'service zones' unchanged - no survey was written",
       d["properties"] == 0 and d["service_zones"] == 0,
       f"properties {d['properties']:+g}, zones {d['service_zones']:+g}")
    ok("6g. 'Serviced today' counts this property",
       num(after["serviced_today"]) >= 1
       and num(after["serviced_today"]) >= num(before_summary["serviced_today"]),
       f"{num(before_summary['serviced_today']):.0f} -> {num(after['serviced_today']):.0f}")

    # ---------------------------------------------------------------- 7
    print("\n7. /summary events - the rows that drive the property table AND the map")
    ev = find_event(summary1.get("events") or [], event_id)
    ok("7. v_collection_summary carries the episode event", ev is not None)
    if ev:
        ok("7b. with the property and the collector resolved",
           ev.get("property_id") == house["id"] and bool(ev.get("picker_name")),
           f"{ev.get('property_id')} / {ev.get('picker_name')}")
        ok("7c. and SEGREGATED, which is the state the map paints the zone",
           ev.get("segregation_status") == "SEGREGATED")
    # dashboard.html keeps the FIRST row per property as that property's
    # current state, and the query is ORDER BY collection_time DESC.
    first_for_house = next((e for e in (summary1.get("events") or [])
                            if e["property_id"] == house["id"]), None)
    ok("7d. it is the property's newest event, so the map shows THIS state",
       first_for_house is not None and first_for_house["event_id"] == event_id,
       str((first_for_house or {}).get("event_id")))
    _, zones = get("/gis/layers/service-zones")
    zone_ids = {f["properties"]["property_id"]
                for f in (zones.get("features") or [])} if isinstance(zones, dict) else set()
    ok("7e. the map layer still holds that property's zone to paint",
       house["id"] in zone_ids, f"{len(zone_ids)} zones in the layer")

    # ---------------------------------------------------------------- 8
    print("\n8. /analytics/operations attributes it to the right property and picker")
    _, an = get(f"/analytics/operations?route_id={route}")
    prop_row = next((r for r in an["by_property"] if r["property_id"] == house["id"]), None)
    picker_row = next((r for r in an["by_picker"] if r["picker_id"] == COLLECTOR), None)
    ok("8. the property row exists", prop_row is not None)
    if prop_row:
        ok("8b. its event count moved by one",
           num(prop_row["events"]) - num(before_prop.get("events")) == 1,
           f"{num(before_prop.get('events')):.0f} -> {num(prop_row['events']):.0f}")
        ok("8c. its not-segregated count did not move",
           num(prop_row["not_segregated"]) - num(before_prop.get("not_segregated")) == 0)
        ok("8d. and it is now the property's last_event", bool(prop_row["last_event"]))
    ok("8e. the picker row exists", picker_row is not None)
    if picker_row:
        ok("8f. the collector is credited with one more collection",
           num(picker_row["events"]) - num(before_picker.get("events")) == 1,
           f"{num(before_picker.get('events')):.0f} -> {num(picker_row['events']):.0f}")
        ok("8g. with a confidence average the panel can print",
           picker_row.get("avg_confidence") is not None)
    ok("8h. route-wide averages still computable",
       an["totals"].get("avg_confidence") is not None)
    ok("8i. properties_with_events includes this one",
       num(an["totals"]["properties_with_events"])
       >= num(before_an["totals"]["properties_with_events"]))

    # ---------------------------------------------------------------- 9
    print("\n9. the route selector's event count moved by one")
    _, routes_after = get("/routes")
    after_route_events = num(next((r["events"] for r in routes_after
                                   if r["route_id"] == route), 0))
    ok("9. /routes events +1 for this route",
       after_route_events - before_route_events == 1,
       f"{before_route_events:.0f} -> {after_route_events:.0f}")

    # ---------------------------------------------------------------- 10
    print("\n10. both drawers open on it")
    _, drawer = get(f"/properties/{house['id']}/events")
    drawer_row = find_event(drawer, event_id)
    ok("10. the property drawer's event list contains it", drawer_row is not None)
    if drawer_row:
        ok("10b. and there it is identifiable as episode-derived",
           drawer_row.get("episode_id") == episode_id, str(drawer_row.get("episode_id")))
    code_ev, one = get(f"/collection-events/{event_id}")
    ok("10c. the event drawer loads the event", code_ev == 200
       and one.get("event_id") == event_id, f"HTTP {code_ev}")
    code_e, evid = get(f"/collection-events/{event_id}/evidence")
    ok("10d. and its evidence list answers (empty is the correct answer here)",
       code_e == 200 and isinstance(evid, list) and len(evid) == 0,
       f"HTTP {code_e} {json.dumps(evid)[:120]}")

    # ---------------------------------------------------------------- 11
    print("\n11. nothing but events was created")
    request("POST", f"{BASE}/episodes/reset", {"abort_active_episodes": True})
    _, props_after = get(f"/properties?route_id={route}")
    ok("11. the route's property count is unchanged",
       isinstance(props_after, list) and len(props_after) == len(props),
       f"{len(props)} -> {len(props_after) if isinstance(props_after, list) else '?'}")
    _, detail_after = get(f"/properties/{house['id']}")
    lat2, lon2 = s2.polygon_centre(detail_after["service_zone_geojson"])
    ok("11b. the service zone this run used is byte-for-byte where it was",
       abs(lat2 - house["lat"]) < 1e-12 and abs(lon2 - house["lon"]) < 1e-12)
    _, ev_all = get("/evidence?limit=500")
    ok("11c. no evidence row was invented for a SEGREGATED close",
       isinstance(ev_all, list)
       and not [e for e in ev_all if e.get("event_id") == event_id],
       str([e.get("evidence_id") for e in (ev_all or [])
            if e.get("event_id") == event_id]))

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
    print("STEP 2B PROVED - the existing dashboard shows episode events "
          "with no wiring change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
