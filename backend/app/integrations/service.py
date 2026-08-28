"""Persistence for GeoVision edge events.

One transaction per event, three statements at most, no GIS.

Ordering matters and is the whole design:

1. ``INSERT INTO geovision_raw_events ... ON CONFLICT (event_id) DO NOTHING
   RETURNING event_id``. The raw envelope is on disk before anything is
   derived from it, and the primary key does the deduplication -- a
   redelivered event loses the insert and returns no row.
2. If no row came back, the event is a duplicate. We stop. No normalised
   row is written, no counter is double-incremented, nothing downstream
   happens twice. The retry queue on the edge keeps the original
   ``event_id`` across attempts precisely so this works.
3. Otherwise, the normalised row for that event type, then
   ``processed = TRUE``.

All three run inside one connection/transaction, so a crash between them
cannot leave a raw event marked processed with nothing to show for it.

What this module deliberately does NOT do
-----------------------------------------
No property lookup. No ``ST_DWithin``, no service-zone test, no nearest-
anything. TRACK_UPDATE arrives at ~5 Hz per track; running the association
ladder on each one would put a PostGIS query on the critical path of a
sensor stream, and -- far worse -- would let a camera observation create a
property association that WASTRAQ's own geometry never sanctioned.

No writes to ``collection_events`` or ``evidence`` either. Those rows mean
"a property was served"; nothing here knows that.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ..database import fetch_all, fetch_one, get_conn
from .schemas import (EvidenceReadyEvent, GeoVisionEvent, HeartbeatEvent,
                      RfidTapEvent, TrackUpdateEvent, WorkerTrackBoundEvent)

# In-process counters. Cheap, and they answer the question the database
# cannot: how many events were REFUSED, which by definition were never
# stored. Reset on restart, and said so in the status payload.
_counter_lock = threading.Lock()
_counters: dict[str, int] = {
    "accepted": 0,
    "duplicates": 0,
    "rejected": 0,
}


def _bump(name: str, by: int = 1) -> None:
    with _counter_lock:
        _counters[name] = _counters.get(name, 0) + by


def record_rejected() -> None:
    """A payload that failed validation and was never stored."""
    _bump("rejected")


def counters() -> dict[str, int]:
    with _counter_lock:
        return dict(_counters)


def reset_counters() -> None:
    with _counter_lock:
        for key in _counters:
            _counters[key] = 0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------

_RAW_INSERT = """
    INSERT INTO geovision_raw_events
        (event_id, event_type, source_id, session_id, event_time, payload)
    VALUES (%(event_id)s, %(event_type)s, %(source_id)s, %(session_id)s,
            %(event_time)s, %(payload)s)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
"""

_DEVICE_UPSERT = """
    INSERT INTO geovision_devices
        (source_id, last_seen_at, last_event_type, last_event_at,
         last_session_id, events_received)
    VALUES (%(source_id)s, now(), %(event_type)s, %(event_time)s,
            %(session_id)s, 1)
    ON CONFLICT (source_id) DO UPDATE SET
        last_seen_at    = now(),
        last_event_type = EXCLUDED.last_event_type,
        last_event_at   = GREATEST(geovision_devices.last_event_at,
                                   EXCLUDED.last_event_at),
        last_session_id = COALESCE(EXCLUDED.last_session_id,
                                   geovision_devices.last_session_id),
        events_received = geovision_devices.events_received + 1
"""


def ingest(event: GeoVisionEvent, raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Store one validated event. Idempotent on ``event_id``.

    Returns the ack body. ``duplicate`` is True when this ``event_id`` had
    already been accepted, in which case nothing was written this time.
    """
    session_id = getattr(event, "session_id", None)
    envelope = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "source_id": event.source_id,
        "session_id": session_id,
        "event_time": event.timestamp,
        "payload": Jsonb(raw_payload),
    }

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_RAW_INSERT, envelope)
        inserted = cur.fetchone()

        if inserted is None:
            # Already seen. The edge's retry queue reuses the event_id on
            # purpose, so this is the normal, expected outcome of a retry -
            # not an error, and not something to log at every occurrence.
            conn.commit()
            _bump("duplicates")
            _touch_duplicate(event.source_id)
            return {
                "status": "DUPLICATE",
                "event_id": event.event_id,
                "event_type": event.event_type,
                "duplicate": True,
                "received_at": now_utc(),
            }

        _write_normalised(cur, event)

        cur.execute(
            "UPDATE geovision_raw_events SET processed = TRUE WHERE event_id = %s",
            (event.event_id,),
        )
        cur.execute(_DEVICE_UPSERT, {
            "source_id": event.source_id,
            "event_type": event.event_type,
            "event_time": event.timestamp,
            "session_id": session_id,
        })
        conn.commit()

    _bump("accepted")
    return {
        "status": "ACCEPTED",
        "event_id": event.event_id,
        "event_type": event.event_type,
        "duplicate": False,
        "received_at": now_utc(),
    }


