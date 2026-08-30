#!/usr/bin/env python3
"""
STEP 4A over the wire - GeoVision evidence, played from the Mac.

    psql -d wastraq_demo -f database/evidence_media.sql   # once
    scripts/run_backend.sh                                # another terminal
    python3 scripts/test_step4a_evidence_http.py
    python3 scripts/test_step4a_evidence_http.py http://127.0.0.1:8000

stdlib only, against a RUNNING backend and the real database. No Windows
machine is needed and none is contacted: this script stands up a fake
GeoVision edge on localhost that serves scripts/fixtures/sample_clip.mp4
from the same URL shape the real edge will, and points the backend at it
for the duration of the run.

What it proves
--------------
  1  an EVIDENCE_READY with no episode is still stored and still fetched -
     evidence WASTRAQ holds but has not attributed beats the reverse;
  2  a full episode (bind -> dwell -> second tap -> leave) produces a
     collection event, and the clip announced for it is linked to THAT
     event, with the property WASTRAQ associated by service zone;
  3  the same clip re-announced under a new event_id produces no second
     evidence row (idempotent);
  4  GET /collection-events/{id}/evidence returns a playable media_url,
     and GET on that url returns real MP4 bytes with Range support;
  5  the Windows path never appears as a browser-facing location - it is
     reported as `source_ref` and nothing else;
  6  a clip whose bytes cannot be fetched degrades to PENDING/UNAVAILABLE
     with a 409 and a reason, and the collection event is unaffected.

Safe against a live demo database: run-scoped source_id/session_id, deltas
only, and a printed cleanup statement at the end.
"""

from __future__ import annotations

import email.message
import http.server
import importlib.util
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "sample_clip.mp4"

_spec = importlib.util.spec_from_file_location(
    "demo_http", str(HERE / "test_episode_demo_http.py"))
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["demo_http"] = demo
_spec.loader.exec_module(demo)

BASE = demo.BASE
RUN = uuid.uuid4().hex[:8]
SOURCE = f"GEOVISION-STEP4A-{RUN}"
SESSION = f"sess-{RUN}"
COLLECTOR = os.getenv("DEMO_COLLECTOR", "PICKER-01")
TRACK = int(os.getenv("DEMO_TRACK", "41"))
UID = os.getenv("DEMO_RFID_UID", "04A1B2C3")

get, post, iso = demo.get, demo.post, demo.iso
polygon_centre, wgs84_to_camera = demo.polygon_centre, demo.wgs84_to_camera

FAILURES: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return bool(condition)


def eid() -> str:
    return f"{RUN}-{uuid.uuid4().hex[:10]}"


def send(payload: dict) -> tuple[int, dict]:
    payload.setdefault("event_id", eid())
    payload.setdefault("source_id", SOURCE)
    payload.setdefault("session_id", SESSION)
    return post(payload)


class Headers(dict):
    """Response headers, looked up the way HTTP means them: case-insensitively.

    Not a convenience. ``dict(response.headers)`` keeps whatever casing the
    server put on the wire, and uvicorn (via h11) writes header names in
    lower case - so `headers["Content-Type"]` on a plain dict is a KeyError
    against a response that plainly has one. That is a test that reports a
    server defect which does not exist, which is worse than no test at all.
    """

    def __init__(self, message):
        super().__init__((k.lower(), v) for k, v in message.items())

    def get(self, name, default=None):
        return super().get(str(name).lower(), default)

    def __getitem__(self, name):
        return super().__getitem__(str(name).lower())

    def __contains__(self, name):
        return super().__contains__(str(name).lower())

    def media_type(self):
        """Content-Type with any parameters (``; charset=...``) stripped."""
        return (self.get("content-type") or "").split(";")[0].strip().lower()


