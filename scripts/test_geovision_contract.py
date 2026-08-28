#!/usr/bin/env python3
"""
Offline contract test for the GeoVision receiver's validation layer.

No database, no FastAPI, no running backend - it loads
backend/app/integrations/schemas.py directly by path and exercises the
pydantic models against the payloads in
geovision-darshan/docs/WASTRAQ_INTEGRATION.md.

What it is actually protecting:

  * All five event types validate from the documented payloads verbatim.
  * A payload that would assert a property association is REFUSED. This is
    the architectural rule of the whole integration - GeoVision observes,
    WASTRAQ decides which house was served - and a test is the only thing
    that keeps it true after the next refactor.
  * RFID ambiguity survives. AMBIGUOUS with a track_id is refused rather
    than quietly stored as a binding.
  * Timestamps are aware and normalised to UTC.
  * Unknown fields are tolerated, so a GeoVision release that adds one does
    not become a WASTRAQ outage.

    python3 scripts/test_geovision_contract.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMAS = os.path.join(ROOT, "backend", "app", "integrations", "schemas.py")

# Loaded by path so this file needs pydantic and nothing else - no fastapi,
# no psycopg, no .env.
_spec = importlib.util.spec_from_file_location("gv_schemas", SCHEMAS)
assert _spec and _spec.loader
gv = importlib.util.module_from_spec(_spec)
# Registered BEFORE exec: schemas.py uses `from __future__ import annotations`,
# so every annotation is a string and pydantic resolves the model's own name
# through sys.modules. Without this the models never finish building and every
# check below fails with "is not fully defined" - an artefact of loading by
# path, not a fault in the schema.
sys.modules["gv_schemas"] = gv
_spec.loader.exec_module(gv)

ADAPTER = gv.EVENT_ADAPTER

SOURCE = "GEOVISION-D455-01"
SESSION = "9f2c1ab0c3d4"
TS = "2026-08-28T07:10:12.341Z"


# --- the five documented payloads -------------------------------------------
def track_update(**over) -> dict:
    event = {
        "event_type": "TRACK_UPDATE",
        "event_id": "0f0b0a4e-1111-4222-8333-444444444444",
        "timestamp": TS,
        "source_id": SOURCE,
        "session_id": SESSION,
        "track_id": 17,
        "confidence": 0.94,
        "bbox": {"x1": 220, "y1": 90, "x2": 390, "y2": 470},
        "depth_m": 3.54,
        "camera_x_m": -0.82,
        "camera_y_m": 0.14,
        "camera_z_m": 3.44,
        "relative_x_m": -0.82,
        "relative_forward_m": 3.44,
        "depth_valid": True,
        "depth_status": "OK",
        "is_authorized_picker": True,
        "collector_id": "GC-001",
        "identity_confidence": 0.88,
        "gps": {
            "timestamp": "2026-08-28T07:10:11.900Z",
            "latitude": 12.294209,
            "longitude": 76.641702,
            "accuracy_m": 8.0,
            "source": "PHONE",
            "age_s": 0.44,
            "stale": False,
            "altitude_m": None,
            "speed_mps": None,
            "hdop": None,
            "satellites": None,
            "heading_deg": None,
        },
    }
    event.update(over)
    return event


def rfid_tap(**over) -> dict:
    event = {
        "event_type": "RFID_TAP",
        "event_id": "0f0b0a4e-2222-4222-8333-444444444444",
        "timestamp": TS,
        "source_id": "GEOVISION-RFID-01",
        "rfid_uid": "04A1B2C3",
        "collector_id": "PICKER-01",
        "track_id": 17,
        "binding_status": "BOUND",
        "binding_confidence": 0.91,
        "candidate_track_ids": [17],
        "session_id": SESSION,
    }
    event.update(over)
    return event


def worker_bound(**over) -> dict:
    event = {
        "event_type": "WORKER_TRACK_BOUND",
        "event_id": "0f0b0a4e-3333-4222-8333-444444444444",
        "timestamp": TS,
        "source_id": SOURCE,
        "collector_id": "PICKER-01",
        "rfid_uid": "04A1B2C3",
        "track_id": 17,
        "confidence": 0.91,
        "session_id": SESSION,
        "rfid_event_id": "0f0b0a4e-2222-4222-8333-444444444444",
    }
    event.update(over)
    return event


def evidence_ready(**over) -> dict:
    event = {
        "event_type": "EVIDENCE_READY",
        "event_id": "0f0b0a4e-4444-4222-8333-444444444444",
        "timestamp": TS,
        "source_id": SOURCE,
        "track_id": 17,
        "rfid_event_id": "0f0b0a4e-2222-4222-8333-444444444444",
        "clip_id": "CLIP-3f2a1b0c9d8e",
        "file_path": "C:\\GeoVision\\backend\\evidence_clips\\CLIP-3f2a1b0c9d8e.mp4",
        "start_time": "2026-08-28T07:10:02.341Z",
        "end_time": "2026-08-28T07:10:15.341Z",
        "frame_count": 131,
        "session_id": SESSION,
    }
    event.update(over)
    return event


def heartbeat(**over) -> dict:
    event = {
        "event_type": "HEARTBEAT",
        "event_id": "0f0b0a4e-5555-4222-8333-444444444444",
        "timestamp": TS,
        "source_id": SOURCE,
        "status": {
            "source_id": SOURCE,
            "realsense_connected": True,
            "camera_running": True,
            "tracking_active": True,
            "depth_available": True,
            "gps_valid": True,
            "rfid_available": False,
            "rfid_mode": "API_INGEST_ONLY",
            "wastraq_enabled": True,
            "wastraq_reachable": True,
            "pending_events": 0,
            "last_track_sent_at": TS,
            "last_rfid_event_at": None,
        },
    }
    event.update(over)
    return event


# --- harness -----------------------------------------------------------------
FAILURES: list[str] = []


def ok(label: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def accepts(label: str, payload: dict):
    try:
        event = ADAPTER.validate_python(payload)
        ok(label, True)
        return event
    except Exception as exc:  # noqa: BLE001
        ok(label, False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return None


def rejects(label: str, payload: dict, expect_in: str = "") -> None:
    try:
        ADAPTER.validate_python(payload)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        ok(label, (expect_in in text) if expect_in else True,
           f"rejected, but not for {expect_in!r}: {text[:200]}")
        return
    ok(label, False, "payload was ACCEPTED and should not have been")


def main() -> int:
    print("GeoVision receiver - contract validation (offline)\n")

    print("1. the five documented event types")
    t = accepts("TRACK_UPDATE validates", track_update())
    r = accepts("RFID_TAP validates", rfid_tap())
    b = accepts("WORKER_TRACK_BOUND validates", worker_bound())
    e = accepts("EVIDENCE_READY validates", evidence_ready())
    h = accepts("HEARTBEAT validates", heartbeat())

    print("\n2. the envelope survives intact")
    if t:
        ok("event_type discriminated", type(t).__name__ == "TrackUpdateEvent")
        ok("timestamp normalised to UTC", t.timestamp.tzinfo == timezone.utc)
        ok("camera metres preserved", t.camera_z_m == 3.44)
        ok("bbox parsed", t.bbox is not None and t.bbox.x2 == 390)
        ok("gps kept as its own object", t.gps is not None and t.gps.source == "PHONE")
    if e:
        ok("clip times aware", e.start_time is not None
           and e.start_time.tzinfo == timezone.utc)
        ok("windows file path untouched", e.file_path.endswith(".mp4"))
    if h:
        ok("heartbeat status kept whole",
           h.status.get("rfid_mode") == "API_INGEST_ONLY")
    if b:
        ok("binding names its rfid event", b.rfid_event_id is not None)

    print("\n3. GeoVision may not assert a property association")
    for field in ("property_id", "authority_property_id", "house_number",
                  "service_zone_id", "zone_id", "segregation_status",
                  "collection_event_id"):
        rejects(f"TRACK_UPDATE carrying {field} is refused",
                track_update(**{field: "PROP-003"}),
                "does not determine the serviced property")
    rejects("a property_id nested inside gps is refused",
            track_update(gps={"latitude": 12.29, "longitude": 76.64,
                              "property_id": "PROP-003"}),
            "does not determine the serviced property")
    rejects("a property_id nested inside heartbeat status is refused",
            heartbeat(status={"camera_running": True, "property_id": "PROP-003"}),
            "does not determine the serviced property")

    print("\n4. RFID ambiguity is preserved, not resolved")
    amb = accepts("AMBIGUOUS tap with no track_id validates",
                  rfid_tap(binding_status="AMBIGUOUS", track_id=None,
                           candidate_track_ids=[17, 22]))
    if amb:
        ok("both candidates kept", amb.candidate_track_ids == [17, 22])
        ok("no track claimed", amb.track_id is None)
    rejects("AMBIGUOUS tap that names a track is refused",
            rfid_tap(binding_status="AMBIGUOUS", track_id=17),
            "only BOUND may name a track")
    rejects("BOUND tap with no track is refused",
            rfid_tap(binding_status="BOUND", track_id=None),
            "requires a track_id")
    for status in ("NO_TRACK_IN_READER_ZONE", "NO_TRACK_DATA", "UNKNOWN_RFID"):
        accepts(f"{status} validates",
                rfid_tap(binding_status=status, track_id=None,
                         candidate_track_ids=[]))

    print("\n5. malformed payloads are refused")
    rejects("unknown event_type", track_update(event_type="SOMETHING_ELSE"))
    rejects("missing event_type", {k: v for k, v in track_update().items()
                                   if k != "event_type"})
    rejects("missing event_id", {k: v for k, v in track_update().items()
                                 if k != "event_id"})
    rejects("empty event_id", track_update(event_id=""))
    rejects("missing source_id", {k: v for k, v in track_update().items()
                                 if k != "source_id"})
    rejects("naive timestamp", track_update(timestamp="2026-08-28T07:10:12.341"),
            "timezone-aware")
    rejects("nonsense timestamp", track_update(timestamp="yesterday afternoon"))
    rejects("missing track_id on TRACK_UPDATE",
            {k: v for k, v in track_update().items() if k != "track_id"})
    rejects("non-numeric track_id", track_update(track_id="seventeen"))
    rejects("latitude out of range",
            track_update(gps={"latitude": 991.0, "longitude": 76.6}))
    rejects("bad binding_status", rfid_tap(binding_status="MAYBE"))
    rejects("missing rfid_uid on RFID_TAP",
            {k: v for k, v in rfid_tap().items() if k != "rfid_uid"})
    rejects("missing clip_id on EVIDENCE_READY",
            {k: v for k, v in evidence_ready().items() if k != "clip_id"})
    rejects("clip that ends before it starts",
            evidence_ready(start_time="2026-08-28T07:10:15.341Z",
                           end_time="2026-08-28T07:10:02.341Z"),
            "before start_time")
    rejects("negative frame_count", evidence_ready(frame_count=-3))
    rejects("empty body", {})
    rejects("a list instead of an object", {"event_type": "TRACK_UPDATE",
                                            "event_id": ["nope"],
                                            "timestamp": TS,
                                            "source_id": SOURCE,
                                            "track_id": 1})

    print("\n6. forward compatibility - unknown fields are tolerated")
    fut = accepts("TRACK_UPDATE with an unheard-of field validates",
                  track_update(imu_yaw_deg=12.5, lidar_hits=None))
    if fut:
        ok("known fields still parsed", fut.track_id == 17)
    accepts("HEARTBEAT with a new status key validates",
            heartbeat(status={"camera_running": True, "new_subsystem": "OK"}))

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