def _touch_duplicate(source_id: str) -> None:
    """Count the redelivery against the device, without a second transaction
    failing the ack if the device row does not exist yet."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE geovision_devices
                   SET duplicates_ignored = duplicates_ignored + 1,
                       last_seen_at = now()
                 WHERE source_id = %s
                """,
                (source_id,),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        # Bookkeeping only. The ack has already been decided.
        pass


def _write_normalised(cur, event: GeoVisionEvent) -> None:
    if isinstance(event, TrackUpdateEvent):
        _upsert_track(cur, event)
    elif isinstance(event, RfidTapEvent):
        _insert_rfid(cur, event)
    elif isinstance(event, WorkerTrackBoundEvent):
        _insert_binding(cur, event)
    elif isinstance(event, EvidenceReadyEvent):
        _insert_clip(cur, event)
    elif isinstance(event, HeartbeatEvent):
        _upsert_heartbeat(cur, event)


# --- TRACK_UPDATE ------------------------------------------------------------
_TRACK_UPSERT = """
    INSERT INTO geovision_track_updates (
        source_id, session_id, track_id,
        first_seen_at, last_seen_at, last_event_id, observation_count,
        confidence, bbox,
        depth_m, camera_x_m, camera_y_m, camera_z_m,
        relative_x_m, relative_forward_m, depth_valid, depth_status,
        is_authorized_picker, collector_id, identity_confidence, gps,
        updated_at
    ) VALUES (
        %(source_id)s, %(session_id)s, %(track_id)s,
        %(event_time)s, %(event_time)s, %(event_id)s, 1,
        %(confidence)s, %(bbox)s,
        %(depth_m)s, %(camera_x_m)s, %(camera_y_m)s, %(camera_z_m)s,
        %(relative_x_m)s, %(relative_forward_m)s, %(depth_valid)s, %(depth_status)s,
        %(is_authorized_picker)s, %(collector_id)s, %(identity_confidence)s, %(gps)s,
        now()
    )
    ON CONFLICT (source_id, session_id, track_id) DO UPDATE SET
        last_seen_at       = GREATEST(geovision_track_updates.last_seen_at,
                                      EXCLUDED.last_seen_at),
        last_event_id      = EXCLUDED.last_event_id,
        observation_count  = geovision_track_updates.observation_count + 1,
        confidence         = EXCLUDED.confidence,
        bbox               = EXCLUDED.bbox,
        depth_m            = EXCLUDED.depth_m,
        camera_x_m         = EXCLUDED.camera_x_m,
        camera_y_m         = EXCLUDED.camera_y_m,
        camera_z_m         = EXCLUDED.camera_z_m,
        relative_x_m       = EXCLUDED.relative_x_m,
        relative_forward_m = EXCLUDED.relative_forward_m,
        depth_valid        = EXCLUDED.depth_valid,
        depth_status       = EXCLUDED.depth_status,
        is_authorized_picker = EXCLUDED.is_authorized_picker,
        -- An identity, once RFID established it, is not un-set by a later
        -- frame that happens not to carry it.
        collector_id       = COALESCE(EXCLUDED.collector_id,
                                      geovision_track_updates.collector_id),
        identity_confidence = COALESCE(EXCLUDED.identity_confidence,
                                       geovision_track_updates.identity_confidence),
        gps                = COALESCE(EXCLUDED.gps, geovision_track_updates.gps),
        updated_at         = now()
"""