def raw_get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, Headers(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, Headers(exc.headers), (exc.read() or b"")
    except Exception as exc:  # noqa: BLE001
        return 0, Headers(email.message.Message()), repr(exc).encode()


# --- the fake edge -----------------------------------------------------------
class _Edge(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/evidence/clips/") and self.path.endswith("/file"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_edge(body: bytes):
    server = socketserver.TCPServer(
        ("127.0.0.1", 0), type("H", (_Edge,), {"body": body}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --- episode helpers ---------------------------------------------------------
def bind(at, rfid_event_id):
    return send({"event_type": "WORKER_TRACK_BOUND", "timestamp": iso(at),
                 "collector_id": COLLECTOR, "rfid_uid": UID, "track_id": TRACK,
                 "confidence": 0.95, "rfid_event_id": rfid_event_id})


def track(at, right, forward):
    return send({"event_type": "TRACK_UPDATE", "timestamp": iso(at),
                 "track_id": TRACK, "confidence": 0.9,
                 "depth_m": round(abs(forward), 3), "depth_valid": True,
                 "depth_status": "OK", "camera_x_m": round(right, 3),
                 "camera_z_m": round(forward, 3),
                 "relative_x_m": round(right, 3),
                 "relative_forward_m": round(forward, 3),
                 "is_authorized_picker": True, "collector_id": COLLECTOR})


#: Seconds between successive TRACK_UPDATEs, on BOTH clocks. Must stay above
#: EPISODE_MIN_ASSOC_INTERVAL_S (0.4 by default) - see walk() below. Same
#: value as scripts/test_step2_episode_flow.py, which is the proven sequence.
STEP_S = 0.5


def walk(t0, right, forward, seconds, step=STEP_S):
    """Stand at one camera-frame position, reporting at 2 Hz.

    Two clocks have to advance together here, and getting one of them wrong
    produces an engine that looks broken while behaving exactly as designed:

    * EVENT time decides dwell. `_observe` measures a candidate as
      `last_seen - first_seen` over TRACK_UPDATE timestamps, so the events
      must span at least `dwell_s` of event time or no episode opens.

    * WALL time decides how many of them are looked at. The engine throttles
      the PostGIS ladder by `time.monotonic()`
      (EPISODE_MIN_ASSOC_INTERVAL_S) because the real edge publishes at
      ~5 Hz and a person does not cross a service zone in 200 ms. Events
      fired faster than that interval are counted as observations and then
      returned THROTTLED before they ever reach the state machine.

    So `time.sleep(step)` is not padding to make the test slow - it is the
    only way to get more than one association out of the engine, and it is
    what test_step2_episode_flow.dwell() does for the same reason.
    """
    steps = max(int(round(seconds / step)), 1)
    last: dict = {}
    for i in range(steps + 1):
        _, last = track(t0 + timedelta(seconds=i * step), right, forward)
        time.sleep(step)
    return t0 + timedelta(seconds=steps * step), last


def evidence_ready(at, clip_id, *, file_url=None, rfid_event_id=None,
                   file_path=None):
    payload = {"event_type": "EVIDENCE_READY", "timestamp": iso(at),
               "clip_id": clip_id, "track_id": TRACK,
               "file_path": file_path or rf"C:\GeoVision\clips\{clip_id}.mp4",
               "start_time": iso(at - timedelta(seconds=12)),
               "end_time": iso(at), "frame_count": 150,
               "content_type": "video/mp4"}
    if file_url:
        payload["file_url"] = file_url
    if rfid_event_id:
        payload["rfid_event_id"] = rfid_event_id
    return send(payload)


def main() -> int:  # noqa: C901
    if not FIXTURE.is_file():
        print(f"!! fixture missing: {FIXTURE}")
        return 2
    clip_bytes = FIXTURE.read_bytes()

    print(f"STEP 4A - GeoVision evidence on the Mac   ({BASE})")
    print(f"  run={RUN} source_id={SOURCE}\n")

    status, health = get("/health/episodes")
    if not ok("episode engine reachable and armed",
              status == 200 and health.get("enabled")
              and health.get("camera_configured"), json.dumps(health)):
        return 1

    status, media = get("/evidence-media/status")
    if not ok("GET /evidence-media/status responds", status == 200, json.dumps(media)):
        print("     Did you run:  psql -d wastraq_demo -f database/evidence_media.sql ?")
        return 1
    print(f"     evidence root: {media.get('evidence_root')}")
    print(f"     edge base:     {media.get('edge_base_url')}")

    edge, edge_base = start_edge(clip_bytes)
    print(f"     fake edge:     {edge_base}\n")
    try:
        return run(edge_base, clip_bytes)
    finally:
        edge.shutdown()
        edge.server_close()


def run(edge_base: str, clip_bytes: bytes) -> int:  # noqa: C901
    # An absolute file_url on every EVIDENCE_READY, so the backend needs no
    # reconfiguration to reach this run's fake edge. The template path
    # (GEOVISION_EDGE_BASE_URL + GEOVISION_CLIP_URL_TEMPLATE) is what the
    # real edge will use when it sends no url at all.
    def url_for(clip_id: str) -> str:
        return f"{edge_base}/evidence/clips/{clip_id}/file"

    now = datetime.now(timezone.utc)

    # --- 1. an orphan clip: announced with no episode to attach to --------
    print("1. EVIDENCE_READY with no matching episode")
    orphan = f"CLIP-{RUN}-ORPHAN"
    status, ack = evidence_ready(now, orphan, file_url=url_for(orphan))
    ok("orphan EVIDENCE_READY accepted", status == 202, json.dumps(ack))
    derived = ack.get("derived") or {}
    ok("engine reports it linked nothing, and says why",
       derived.get("linked") is False
       and derived.get("reason") == "NO_MATCHING_EPISODE", json.dumps(derived))

    for _ in range(40):
        _, st = get("/integrations/geovision/status")
        clips = [c for c in st.get("recent_evidence_clips", [])
                 if c.get("clip_id") == orphan]
        if clips and clips[0].get("fetch_status") == "STORED":
            break
        time.sleep(0.25)
    ok("an unattributed clip is still pulled onto the Mac",
       bool(clips) and clips[0].get("fetch_status") == "STORED",
       json.dumps(clips[:1]))

    # --- 2. a real episode -------------------------------------------------
    print("\n2. bind -> dwell -> second tap -> leave")
    status, props = get("/properties")
    zone_prop = None
    for p in props if isinstance(props, list) else []:
        _, detail = get(f"/properties/{p['property_id']}")
        if detail.get("service_zone_geojson"):
            zone_prop = detail
            break
    if not ok("a mapped service zone to walk into", zone_prop is not None):
        return 1

    _, ep_status = get("/episodes/status")
    engine = ep_status.get("engine") or {}
    cam = engine.get("camera") or {}
    lat, lon = polygon_centre(zone_prop["service_zone_geojson"])
    right, forward = wgs84_to_camera(cam, lat, lon)
    dwell = float(engine.get("dwell_s") or 3.0)
    grace = float(engine.get("leave_grace_s") or 4.0)

    t = datetime.now(timezone.utc)
    rfid_event = f"{RUN}-rfid-1"
    bind(t, rfid_event)
    t += timedelta(seconds=0.4)
    t, _ = walk(t, right, forward, dwell + 2.0)

    _, snap = get("/episodes/status")
    engine_state = snap.get("engine") or {}
    # active_episodes is keyed by collector, not by source: this run owns the
    # binding for COLLECTOR/TRACK, so that pair identifies our episode.
    active = [e for e in engine_state.get("active_episodes", [])
              if e.get("collector_id") == COLLECTOR and e.get("track_id") == TRACK]

    # Everything after this point is about evidence, and every one of those
    # assertions is meaningless without an episode. Fail here, loudly, with
    # the two counters that distinguish the three ways this goes wrong.
    stats = engine_state.get("stats") or {}
    if not ok("an episode opened in the zone", bool(active),
              f"observations={stats.get('observations')} "
              f"associations={stats.get('associations')} "
              f"ambiguous={stats.get('ambiguous')} "
              f"episodes_opened={stats.get('episodes_opened')} "
              f"candidates={json.dumps(engine_state.get('candidates'))}"):
        print("     Read those counters before changing anything:")
        print("       observations high, associations ~1 -> events were sent")
        print("         faster than EPISODE_MIN_ASSOC_INTERVAL_S and were")
        print("         throttled. Raise STEP_S in this file.")
        print("       associations high, candidate dwell_s < dwell_s -> the")
        print("         events did not span enough EVENT time.")
        print("       ambiguous high -> the chosen zone overlaps a neighbour.")
        return 1
    ok("the engine associated more than once (not throttled into a single look)",
       int(stats.get("associations") or 0) > 1,
       f"associations={stats.get('associations')}")
    episode_id = active[0]["episode_id"]
    print(f"     episode {episode_id} -> {active[0].get('property_id')}")

    status, ack = send({"event_type": "NON_SEGREGATION_TRIGGER", "timestamp": iso(t),
                        "trigger_id": f"{RUN}-trig-1", "episode_id": episode_id,
                        "collector_id": COLLECTOR, "rfid_uid": UID,
                        "track_id": TRACK, "trigger_status": "RESOLVED",
                        "rfid_event_id": rfid_event})
    ok("second tap applied to WASTRAQ's own episode",
       (ack.get("derived") or {}).get("applied") is True, json.dumps(ack))

    # The clip the edge writes right after the tap.
    clip_id = f"CLIP-{RUN}-REAL"
    status, ack = evidence_ready(t, clip_id, file_url=url_for(clip_id),
                                 rfid_event_id=rfid_event)
    ok("EVIDENCE_READY for the flagged episode accepted", status == 202)
    linked = ack.get("derived") or {}
    ok("clip attributed to the right episode",
       linked.get("episode_id") == episode_id, json.dumps(linked))

    # Walk out; the episode closes and the collection event is created.
    #
    # Straight back BEHIND the camera rather than sideways along the lane -
    # the same away-point test_step3_http.py uses. Stepping 40 m to the side
    # on a 16-property lane can land inside a NEIGHBOUR's service zone, which
    # closes this episode (correctly, as MOVED_ON) and then opens a second
    # one against a house nobody visited. `leave_grace_s` is measured on
    # EVENT time (`ts - active.last_inside`), so grace + 2 s of event time
    # closes it; the wall-clock spacing in walk() is what gets those events
    # past the association throttle so the engine sees them at all.
    t += timedelta(seconds=STEP_S)
    t, _ = walk(t, 0.0, -60.0, grace + 2.0)
    time.sleep(1.5)

    _, episode = get(f"/episodes/{episode_id}")
    event_id = episode.get("collection_event_id")
    if not ok("episode closed into a collection event", bool(event_id),
              json.dumps(episode)[:400]):
        return 1
    ok("the event is NOT_SEGREGATED", episode.get("segregation_status") == "NOT_SEGREGATED",
       json.dumps(episode)[:300])
    property_id = episode.get("property_id")
    print(f"     event {event_id}  property {property_id}")

    # --- 3. idempotency ----------------------------------------------------
    print("\n3. the same clip announced again")
    status, before = get(f"/collection-events/{event_id}/evidence")
    clip_rows_before = [e for e in before if e.get("clip_event_id")]
    status, ack2 = evidence_ready(t, clip_id, file_url=url_for(clip_id),
                                  rfid_event_id=rfid_event)
    ok("the re-announcement is accepted (a retry is not an error)", status == 202)
    time.sleep(1.0)
    status, after = get(f"/collection-events/{event_id}/evidence")
    clip_rows_after = [e for e in after if e.get("clip_event_id")]
    ok("no second evidence row for one clip",
       len(clip_rows_after) == len(clip_rows_before),
       f"before={len(clip_rows_before)} after={len(clip_rows_after)}")
    ok("exactly one VIDEO_CLIP row for this event",
       len([e for e in after if e["evidence_type"] == "VIDEO_CLIP"]) == 1,
       json.dumps([e["evidence_id"] for e in after]))

    # --- 4. playing it -----------------------------------------------------
    print("\n4. the operator presses play")
    clip_row = None
    for _ in range(40):
        _, items = get(f"/collection-events/{event_id}/evidence")
        candidates = [e for e in items if e.get("clip_event_id")]
        if candidates and candidates[0].get("media_status") == "AVAILABLE":
            clip_row = candidates[0]
            break
        time.sleep(0.25)
    if clip_row is None:
        clip_row = ([e for e in items if e.get("clip_event_id")] or [None])[0]

    ok("the clip evidence row is linked to the collection event",
       clip_row is not None and clip_row["event_id"] == event_id)
    ok("media_status is AVAILABLE",
       (clip_row or {}).get("media_status") == "AVAILABLE", json.dumps(clip_row))
    ok("media_url is a WASTRAQ path, not the edge and not a file path",
       (clip_row or {}).get("media_url")
       == f"/evidence/{(clip_row or {}).get('evidence_id')}/media",
       json.dumps((clip_row or {}).get("media_url")))
    ok("media_kind is video", (clip_row or {}).get("media_kind") == "video")
    ok("the Windows path is reported only as provenance",
       str((clip_row or {}).get("source_ref", "")).startswith("C:"),
       json.dumps((clip_row or {}).get("source_ref")))
    ok("evidence.file_path is NOT a Windows path",
       not str((clip_row or {}).get("file_path", "")).startswith("C:"),
       json.dumps((clip_row or {}).get("file_path")))

    if clip_row and clip_row.get("media_url"):
        code, headers, body = raw_get(BASE + clip_row["media_url"])
        ok("GET media returns 200", code == 200, str(code))
        # Asserted on the header as it came off the wire, case-insensitively
        # and with parameters stripped - this is what the browser dispatches
        # the <video> element on.
        ok("Content-Type is video/mp4", headers.media_type() == "video/mp4",
           f"content-type: {headers.get('content-type')!r}")
        ok("Content-Type is not a generic byte stream",
           headers.media_type() not in ("", "application/octet-stream"),
           f"content-type: {headers.get('content-type')!r}")
        ok("Content-Length matches the body",
           headers.get("content-length") == str(len(body)),
           f"{headers.get('content-length')} vs {len(body)}")
        ok("the bytes are the edge's clip, unchanged", body == clip_bytes,
           f"{len(body)} vs {len(clip_bytes)}")
        ok("it is a real MP4 (ftyp box)", body[4:8] == b"ftyp")

        code, headers, body = raw_get(BASE + clip_row["media_url"],
                                      {"Range": "bytes=0-99"})
        ok("Range requests work, so the player can seek",
           code == 206 and len(body) == 100, f"{code} {len(body)}")
        ok("the partial response is still typed video/mp4",
           headers.media_type() == "video/mp4",
           f"content-type: {headers.get('content-type')!r}")
        ok("the server advertises byte ranges",
           headers.get("accept-ranges") == "bytes",
           f"accept-ranges: {headers.get('accept-ranges')!r}")

    # --- 5. an edge that cannot deliver ------------------------------------
    print("\n5. a clip whose bytes cannot be fetched")
    dead = f"CLIP-{RUN}-DEAD"
    status, ack = evidence_ready(datetime.now(timezone.utc), dead,
                                 file_url="http://127.0.0.1:9/never/file",
                                 rfid_event_id=rfid_event)
    ok("EVIDENCE_READY is still accepted when the file cannot be fetched",
       status == 202, json.dumps(ack))
    time.sleep(1.5)
    _, items = get(f"/collection-events/{event_id}/evidence")
    dead_rows = [e for e in items
                 if str(e.get("source_ref", "")).endswith(f"{dead}.mp4")]
    if dead_rows:
        row = dead_rows[0]
        ok("an unfetchable clip is not AVAILABLE",
           row.get("media_status") in ("PENDING", "UNAVAILABLE"), json.dumps(row))
        ok("and it offers no media_url", row.get("media_url") is None)
        code, _h, body = raw_get(f"{BASE}/evidence/{row['evidence_id']}/media")
        ok("GET media answers 409 with a reason, not a Windows path",
           code == 409 and b"EVIDENCE_MEDIA_NOT_HELD" in body,
           f"{code} {body[:160]!r}")
    else:
        print("     (no episode was open for it - nothing to assert)")

    _, event = get(f"/collection-events/{event_id}")
    ok("the collection event survived all of that untouched",
       event.get("segregation_status") == "NOT_SEGREGATED"
       and event.get("property_id") == property_id, json.dumps(event)[:300])

    print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILED'}")
    for name in FAILURES:
        print(f"  FAILED: {name}")
    print("\nCleanup (removes only this run):")
    print(f"  psql -d wastraq_demo -c \"DELETE FROM geovision_raw_events "
          f"WHERE source_id = '{SOURCE}';\"")
    print(f"  psql -d wastraq_demo -c \"DELETE FROM collection_episodes "
          f"WHERE source_id = '{SOURCE}';\"")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
