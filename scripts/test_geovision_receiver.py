#!/usr/bin/env python3
"""
End-to-end test for the GeoVision receiver against a RUNNING WASTRAQ backend.

    scripts/run_backend.sh            # in another terminal
    python3 scripts/test_geovision_receiver.py
    python3 scripts/test_geovision_receiver.py http://192.168.1.23:8000

stdlib only (urllib) - the same transport the GeoVision edge uses, so this
exercises the real HTTP path rather than a TestClient that shares the
process.

It proves, in order:

  1. the existing WASTRAQ demo still works  (regression)
  2. all five event types are accepted
  3. the SAME event_id is deduplicated and creates nothing downstream
  4. malformed payloads are refused with 422
  5. GeoVision cannot assert a property association
  6. a 5 Hz burst is absorbed without heavy per-event work

Every event it sends carries a run-scoped uuid, so it is safe to run against
a live demo database: it never touches properties, collection_events or
evidence, and its rows are identifiable afterwards by their source_id.
"""

from __future__ import annotations

import json
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
STATUS = f"{BASE}/integrations/geovision/status"

RUN = uuid.uuid4().hex[:8]
SOURCE = f"GEOVISION-TEST-{RUN}"
SESSION = f"sess-{RUN}"

FAILURES: list[str] = []


# --- transport ---------------------------------------------------------------
def iso(when: datetime | None = None) -> str:
    moment = when or datetime.now(timezone.utc)
    return (f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}"
            f".{moment.microsecond // 1000:03d}Z")


def post(payload: dict, timeout: float = 5.0) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        EVENTS, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "GeoVision-Integration/1.0"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            elapsed = (time.perf_counter() - started) * 1000
            return response.status, json.loads(response.read() or b"{}"), elapsed
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw), elapsed
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": raw.decode(errors="replace")}, elapsed


def get(url: str, timeout: float = 15.0) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
            ctype = response.headers.get("Content-Type", "")
            if "json" in ctype:
                return response.status, json.loads(raw or b"null")
            return response.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode(errors="replace")


