"""The GeoVision -> WASTRAQ event vocabulary.

Five event types, one envelope. Every event carries:

``event_type``
    Which of the five it is.
``event_id``
    A UUID. WASTRAQ deduplicates on it, which is what makes the retry queue
    safe: the same event may legitimately arrive twice.
``timestamp``
    ISO-8601 UTC with a ``Z`` suffix and millisecond precision, e.g.
    ``2026-08-28T07:10:12.341Z``. Never a frame number, never a naive local
    time -- two machines are involved and their clocks must be comparable.
``source_id``
    Which sensor produced it (``GEOVISION-D455-01`` and friends).

Stdlib only. No numpy, no FastAPI, no pyrealsense2 -- these builders are
pure functions over dicts so the whole vocabulary is testable with nothing
installed.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

EVENT_TRACK_UPDATE = "TRACK_UPDATE"
EVENT_RFID_TAP = "RFID_TAP"
EVENT_WORKER_TRACK_BOUND = "WORKER_TRACK_BOUND"
EVENT_NON_SEGREGATION_TRIGGER = "NON_SEGREGATION_TRIGGER"
EVENT_EVIDENCE_READY = "EVIDENCE_READY"
EVENT_HEARTBEAT = "HEARTBEAT"

EVENT_TYPES = (
    EVENT_TRACK_UPDATE,
    EVENT_RFID_TAP,
    EVENT_WORKER_TRACK_BOUND,
    EVENT_NON_SEGREGATION_TRIGGER,
    EVENT_EVIDENCE_READY,
    EVENT_HEARTBEAT,
)

#: Keys that would assert a property association. GeoVision has no basis for
#: any of them -- it cannot see a service zone -- so they are forbidden in
#: outbound events rather than merely discouraged.
PROPERTY_FIELDS = frozenset(
    {
        "property_id",
        "property_name",
        "properties",
        "property_candidates",
        "authority_property_id",
        "house_number",
        "service_zone_id",
        "zone_id",
        "entrance_id",
        "frontage_id",
        "segregation_status",
        "collection_event_id",
    }
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def new_event_id() -> str:
    """A fresh event id. Used by WASTRAQ for idempotent ingestion."""

    return str(uuid.uuid4())


def iso_utc(timestamp: Optional[float] = None) -> str:
    """Epoch seconds -> ``2026-08-28T07:10:12.341Z``.

    Millisecond precision, explicit ``Z``. ``datetime.isoformat()`` would
    give ``+00:00`` and microseconds; both are valid ISO-8601, but a single
    fixed shape is easier to eyeball in two logs side by side.
    """

    moment = datetime.fromtimestamp(
        time.time() if timestamp is None else float(timestamp),
        tz=timezone.utc,
    )
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def bbox_dict(bbox: Sequence[int]) -> Dict[str, int]:
    """``[x1, y1, x2, y2]`` -> ``{"x1": .., "y1": .., "x2": .., "y2": ..}``."""

    x1, y1, x2, y2 = bbox
    return {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2)}


def _envelope(event_type: str, source_id: str, timestamp: Optional[float]) -> dict:
    return {
        "event_type": event_type,
        "event_id": new_event_id(),
        "timestamp": iso_utc(timestamp),
        "source_id": source_id,
    }


# ---------------------------------------------------------------------------
# The no-property-association guard
# ---------------------------------------------------------------------------


def find_property_fields(payload: Any, _path: str = "") -> list:
    """Every path in ``payload`` whose key asserts a property association."""

    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{_path}.{key}" if _path else str(key)
            if key in PROPERTY_FIELDS:
                found.append(path)
            found.extend(find_property_fields(value, path))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(find_property_fields(item, f"{_path}[{index}]"))
    return found


def reject_property_fields(payload: dict) -> dict:
    """Strip any property-association field, loudly.

    Belt and braces. The builders below never add one, but GeoVision still
    contains legacy proximity-matching code and a database full of
    ``properties`` rows; a future edit that accidentally forwards one must
    fail visibly here rather than teach WASTRAQ to trust a GPS guess.
    """

    offending = find_property_fields(payload)
    if not offending:
        return payload

    logger.error(
        "Refusing to publish property association field(s) %s -- GeoVision "
        "does not determine the serviced property.",
        ", ".join(offending),
    )
    return _strip(payload)


def _strip(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _strip(value)
            for key, value in payload.items()
            if key not in PROPERTY_FIELDS
        }
    if isinstance(payload, list):
        return [_strip(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(_strip(item) for item in payload)
    return payload


# ---------------------------------------------------------------------------
# GPS side-car
# ---------------------------------------------------------------------------


def gps_block(fix: Optional[dict]) -> Optional[dict]:
    """The optional ``gps`` object attached to a TRACK_UPDATE.

    Kept as a nested object on purpose. A camera observation and a phone fix
    are measurements of different things, taken by different devices, with
    different error models; flattening them together would invite WASTRAQ to
    treat the pair as one fused position, which it is not.
    """

    if not fix:
        return None
    return {
        "timestamp": fix.get("timestamp"),
        "latitude": fix.get("latitude"),
        "longitude": fix.get("longitude"),
        "accuracy_m": fix.get("accuracy_m"),
        "source": fix.get("source"),
        "age_s": fix.get("age_s"),
        "stale": fix.get("stale"),
        # No GNSS receiver and no IMU exist in this system. Reported as null
        # rather than omitted so the absence is explicit, not accidental.
        "altitude_m": None,
        "speed_mps": None,
        "hdop": None,
        "satellites": None,
        "heading_deg": None,
    }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def track_update_event(
    source_id: str,
    observation: dict,
    timestamp: Optional[float] = None,
    gps: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """One tracked person, at one instant, in the camera's own frame.

    Deliberately *not* included: any world position, any property, any
    judgement about whether a collection happened. Those are WASTRAQ's.
    """

    event = _envelope(EVENT_TRACK_UPDATE, source_id, timestamp)
    event.update(
        {
            "session_id": session_id or observation.get("session_id"),
            "track_id": observation.get("track_id"),
            "confidence": observation.get("detection_confidence"),
            "bbox": bbox_dict(observation["bbox"]),
            "depth_m": observation.get("depth_m"),
            "camera_x_m": observation.get("camera_x_m"),
            "camera_y_m": observation.get("camera_y_m"),
            "camera_z_m": observation.get("camera_z_m"),
            "relative_x_m": observation.get("relative_x_m"),
            "relative_forward_m": observation.get("relative_forward_m"),
            "depth_valid": bool(observation.get("depth_valid")),
            "depth_status": observation.get("depth_status"),
            # Worker identity, when RFID established it. Never inferred.
            "is_authorized_picker": bool(observation.get("is_authorized_picker")),
            "collector_id": observation.get("collector_id"),
            "identity_confidence": observation.get("identity_confidence"),
        }
    )
    block = gps_block(gps)
    if block is not None:
        event["gps"] = block
    return reject_property_fields(event)


def rfid_tap_event(
    source_id: str,
    rfid_uid: str,
    collector_id: Optional[str],
    track_id: Optional[int],
    status: str,
    timestamp: Optional[float] = None,
    candidate_track_ids: Optional[Iterable[int]] = None,
    confidence: float = 0.0,
    session_id: Optional[str] = None,
    reason: str = "",
) -> dict:
    """One RFID tap, with whatever the camera could honestly attribute to it.

    ``track_id`` is ``None`` unless the evidence zone singled out exactly one
    track. When several people were at the reader the status is ``AMBIGUOUS``
    and every candidate is listed -- ambiguity travels to WASTRAQ instead of
    being resolved by a coin flip here.
    """

    event = _envelope(EVENT_RFID_TAP, source_id, timestamp)
    event.update(
        {
            "rfid_uid": rfid_uid,
            "collector_id": collector_id,
            "track_id": track_id,
            "binding_status": status,
            "binding_confidence": round(float(confidence or 0.0), 3),
            "candidate_track_ids": list(candidate_track_ids or []),
            "session_id": session_id,
        }
    )
    if reason:
        event["reason"] = reason
    return reject_property_fields(event)


def worker_track_bound_event(
    source_id: str,
    collector_id: str,
    rfid_uid: str,
    track_id: int,
    confidence: float,
    session_id: str,
    timestamp: Optional[float] = None,
    rfid_event_id: Optional[str] = None,
) -> dict:
    """A collector is now associated with a camera track for this session."""

    event = _envelope(EVENT_WORKER_TRACK_BOUND, source_id, timestamp)
    event.update(
        {
            "collector_id": collector_id,
            "rfid_uid": rfid_uid,
            "track_id": int(track_id),
            "confidence": round(float(confidence), 3),
            "session_id": session_id,
            "rfid_event_id": rfid_event_id,
        }
    )
    return reject_property_fields(event)


def non_segregation_trigger_event(
    source_id: str,
    trigger_id: str,
    episode_id: Optional[str],
    collector_id: str,
    rfid_uid: str,
    track_id: Optional[int],
    status: str,
    timestamp: Optional[float] = None,
    session_id: Optional[str] = None,
    rfid_event_id: Optional[str] = None,
    duplicate: bool = False,
    reason: str = "",
) -> dict:
    """A bound collector says the waste in front of them is not segregated.

    A *signal*, not a verdict. GeoVision reports that the person locked to
    track N pressed their card against the reader while WASTRAQ's episode
    ``episode_id`` was open; WASTRAQ decides which property that episode
    belongs to and sets its segregation status. ``segregation_status`` is in
    :data:`PROPERTY_FIELDS` for exactly that reason -- this builder must not
    be able to assert one.

    ``trigger_id`` is stable across repeats. A card held a moment too long,
    a bouncing reader or a retried POST produces the same id with
    ``duplicate: true``, so WASTRAQ can ingest idempotently.

    Unresolved outcomes travel too: ``NO_ACTIVE_EPISODE`` with a null
    ``episode_id`` is sent rather than swallowed, because WASTRAQ holds the
    authoritative episode table and may know about a collection this edge
    mirror does not.
    """

    event = _envelope(EVENT_NON_SEGREGATION_TRIGGER, source_id, timestamp)
    event.update(
        {
            "trigger_id": trigger_id,
            "episode_id": episode_id,
            "collector_id": collector_id,
            "rfid_uid": rfid_uid,
            "track_id": track_id,
            "trigger_status": status,
            "duplicate": bool(duplicate),
            "session_id": session_id,
            "rfid_event_id": rfid_event_id,
        }
    )
    if reason:
        event["reason"] = reason
    return reject_property_fields(event)


def evidence_ready_event(
    source_id: str,
    clip_id: str,
    file_path: str,
    start_time: float,
    end_time: float,
    track_id: Optional[int] = None,
    rfid_event_id: Optional[str] = None,
    timestamp: Optional[float] = None,
    frame_count: Optional[int] = None,
    session_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    file_url: Optional[str] = None,
    file_name: Optional[str] = None,
    content_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    sha256: Optional[str] = None,
) -> dict:
    """A clip exists on the GeoVision machine and can be fetched on demand.

    A *reference*, never the bytes. Streaming raw video to WASTRAQ over the
    site wifi would be the fastest way to lose the tracking loop.

    Two strings that must never be confused, and are kept apart here:

    ``file_path``
        Where the clip sits on *this Windows machine*. Provenance only. It is
        meaningless as a path on the Mac and WASTRAQ stores it as a
        source reference, never as something to open.
    ``file_url``
        How to *retrieve* the bytes: ``/evidence/clips/{clip_id}/file``,
        absolute when this node has been told its own reachable address.
        Derived from the clip id, so it contains no filesystem layout.

    ``content_type``, ``size_bytes`` and ``sha256`` describe what will come
    back from that URL, so WASTRAQ can decide whether to fetch, and verify
    what it got. All five are optional: a clip whose file cannot be resolved
    is still announced, with the retrieval fields absent rather than
    pointing at a URL this node cannot serve.

    ``episode_id`` is carried when the clip was triggered inside a
    collection WASTRAQ had open. Mirrored, never decided here -- and it names
    an episode, not a property.
    """

    event = _envelope(EVENT_EVIDENCE_READY, source_id, timestamp)
    event.update(
        {
            "clip_id": clip_id,
            "file_path": file_path,
            "start_time": iso_utc(start_time),
            "end_time": iso_utc(end_time),
            "track_id": track_id,
            "rfid_event_id": rfid_event_id,
            "episode_id": episode_id,
            "frame_count": frame_count,
            "session_id": session_id,
            # Retrieval. Null means "no fetchable file", not "fetch failed".
            "file_url": file_url,
            "file_name": file_name,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
        }
    )
    return reject_property_fields(event)


def heartbeat_event(
    source_id: str,
    timestamp: Optional[float] = None,
    status: Optional[dict] = None,
) -> dict:
    """Liveness. Lets WASTRAQ tell "no pickers" from "no GeoVision"."""

    event = _envelope(EVENT_HEARTBEAT, source_id, timestamp)
    event["status"] = reject_property_fields(dict(status or {}))
    return event