def _upsert_track(cur, event: TrackUpdateEvent) -> None:
    cur.execute(_TRACK_UPSERT, {
        "source_id": event.source_id,
        # '' not NULL: session_id is part of the primary key, and a NULL in a
        # key means ON CONFLICT never matches and the table grows forever.
        "session_id": event.session_id or "",
        "track_id": event.track_id,
        "event_time": event.timestamp,
        "event_id": event.event_id,
        "confidence": event.confidence,
        "bbox": Jsonb(event.bbox.model_dump()) if event.bbox else None,
        "depth_m": event.depth_m,
        "camera_x_m": event.camera_x_m,
        "camera_y_m": event.camera_y_m,
        "camera_z_m": event.camera_z_m,
        "relative_x_m": event.relative_x_m,
        "relative_forward_m": event.relative_forward_m,
        "depth_valid": event.depth_valid,
        "depth_status": event.depth_status,
        "is_authorized_picker": event.is_authorized_picker,
        "collector_id": event.collector_id,
        "identity_confidence": event.identity_confidence,
        "gps": Jsonb(json.loads(event.gps.model_dump_json())) if event.gps else None,
    })


# --- RFID_TAP ----------------------------------------------------------------
def _insert_rfid(cur, event: RfidTapEvent) -> None:
    cur.execute(
        """
        INSERT INTO geovision_rfid_taps
            (event_id, source_id, session_id, event_time, rfid_uid,
             collector_id, track_id, binding_status, binding_confidence,
             candidate_track_ids, reason)
        VALUES (%(event_id)s, %(source_id)s, %(session_id)s, %(event_time)s,
                %(rfid_uid)s, %(collector_id)s, %(track_id)s,
                %(binding_status)s, %(binding_confidence)s,
                %(candidate_track_ids)s, %(reason)s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        {
            "event_id": event.event_id,
            "source_id": event.source_id,
            "session_id": event.session_id,
            "event_time": event.timestamp,
            "rfid_uid": event.rfid_uid,
            "collector_id": event.collector_id,
            "track_id": event.track_id,
            "binding_status": event.binding_status,
            "binding_confidence": event.binding_confidence,
            # Kept whole. Two people at the reader is a fact about the world,
            # not a problem to be tidied away by picking one.
            "candidate_track_ids": list(event.candidate_track_ids),
            "reason": event.reason,
        },
    )


# --- WORKER_TRACK_BOUND ------------------------------------------------------
def _insert_binding(cur, event: WorkerTrackBoundEvent) -> None:
    cur.execute(
        """
        INSERT INTO geovision_worker_bindings
            (event_id, source_id, session_id, event_time, collector_id,
             rfid_uid, track_id, confidence, rfid_event_id)
        VALUES (%(event_id)s, %(source_id)s, %(session_id)s, %(event_time)s,
                %(collector_id)s, %(rfid_uid)s, %(track_id)s, %(confidence)s,
                %(rfid_event_id)s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        {
            "event_id": event.event_id,
            "source_id": event.source_id,
            "session_id": event.session_id,
            "event_time": event.timestamp,
            "collector_id": event.collector_id,
            "rfid_uid": event.rfid_uid,
            "track_id": event.track_id,
            "confidence": event.confidence,
            "rfid_event_id": event.rfid_event_id,
        },
    )


# --- EVIDENCE_READY ----------------------------------------------------------
def _insert_clip(cur, event: EvidenceReadyEvent) -> None:
    cur.execute(
        """
        INSERT INTO geovision_evidence_clips
            (event_id, source_id, session_id, event_time, clip_id, file_path,
             clip_start, clip_end, frame_count, track_id, rfid_event_id)
        VALUES (%(event_id)s, %(source_id)s, %(session_id)s, %(event_time)s,
                %(clip_id)s, %(file_path)s, %(clip_start)s, %(clip_end)s,
                %(frame_count)s, %(track_id)s, %(rfid_event_id)s)
        -- (source_id, clip_id) is unique: the same clip re-announced under a
        -- new event_id is still one clip.
        ON CONFLICT DO NOTHING
        """,
        {
            "event_id": event.event_id,
            "source_id": event.source_id,
            "session_id": event.session_id,
            "event_time": event.timestamp,
            "clip_id": event.clip_id,
            "file_path": event.file_path,
            "clip_start": event.start_time,
            "clip_end": event.end_time,
            "frame_count": event.frame_count,
            "track_id": event.track_id,
            "rfid_event_id": event.rfid_event_id,
        },
    )


# --- HEARTBEAT ---------------------------------------------------------------
def _upsert_heartbeat(cur, event: HeartbeatEvent) -> None:
    cur.execute(
        """
        INSERT INTO geovision_devices
            (source_id, last_seen_at, last_heartbeat_at, last_status)
        VALUES (%(source_id)s, now(), %(event_time)s, %(status)s)
        ON CONFLICT (source_id) DO UPDATE SET
            last_seen_at      = now(),
            last_heartbeat_at = EXCLUDED.last_heartbeat_at,
            last_status       = EXCLUDED.last_status
        """,
        {
            "source_id": event.source_id,
            "event_time": event.timestamp,
            "status": Jsonb(event.status),
        },
    )


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

def ingest_summary() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT event_type, events, last_event_time, last_received_at, unprocessed
        FROM geovision_ingest_summary
        ORDER BY event_type
        """
    )


def devices() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT source_id, first_seen_at, last_seen_at, last_event_type,
               last_event_at, last_heartbeat_at, last_session_id,
               events_received, duplicates_ignored,
               EXTRACT(EPOCH FROM (now() - last_seen_at))::float AS seconds_since_seen,
               last_status
        FROM geovision_devices
        ORDER BY last_seen_at DESC
        """
    )