def post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[int, object]:
    """POST to any WASTRAQ endpoint. Used for the read-only /gis/lookup check."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode(errors="replace")


def ok(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'FAIL'}] {label}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# --- payload builders --------------------------------------------------------
def track(event_id: str, track_id: int = 17, **over) -> dict:
    event = {
        "event_type": "TRACK_UPDATE",
        "event_id": event_id,
        "timestamp": iso(),
        "source_id": SOURCE,
        "session_id": SESSION,
        "track_id": track_id,
        "confidence": 0.94,
        "bbox": {"x1": 220, "y1": 90, "x2": 390, "y2": 470},
        "depth_m": 3.54,
        "camera_x_m": -0.82, "camera_y_m": 0.14, "camera_z_m": 3.44,
        "relative_x_m": -0.82, "relative_forward_m": 3.44,
        "depth_valid": True, "depth_status": "OK",
        "is_authorized_picker": True,
        "collector_id": "PICKER-01",
        "identity_confidence": 0.88,
        "gps": {"timestamp": iso(), "latitude": 12.294209,
                "longitude": 76.641702, "accuracy_m": 8.0, "source": "PHONE",
                "age_s": 0.44, "stale": False, "altitude_m": None,
                "speed_mps": None, "hdop": None, "satellites": None,
                "heading_deg": None},
    }
    event.update(over)
    return event


def status_of(url: str = STATUS) -> dict:
    code, body = get(url)
    return body if code == 200 and isinstance(body, dict) else {}


def totals_for(payload: dict, key: str) -> int:
    return int((payload.get("totals") or {}).get(key) or 0)


def main() -> int:  # noqa: C901
    print(f"WASTRAQ GeoVision receiver - live test against {BASE}")
    print(f"run id {RUN}, source_id {SOURCE}\n")

    # -- 0. reachable ---------------------------------------------------------
    code, body = get(f"{BASE}/")
    if code != 200:
        print(f"!! {BASE}/ returned {code}. Is the backend running?")
        return 2

    # -- 1. regression: the existing demo still works -------------------------
    print("1. existing WASTRAQ endpoints (regression)")
    ok("GET /", isinstance(body, dict)
       and "Wastraq" in str(body.get("status", "")), str(body)[:120])

    code, props = get(f"{BASE}/properties")
    ok("GET /properties -> 200", code == 200, str(code))
    n_props = len(props) if isinstance(props, list) else 0
    ok("the 16-property demo lane is intact", n_props >= 16,
       f"got {n_props} properties")

    code, zones = get(f"{BASE}/gis/layers/service-zones")
    n_zones = len((zones or {}).get("features", [])) if isinstance(zones, dict) else 0
    ok("GET /gis/layers/service-zones -> 200", code == 200, str(code))
    ok("service-zone polygons still served", n_zones >= 16, f"got {n_zones} zones")

    for path in ("/summary", "/routes", "/collection-events/feed/detailed",
                 "/collection-events", "/pickers", "/health/db"):
        code, _ = get(f"{BASE}{path}")
        ok(f"GET {path} -> 200", code == 200, str(code))

    code, html = get(f"{BASE}/dashboard")
    ok("GET /dashboard -> 200 html", code == 200 and isinstance(html, str)
       and "<" in html, str(code))

    # PostGIS association still decides properties, and still refuses to guess.
    # POST /gis/lookup is a pure read - it creates no collection event.
    first = props[0] if isinstance(props, list) and props else {}
    if first.get("latitude") and first.get("longitude"):
        code, decision = post_json(
            f"{BASE}/gis/lookup",
            {"latitude": first["latitude"], "longitude": first["longitude"]},
        )
        ok("POST /gis/lookup still decides property association", code == 200,
           str(code))
        ok("its decision is one of the three",
           isinstance(decision, dict) and decision.get("decision") in
           ("AUTO_ASSOCIATED", "AMBIGUOUS", "NO_MATCH"), str(decision)[:160])

    before = status_of()
    ok("GET /integrations/geovision/status -> 200", bool(before))
    ok("status declares it does not associate properties",
       (before.get("property_association") or {}).get("performed_here") is False)
    raw_before = totals_for(before, "raw_events")

    # -- 2. all five event types ---------------------------------------------
    print("\n2. the five event types")
    ids = {name: str(uuid.uuid4()) for name in
           ("track", "rfid", "bound", "clip", "beat")}

    code, ack, ms = post(track(ids["track"]))
    ok("TRACK_UPDATE accepted (202)", code == 202, f"{code} {ack}")
    ok("ack is not a duplicate", ack.get("duplicate") is False)
    ok("ack echoes the event_id", ack.get("event_id") == ids["track"])
    print(f"        ack in {ms:.1f} ms")

    code, ack, _ = post({
        "event_type": "RFID_TAP", "event_id": ids["rfid"], "timestamp": iso(),
        "source_id": SOURCE, "session_id": SESSION,
        "rfid_uid": f"04A1B2{RUN[:2].upper()}", "collector_id": "PICKER-01",
        "track_id": 17, "binding_status": "BOUND", "binding_confidence": 0.91,
        "candidate_track_ids": [17],
    })
    ok("RFID_TAP accepted (202)", code == 202, f"{code} {ack}")

    code, ack, _ = post({
        "event_type": "WORKER_TRACK_BOUND", "event_id": ids["bound"],
        "timestamp": iso(), "source_id": SOURCE, "session_id": SESSION,
        "collector_id": "PICKER-01", "rfid_uid": f"04A1B2{RUN[:2].upper()}",
        "track_id": 17, "confidence": 0.91, "rfid_event_id": ids["rfid"],
    })
    ok("WORKER_TRACK_BOUND accepted (202)", code == 202, f"{code} {ack}")

    now = datetime.now(timezone.utc)
    code, ack, _ = post({
        "event_type": "EVIDENCE_READY", "event_id": ids["clip"],
        "timestamp": iso(), "source_id": SOURCE, "session_id": SESSION,
        "track_id": 17, "rfid_event_id": ids["rfid"],
        "clip_id": f"CLIP-{RUN}",
        "file_path": f"C:\\GeoVision\\backend\\evidence_clips\\CLIP-{RUN}.mp4",
        "start_time": iso(now - timedelta(seconds=13)),
        "end_time": iso(now), "frame_count": 131,
    })
    ok("EVIDENCE_READY accepted (202)", code == 202, f"{code} {ack}")

    code, ack, _ = post({
        "event_type": "HEARTBEAT", "event_id": ids["beat"], "timestamp": iso(),
        "source_id": SOURCE,
        "status": {"realsense_connected": True, "camera_running": True,
                   "tracking_active": True, "depth_available": True,
                   "gps_valid": True, "rfid_available": False,
                   "rfid_mode": "API_INGEST_ONLY", "wastraq_enabled": True,
                   "wastraq_reachable": True, "pending_events": 0},
    })
    ok("HEARTBEAT accepted (202)", code == 202, f"{code} {ack}")

    after_five = status_of()
    ok("all five are stored",
       totals_for(after_five, "raw_events") == raw_before + 5,
       f"{raw_before} -> {totals_for(after_five, 'raw_events')}")
    ok("the RFID tap is normalised", totals_for(after_five, "rfid_taps")
       > totals_for(before, "rfid_taps"))
    ok("the binding is normalised", totals_for(after_five, "worker_bindings")
       > totals_for(before, "worker_bindings"))
    ok("the clip reference is normalised", totals_for(after_five, "evidence_clips")
       > totals_for(before, "evidence_clips"))

    mine = [d for d in (after_five.get("devices") or [])
            if d.get("source_id") == SOURCE]
    ok("the device is registered", len(mine) == 1, f"{len(mine)} rows")
    if mine:
        ok("the heartbeat status was stored",
           (mine[0].get("last_status") or {}).get("rfid_mode") == "API_INGEST_ONLY")
        ok("events_received counted", int(mine[0].get("events_received") or 0) >= 5)

    tracks_mine = [t for t in (after_five.get("active_tracks") or [])
                   if t.get("source_id") == SOURCE]
    ok("the track is active", len(tracks_mine) == 1, f"{len(tracks_mine)} tracks")

    # -- 3. duplicate event_id ------------------------------------------------
    print("\n3. duplicate event_id is idempotent")
    obs_before = tracks_mine[0].get("observation_count") if tracks_mine else None

    code, ack, ms = post(track(ids["track"]))
    ok("resent event_id -> 200, not an error", code == 200, f"{code} {ack}")
    ok("ack says duplicate", ack.get("duplicate") is True, str(ack))
    ok("ack status DUPLICATE", ack.get("status") == "DUPLICATE", str(ack))
    print(f"        ack in {ms:.1f} ms")

    for _ in range(3):
        post(track(ids["track"]))
    for name in ("rfid", "bound", "clip", "beat"):
        code, ack, _ = post(_resend_payload(name, ids, RUN, SOURCE, SESSION))
        ok(f"resent {name} -> 200 duplicate",
           code == 200 and ack.get("duplicate") is True, f"{code} {ack}")

    after_dupes = status_of()
    ok("no new raw event rows",
       totals_for(after_dupes, "raw_events") == totals_for(after_five, "raw_events"),
       f"{totals_for(after_five,'raw_events')} -> {totals_for(after_dupes,'raw_events')}")
    ok("no duplicate rfid tap row",
       totals_for(after_dupes, "rfid_taps") == totals_for(after_five, "rfid_taps"))
    ok("no duplicate binding row",
       totals_for(after_dupes, "worker_bindings")
       == totals_for(after_five, "worker_bindings"))
    ok("no duplicate clip row",
       totals_for(after_dupes, "evidence_clips")
       == totals_for(after_five, "evidence_clips"))

    tracks_mine = [t for t in (after_dupes.get("active_tracks") or [])
                   if t.get("source_id") == SOURCE]
    obs_after = tracks_mine[0].get("observation_count") if tracks_mine else None
    ok("the track's observation_count did not move",
       obs_before is not None and obs_after == obs_before,
       f"{obs_before} -> {obs_after}")

    # -- 4. malformed payloads ------------------------------------------------
    print("\n4. invalid payloads are refused (422)")
    bad: list[tuple[str, dict]] = [
        ("unknown event_type", track(str(uuid.uuid4()), event_type="NONSENSE")),
        ("missing event_type", {k: v for k, v in track(str(uuid.uuid4())).items()
                                if k != "event_type"}),
        ("missing event_id", {k: v for k, v in track(str(uuid.uuid4())).items()
                              if k != "event_id"}),
        ("missing source_id", {k: v for k, v in track(str(uuid.uuid4())).items()
                               if k != "source_id"}),
        ("naive timestamp", track(str(uuid.uuid4()),
                                  timestamp="2026-08-28T07:10:12.341")),
        ("missing track_id", {k: v for k, v in track(str(uuid.uuid4())).items()
                              if k != "track_id"}),
        ("track_id not a number", track(str(uuid.uuid4()), track_id="seventeen")),
        ("latitude out of range", track(str(uuid.uuid4()),
                                        gps={"latitude": 991.0, "longitude": 76.6})),
        ("empty object", {}),
        ("AMBIGUOUS tap naming a track", {
            "event_type": "RFID_TAP", "event_id": str(uuid.uuid4()),
            "timestamp": iso(), "source_id": SOURCE, "rfid_uid": "04A1B2C3",
            "binding_status": "AMBIGUOUS", "track_id": 17,
            "candidate_track_ids": [17, 22]}),
        ("bad binding_status", {
            "event_type": "RFID_TAP", "event_id": str(uuid.uuid4()),
            "timestamp": iso(), "source_id": SOURCE, "rfid_uid": "04A1B2C3",
            "binding_status": "MAYBE"}),
    ]
    for label, payload in bad:
        code, ack, _ = post(payload)
        ok(f"{label} -> 422", code == 422, f"got {code}")

    # -- 5. no property association from the edge -----------------------------
    print("\n5. GeoVision may not assert a property association")
    for field in ("property_id", "authority_property_id", "service_zone_id",
                  "segregation_status", "collection_event_id"):
        code, ack, _ = post(track(str(uuid.uuid4()), **{field: "PROP-003"}))
        ok(f"TRACK_UPDATE carrying {field} -> 422", code == 422, f"got {code}")

    after_bad = status_of()
    ok("nothing invalid reached the database",
       totals_for(after_bad, "raw_events") == totals_for(after_dupes, "raw_events"),
       f"{totals_for(after_dupes,'raw_events')} -> {totals_for(after_bad,'raw_events')}")
    ok("rejections are counted",
       int((after_bad.get("since_restart") or {}).get("rejected", 0)) >= len(bad))

    # -- 6. 5 Hz burst --------------------------------------------------------
    print("\n6. the published rate (~5 Hz per track, two tracks)")
    n = 60
    latencies: list[float] = []
    started = time.perf_counter()
    for i in range(n):
        _, _, ms = post(track(str(uuid.uuid4()), track_id=17 + (i % 2)))
        latencies.append(ms)
    wall = time.perf_counter() - started
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"        {n} events in {wall:.2f}s "
          f"({n / wall:.0f}/s)  p50 {p50:.1f} ms  p95 {p95:.1f} ms")
    ok("sustains well over 10 events/s", (n / wall) > 10.0, f"{n / wall:.1f}/s")
    ok("p95 ack under the sender's 2 s timeout", p95 < 2000.0, f"{p95:.0f} ms")

    final = status_of()
    burst_tracks = [t for t in (final.get("active_tracks") or [])
                    if t.get("source_id") == SOURCE]
    ok("two tracks, not 60 rows - state is upserted", len(burst_tracks) == 2,
       f"{len(burst_tracks)} track rows")

    # -- 7. regression again, after all that ---------------------------------
    print("\n7. existing endpoints still fine afterwards")
    for path in ("/", "/summary", "/properties", "/collection-events/feed/detailed",
                 "/gis/layers/service-zones", "/dashboard"):
        code, _ = get(f"{BASE}{path}")
        ok(f"GET {path} -> 200", code == 200, str(code))

    print()
    print(f"source_id used: {SOURCE}   (clean up with: "
          f"DELETE FROM geovision_raw_events WHERE source_id = '{SOURCE}';)")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _resend_payload(name: str, ids: dict, run: str, source: str,
                    session: str) -> dict:
    """Rebuild each event with its ORIGINAL event_id but a fresh timestamp -
    which is exactly what the edge's retry queue does."""
    now = datetime.now(timezone.utc)
    stamp = (f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z")
    if name == "rfid":
        return {"event_type": "RFID_TAP", "event_id": ids["rfid"],
                "timestamp": stamp, "source_id": source, "session_id": session,
                "rfid_uid": f"04A1B2{run[:2].upper()}",
                "collector_id": "PICKER-01", "track_id": 17,
                "binding_status": "BOUND", "binding_confidence": 0.91,
                "candidate_track_ids": [17]}
    if name == "bound":
        return {"event_type": "WORKER_TRACK_BOUND", "event_id": ids["bound"],
                "timestamp": stamp, "source_id": source, "session_id": session,
                "collector_id": "PICKER-01",
                "rfid_uid": f"04A1B2{run[:2].upper()}", "track_id": 17,
                "confidence": 0.91, "rfid_event_id": ids["rfid"]}
    if name == "clip":
        return {"event_type": "EVIDENCE_READY", "event_id": ids["clip"],
                "timestamp": stamp, "source_id": source, "session_id": session,
                "track_id": 17, "rfid_event_id": ids["rfid"],
                "clip_id": f"CLIP-{run}",
                "file_path": f"C:\\GeoVision\\backend\\evidence_clips\\CLIP-{run}.mp4",
                "frame_count": 131}
    return {"event_type": "HEARTBEAT", "event_id": ids["beat"],
            "timestamp": stamp, "source_id": source,
            "status": {"camera_running": True}}


if __name__ == "__main__":
    sys.exit(main())
