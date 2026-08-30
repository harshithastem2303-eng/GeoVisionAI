"""Outbound integration with the WASTRAQ backend.

The architectural boundary this package exists to hold::

    GeoVision (here)                  WASTRAQ (elsewhere)
    ----------------                  -------------------
    person detection                  Property Master
    persistent track ids              PostGIS service zones
    per-track depth + camera XYZ      world trajectory
    absolute timestamps               collection episodes
    RFID tap -> track attribution     candidate properties
    non-segregation trigger signal    segregation status + review policy
    coarse location                   property matcher
    evidence clip references          confidence / ambiguity policy
                                      final property association

GeoVision reports **what the camera saw**. It never reports which house was
serviced -- there is no property id in any event this package emits, and
:func:`events.reject_property_fields` enforces that at runtime rather than
trusting the builders.

Modules::

    events.py      event construction + the no-property-association guard
    client.py      HTTP transport to WASTRAQ (stdlib only, short timeout)
    publisher.py   rate limiting, bounded retry queue, background sender
"""

from .client import TransportError, WastraqClient
from .events import (
    EVENT_EVIDENCE_READY,
    EVENT_HEARTBEAT,
    EVENT_NON_SEGREGATION_TRIGGER,
    EVENT_RFID_TAP,
    EVENT_TRACK_UPDATE,
    EVENT_WORKER_TRACK_BOUND,
    PROPERTY_FIELDS,
    bbox_dict,
    evidence_ready_event,
    heartbeat_event,
    iso_utc,
    new_event_id,
    non_segregation_trigger_event,
    reject_property_fields,
    rfid_tap_event,
    track_update_event,
    worker_track_bound_event,
)
from .publisher import EventPublisher

__all__ = [
    "TransportError",
    "WastraqClient",
    "EventPublisher",
    "EVENT_TRACK_UPDATE",
    "EVENT_RFID_TAP",
    "EVENT_WORKER_TRACK_BOUND",
    "EVENT_NON_SEGREGATION_TRIGGER",
    "EVENT_EVIDENCE_READY",
    "EVENT_HEARTBEAT",
    "PROPERTY_FIELDS",
    "bbox_dict",
    "iso_utc",
    "new_event_id",
    "reject_property_fields",
    "track_update_event",
    "rfid_tap_event",
    "worker_track_bound_event",
    "non_segregation_trigger_event",
    "evidence_ready_event",
    "heartbeat_event",
]