def active_tracks(stale_after_s: float, limit: int = 50) -> list[dict[str, Any]]:
    """Tracks seen within ``stale_after_s``. Camera-frame numbers only."""
    return fetch_all(
        """
        SELECT source_id, session_id, track_id, last_seen_at, observation_count,
               collector_id, is_authorized_picker, depth_m, depth_valid,
               depth_status, relative_x_m, relative_forward_m,
               EXTRACT(EPOCH FROM (now() - last_seen_at))::float AS age_s
        FROM geovision_track_updates
        WHERE last_seen_at > now() - make_interval(secs => %(stale)s)
        ORDER BY last_seen_at DESC
        LIMIT %(limit)s
        """,
        {"stale": float(stale_after_s), "limit": limit},
    )


def recent_rfid_taps(limit: int = 10) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT event_id, source_id, session_id, event_time, rfid_uid,
               collector_id, track_id, binding_status, binding_confidence,
               candidate_track_ids, reason
        FROM geovision_rfid_taps
        ORDER BY event_time DESC
        LIMIT %s
        """,
        (limit,),
    )


def recent_bindings(limit: int = 10) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT event_id, source_id, session_id, event_time, collector_id,
               rfid_uid, track_id, confidence, rfid_event_id
        FROM geovision_worker_bindings
        ORDER BY event_time DESC
        LIMIT %s
        """,
        (limit,),
    )


def recent_clips(limit: int = 10) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT event_id, source_id, session_id, event_time, clip_id, file_path,
               clip_start, clip_end, frame_count, track_id, rfid_event_id, fetched
        FROM geovision_evidence_clips
        ORDER BY event_time DESC
        LIMIT %s
        """,
        (limit,),
    )


def totals() -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT
          (SELECT count(*) FROM geovision_raw_events)        AS raw_events,
          (SELECT count(*) FROM geovision_track_updates)     AS tracks,
          (SELECT count(*) FROM geovision_rfid_taps)         AS rfid_taps,
          (SELECT count(*) FROM geovision_worker_bindings)   AS worker_bindings,
          (SELECT count(*) FROM geovision_evidence_clips)    AS evidence_clips,
          (SELECT count(*) FROM geovision_devices)           AS devices,
          (SELECT max(received_at) FROM geovision_raw_events) AS last_received_at
        """
    )
    return row or {}


def raw_events(limit: int = 20, event_type: str | None = None) -> list[dict[str, Any]]:
    if event_type:
        return fetch_all(
            """
            SELECT event_id, event_type, source_id, session_id, event_time,
                   received_at, processed
            FROM geovision_raw_events
            WHERE event_type = %s
            ORDER BY received_at DESC
            LIMIT %s
            """,
            (event_type, limit),
        )
    return fetch_all(
        """
        SELECT event_id, event_type, source_id, session_id, event_time,
               received_at, processed
        FROM geovision_raw_events
        ORDER BY received_at DESC
        LIMIT %s
        """,
        (limit,),
    )
