"""The GeoVision edge receiver.

    POST /integrations/geovision/events   ingest one edge event
    GET  /integrations/geovision/status   what has arrived, and from whom
    GET  /integrations/geovision/events   recent raw envelopes (inspection)

Where this sits in the architecture
-----------------------------------
GeoVision is a PERCEPTION source: a RealSense camera, a person tracker and
an RFID reader on a Windows laptop. It reports what it saw. It does not
report which property was served, because it cannot see a service zone --
and this receiver will not let it claim otherwise (see
``schemas.FORBIDDEN_PROPERTY_FIELDS``).

WASTRAQ remains authoritative for property association. That decision is
made by the PostGIS ladder in ``app.gis``, against surveyed service-zone
polygons, and by nothing else. This module is the ingestion floor beneath
that: it accepts observations, deduplicates them, stores them, and stops.

Nothing here writes to ``collection_events`` or ``evidence``.

Speed
-----
The edge publishes TRACK_UPDATE at ~5 Hz per track and gives up on a
request after 2 seconds. Every accepted event costs three small statements
on one connection and no spatial work at all; an ack is a few hundred
bytes. If association work is ever added it belongs behind this endpoint,
reading ``geovision_raw_events``, not inside the request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import ValidationError

from ..config import settings
from . import service
from .schemas import EVENT_ADAPTER, EVENT_TYPES, IngestAck

router = APIRouter(prefix="/integrations/geovision", tags=["integrations"])


@router.post("/events", response_model=IngestAck, status_code=202)
def ingest_event(response: Response, payload: dict[str, Any] = Body(...)):
    """Accept one GeoVision edge event.

    202 for a new event, 200 for one already seen. Both are 2xx on purpose:
    the sender treats any non-2xx as a delivery failure and requeues the
    event with the same ``event_id``, so answering a duplicate with 409
    would produce an event that retries until it is dropped. A duplicate is
    not an error -- it is the retry queue working.

    422 for a payload that does not match the contract. That one the sender
    should retry and then drop; a malformed event will not become valid by
    being sent again, and it must not reach the database.
    """
    try:
        event = EVENT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        service.record_rejected()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_GEOVISION_EVENT",
                "accepted_event_types": list(EVENT_TYPES),
                "errors": [
                    {
                        "loc": [str(p) for p in err.get("loc", ())],
                        "msg": err.get("msg", ""),
                        "type": err.get("type", ""),
                    }
                    for err in exc.errors()[:12]
                ],
            },
        ) from exc

    result = service.ingest(event, payload)

    if result["duplicate"]:
        # Idempotent: this event_id was already stored and nothing was
        # written again. 200, not 202 - nothing was created this time.
        response.status_code = 200

    return result


@router.get("/status")
def integration_status(
    stale_after_s: float | None = Query(
        None, gt=0, le=3600,
        description="A track older than this is not counted as active.",
    ),
):
    """What the edge has actually delivered.

    Reads only. It makes no outbound request to GeoVision - a status
    endpoint that itself blocks on an unreachable host is useless during
    exactly the outage you are trying to diagnose.
    """
    stale = stale_after_s or settings.GEOVISION_TRACK_STALE_S
    tracks = service.active_tracks(stale)
    device_rows = service.devices()

    online = [
        d for d in device_rows
        if (d.get("seconds_since_seen") or 1e9) <= settings.GEOVISION_DEVICE_STALE_S
    ]

    return {
        "receiver": "WASTRAQ GeoVision ingestion",
        "endpoint": "/integrations/geovision/events",
        "accepted_event_types": list(EVENT_TYPES),
        "enabled": settings.GEOVISION_ENABLED,
        "now": datetime.now(timezone.utc),

        # Authority statement, in the payload rather than only in the docs,
        # so anyone reading this endpoint knows what these numbers are not.
        "property_association": {
            "performed_here": False,
            "authority": "WASTRAQ PostGIS service-zone association (POST /gis/lookup)",
            "note": "GeoVision events carry no property. RFID identifies who and when, not where.",
        },

        "totals": service.totals(),
        "by_event_type": service.ingest_summary(),
        # Since this process started - the database cannot count events it
        # was never allowed to store.
        "since_restart": service.counters(),

        "devices": device_rows,
        "devices_online": len(online),
        "device_stale_after_s": settings.GEOVISION_DEVICE_STALE_S,

        "active_tracks": tracks,
        "active_track_count": len(tracks),
        "track_stale_after_s": stale,

        "recent_rfid_taps": service.recent_rfid_taps(),
        "recent_worker_bindings": service.recent_bindings(),
        "recent_evidence_clips": service.recent_clips(),
    }


@router.get("/events")
def list_raw_events(
    limit: int = Query(20, ge=1, le=200),
    event_type: str | None = Query(None),
):
    """Recent raw envelopes. Inspection aid - the payloads themselves stay
    in the table; this is the index, not a firehose."""
    if event_type and event_type not in EVENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"event_type must be one of {list(EVENT_TYPES)}",
        )
    return {"events": service.raw_events(limit=limit, event_type=event_type)}
