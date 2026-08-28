#!/usr/bin/env python3
"""
Database-level test for the GeoVision receiver's persistence contract.

Runs the ACTUAL SQL from backend/app/integrations/service.py -- extracted
from the shipped source, not retyped -- against a real PostgreSQL, through
psql. No FastAPI, no running backend, no psycopg needed.

    psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/geovision_integration.sql
    python3 scripts/test_geovision_sql.py

What it proves, which the offline contract test cannot:

  * INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING really is the
    dedup gate: the second delivery of an event_id returns no row, which is
    what makes the receiver stop before writing anything downstream.
  * A 5 Hz stream does not grow the table. Sixty observations of one track
    leave one row with observation_count = 60.
  * Every normalised insert is idempotent, and a clip re-announced under a
    new event_id is still one clip.
  * The CHECK constraints refuse an unknown event_type and an unknown
    binding_status.
  * No geovision table has a property-association column, and none of the
    read queries behind /integrations/geovision/status is broken.

It is NON-DESTRUCTIVE: everything it writes is scoped to its own generated
source_id, every count is filtered by that source_id, and it deletes its own
rows at the end. Safe against the live demo database.

Connection comes from the standard libpq environment (PGHOST, PGPORT,
PGUSER) plus DB_NAME, so it follows backend/.env like everything else.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVICE = os.path.join(ROOT, "backend", "app", "integrations", "service.py")

DB = os.getenv("DB_NAME", "wastraq_demo")
PSQL = ["psql", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-d", DB]
if os.getenv("DB_HOST"):
    PSQL += ["-h", os.environ["DB_HOST"]]
if os.getenv("DB_PORT"):
    PSQL += ["-p", os.environ["DB_PORT"]]
if os.getenv("DB_USER"):
    PSQL += ["-U", os.environ["DB_USER"]]

RUN = uuid.uuid4().hex[:8]
SOURCE = f"GEOVISION-SQLTEST-{RUN}"
SESSION = f"sess-{RUN}"
TS = datetime.now(timezone.utc).replace(microsecond=341000)

FAILURES: list[str] = []


# --- extract the shipped SQL -------------------------------------------------
SRC = open(SERVICE).read()
BLOCKS = re.findall(r'"""(.*?)"""', SRC, re.S)
WRITES: dict[str, str] = {}
READS: list[str] = []
for block in BLOCKS:
    head = block.strip()[:40].upper()
    match = re.search(r"(?:INSERT INTO|UPDATE)\s+(geovision_\w+)", block)
    if match and (head.startswith("INSERT") or head.startswith("UPDATE")):
        WRITES.setdefault(match.group(1), block)
    elif head.startswith("SELECT") and "geovision" in block:
        READS.append(block)


def lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, datetime):
        return "'" + value.isoformat() + "'::timestamptz"
    if isinstance(value, list):
        return "ARRAY[" + ",".join(str(int(v)) for v in value) + "]::int[]"
    if isinstance(value, dict):
        return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


def render(sql: str, params: dict) -> str:
    """psycopg's %(name)s -> a SQL literal, so psql can run the same text.

    Positional %s only appears in the read queries, where it is either a
    LIMIT or an event_type / uid filter; substituted by position so the
    query text that ships is what gets parsed.
    """
    sql = re.sub(r"%\((\w+)\)s", lambda m: lit(params[m.group(1)]), sql)
    sql = re.sub(r"(?i)(LIMIT\s+)%s", lambda m: m.group(1) + "10", sql)
    return re.sub(r"%s", lambda _: lit(params.get("__filter__", "TRACK_UPDATE")), sql)


def run(sql: str, allow_fail: bool = False) -> str:
    proc = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True)
    if proc.returncode and not allow_fail:
        raise SystemExit(f"SQL FAILED:\n{sql}\n{proc.stderr}")
    if proc.returncode:
        return "__ERROR__"
    # psql prints the command tag after any RETURNING rows; drop it so what
    # is left is the data the application itself would have seen.
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if not re.match(r"^(INSERT|UPDATE|DELETE|SELECT|TRUNCATE)\s", ln)]
    return "\n".join(lines).strip()


def ok(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'FAIL'}] {label}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def mine(table: str) -> str:
    return run(f"SELECT count(*) FROM {table} WHERE source_id = {lit(SOURCE)}")


def raw_insert(event_id: str, event_type: str, payload: dict | None = None) -> str:
    return run(render(WRITES["geovision_raw_events"], {
        "event_id": event_id, "event_type": event_type, "source_id": SOURCE,
        "session_id": SESSION, "event_time": TS,
        "payload": payload if payload is not None else {"event_type": event_type},
    }))


def cleanup() -> None:
    # ON DELETE CASCADE from geovision_raw_events clears the normalised rows.
    run(f"DELETE FROM geovision_raw_events WHERE source_id = {lit(SOURCE)}",
        allow_fail=True)
    run(f"DELETE FROM geovision_track_updates WHERE source_id = {lit(SOURCE)}",
        allow_fail=True)
    run(f"DELETE FROM geovision_devices WHERE source_id = {lit(SOURCE)}",
        allow_fail=True)


def main() -> int:  # noqa: C901
    print(f"GeoVision receiver - database contract (db={DB}, source={SOURCE})\n")
    print(f"extracted {len(WRITES)} write statements and {len(READS)} read "
          f"queries from service.py\n")

    if run("SELECT to_regclass('public.geovision_raw_events')") in ("", "__ERROR__"):
        print("!! geovision_* tables are missing. Apply the migration first:")
        print("   psql -v ON_ERROR_STOP=1 -d wastraq_demo "
              "-f database/geovision_integration.sql")
        return 2

    cleanup()
    eid = str(uuid.uuid4())
    payload = {"event_type": "TRACK_UPDATE", "event_id": eid, "track_id": 17,
               "camera_z_m": 3.44, "depth_status": "OK"}

    print("1. the raw insert is the deduplication gate")
    ok("first delivery RETURNs the event_id",
       raw_insert(eid, "TRACK_UPDATE", payload) == eid)
    ok("second delivery of the same event_id RETURNs nothing",
       raw_insert(eid, "TRACK_UPDATE", payload) == "",
       "a redelivery must lose the insert, so the receiver stops before "
       "writing anything downstream")
    ok("exactly one raw row", mine("geovision_raw_events") == "1")
    ok("the payload is stored verbatim",
       json.loads(run("SELECT payload FROM geovision_raw_events "
                      f"WHERE event_id = {lit(eid)}")) == payload)

    print("\n2. ~5 Hz per track does not grow the table")
    track_sql = WRITES["geovision_track_updates"]

    def observe(event_id: str, track_id: int = 17, **over) -> None:
        params = {"source_id": SOURCE, "session_id": SESSION,
                  "track_id": track_id, "event_time": TS, "event_id": event_id,
                  "confidence": 0.94,
                  "bbox": {"x1": 220, "y1": 90, "x2": 390, "y2": 470},
                  "depth_m": 3.54, "camera_x_m": -0.82, "camera_y_m": 0.14,
                  "camera_z_m": 3.44, "relative_x_m": -0.82,
                  "relative_forward_m": 3.44, "depth_valid": True,
                  "depth_status": "OK", "is_authorized_picker": True,
                  "collector_id": "PICKER-01", "identity_confidence": 0.88,
                  "gps": {"latitude": 12.294209, "longitude": 76.641702}}
        params.update(over)
        run(render(track_sql, params))

    observe(eid)
    ok("one row after the first observation", mine("geovision_track_updates") == "1")
    for i in range(59):
        observe(f"{RUN}-obs-{i}")
    ok("60 observations, still ONE row", mine("geovision_track_updates") == "1",
       mine("geovision_track_updates"))
    ok("observation_count reached 60",
       run("SELECT observation_count FROM geovision_track_updates "
           f"WHERE source_id = {lit(SOURCE)} AND track_id = 17") == "60")

    observe(f"{RUN}-no-identity", collector_id=None, identity_confidence=None)
    ok("an identity RFID established is not un-set by a later frame",
       run("SELECT collector_id FROM geovision_track_updates "
           f"WHERE source_id = {lit(SOURCE)} AND track_id = 17") == "PICKER-01")

    observe(f"{RUN}-t22", track_id=22)
    ok("a second track_id makes a second row",
       mine("geovision_track_updates") == "2")

    print("\n3. normalised inserts are idempotent, ambiguity is preserved")
    rfid_id = f"{RUN}-rfid"
    raw_insert(rfid_id, "RFID_TAP")
    rfid_params = {"event_id": rfid_id, "source_id": SOURCE,
                   "session_id": SESSION, "event_time": TS,
                   "rfid_uid": "04A1B2C3", "collector_id": "PICKER-01",
                   "track_id": None, "binding_status": "AMBIGUOUS",
                   "binding_confidence": 0.0,
                   "candidate_track_ids": [17, 22],
                   "reason": "two tracks in the reader zone"}
    run(render(WRITES["geovision_rfid_taps"], rfid_params))
    run(render(WRITES["geovision_rfid_taps"], rfid_params))
    ok("one rfid tap after two identical inserts", mine("geovision_rfid_taps") == "1")
    ok("both candidate tracks kept",
       run("SELECT candidate_track_ids FROM geovision_rfid_taps "
           f"WHERE event_id = {lit(rfid_id)}") == "{17,22}")
    ok("an ambiguous tap names no track",
       run("SELECT coalesce(track_id::text,'NULL') FROM geovision_rfid_taps "
           f"WHERE event_id = {lit(rfid_id)}") == "NULL")

    bind_id = f"{RUN}-bind"
    raw_insert(bind_id, "WORKER_TRACK_BOUND")
    bind_params = {"event_id": bind_id, "source_id": SOURCE,
                   "session_id": SESSION, "event_time": TS,
                   "collector_id": "PICKER-01", "rfid_uid": "04A1B2C3",
                   "track_id": 17, "confidence": 0.91,
                   "rfid_event_id": rfid_id}
    run(render(WRITES["geovision_worker_bindings"], bind_params))
    run(render(WRITES["geovision_worker_bindings"], bind_params))
    ok("one worker binding after two identical inserts",
       mine("geovision_worker_bindings") == "1")

    clip_id = f"{RUN}-clip"
    raw_insert(clip_id, "EVIDENCE_READY")
    clip_params = {"event_id": clip_id, "source_id": SOURCE,
                   "session_id": SESSION, "event_time": TS,
                   "clip_id": f"CLIP-{RUN}",
                   "file_path": rf"C:\GeoVision\evidence_clips\CLIP-{RUN}.mp4",
                   "clip_start": TS - timedelta(seconds=13), "clip_end": TS,
                   "frame_count": 131, "track_id": 17,
                   "rfid_event_id": rfid_id}
    run(render(WRITES["geovision_evidence_clips"], clip_params))
    run(render(WRITES["geovision_evidence_clips"], clip_params))
    ok("one clip after two identical inserts",
       mine("geovision_evidence_clips") == "1")

    clip2 = f"{RUN}-clip2"
    raw_insert(clip2, "EVIDENCE_READY")
    run(render(WRITES["geovision_evidence_clips"], dict(clip_params, event_id=clip2)))
    ok("the same clip re-announced under a new event_id is still one clip",
       mine("geovision_evidence_clips") == "1")
    ok("the Windows path survived the round trip",
       run("SELECT file_path FROM geovision_evidence_clips "
           f"WHERE event_id = {lit(clip_id)}").endswith(f"CLIP-{RUN}.mp4"))

    print("\n4. device liveness")
    for event_type in ("TRACK_UPDATE", "HEARTBEAT"):
        run(render(WRITES["geovision_devices"],
                   {"source_id": SOURCE, "event_type": event_type,
                    "event_time": TS, "session_id": SESSION,
                    "status": {"camera_running": True}}))
    ok("one device row", mine("geovision_devices") == "1")
    ok("events_received counted up",
       int(run("SELECT events_received FROM geovision_devices "
               f"WHERE source_id = {lit(SOURCE)}") or 0) >= 1)

    print("\n5. the schema refuses what it should")
    ok("an unknown event_type is rejected by the CHECK constraint",
       run("INSERT INTO geovision_raw_events "
           "(event_id,event_type,source_id,event_time,payload) VALUES "
           f"({lit(RUN + '-bad')},'NOT_A_TYPE',{lit(SOURCE)},now(),'{{}}'::jsonb)",
           allow_fail=True) == "__ERROR__")
    ok("an unknown binding_status is rejected by the CHECK constraint",
       run("INSERT INTO geovision_rfid_taps "
           "(event_id,source_id,event_time,rfid_uid,binding_status) VALUES "
           f"({lit(rfid_id)},{lit(SOURCE)},now(),'04','MAYBE')",
           allow_fail=True) == "__ERROR__")
    ok("no geovision table has a property-association column",
       run("SELECT count(*) FROM information_schema.columns "
           "WHERE table_name LIKE 'geovision_%' AND column_name IN "
           "('property_id','service_zone_id','collection_event_id',"
           "'segregation_status','authority_property_id','zone_id')") == "0")
    ok("no geovision table references properties or collection_events",
       run("SELECT count(*) FROM information_schema.table_constraints tc "
           "JOIN information_schema.constraint_column_usage ccu "
           "  ON ccu.constraint_name = tc.constraint_name "
           "WHERE tc.table_name LIKE 'geovision_%' "
           "AND tc.constraint_type = 'FOREIGN KEY' "
           "AND ccu.table_name NOT LIKE 'geovision_%'") == "0")

    print("\n6. every read query behind /integrations/geovision/status parses")
    for query in READS:
        rendered = render(query, {"stale": 15.0, "limit": 10})
        label = " ".join(rendered.split())[:58]
        ok(f"runs: {label}...", run(rendered, allow_fail=True) != "__ERROR__")
    ok("the ingest summary view answers",
       run("SELECT count(*) FROM geovision_ingest_summary") != "__ERROR__")

    print("\n7. cleanup")
    cleanup()
    ok("this test left nothing behind",
       mine("geovision_raw_events") == "0"
       and mine("geovision_track_updates") == "0"
       and mine("geovision_devices") == "0")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        pass
