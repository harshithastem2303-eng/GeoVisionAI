from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from ..database import execute, fetch_all, fetch_one
from ..gis import lookup_property
from ..models import fake_evidence_path, next_event_id, next_evidence_id
from ..tracing import annotate_run
from ..schemas import (
    CollectionEventCreate,
    CollectionEventOut,
    CollectionEventWithEvidence,
    NonSegregationRequest,
)

# Rolling video evidence. Optional -- a failure here must not stop the
# status write, which is the operation the demo depends on.
try:
    from ..evidence_buffer import evidence_recorder
except Exception as _evidence_error:  # noqa: BLE001
    evidence_recorder = None
    print("!! Evidence buffer unavailable:", repr(_evidence_error))

router = APIRouter(prefix="/collection-events", tags=["collection-events"])


@router.get("", response_model=list[CollectionEventOut])
def list_events(
    segregation_status: str | None = Query(None),
    picker_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    clauses, params = [], []
    if segregation_status:
        clauses.append("segregation_status = %s")
        params.append(segregation_status)
    if picker_id:
        clauses.append("picker_id = %s")
        params.append(picker_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return fetch_all(
        f"SELECT * FROM collection_events {where} ORDER BY collection_time DESC LIMIT %s",
        tuple(params),
    )


@router.get("/feed/detailed", tags=["dashboard"])
def event_feed(
    route_id: str | None = Query(None, description="defaults to the operations route"),
    segregation_status: str | None = None,
    review_status: str | None = None,
    picker_id: str | None = None,
    property_id: str | None = None,
    since_hours: int | None = Query(None, ge=1, le=8760),
    q: str | None = None,
    limit: int = Query(80, ge=1, le=500),
):
    """The dashboard event feed: one row per event with everything it shows."""
    params = {
        "route_id": route_id or settings.DEMO_ROUTE_ID,
        "seg": segregation_status,
        "rev": review_status,
        "picker": picker_id,
        "prop": property_id,
        "hours": since_hours,
        "q": f"%{q}%" if q else None,
        "limit": limit,
    }
    return fetch_all(
        """
        SELECT ce.event_id, ce.property_id, ce.picker_id, ce.track_id, ce.collected,
               ce.segregation_status, ce.association_confidence, ce.collection_time,
               ce.rfid_triggered, ce.review_status,
               p.house_number, p.owner_name, p.formatted_address, p.route_id,
               pk.picker_name,
               (SELECT count(*) FROM evidence e WHERE e.event_id = ce.event_id) AS evidence_count
        FROM collection_events ce
        JOIN properties p ON p.property_id = ce.property_id
        LEFT JOIN pickers pk ON pk.picker_id = ce.picker_id
        WHERE (%(route_id)s::text IS NULL OR p.route_id = %(route_id)s)
          AND (%(seg)s::text    IS NULL OR ce.segregation_status = %(seg)s)
          AND (%(rev)s::text    IS NULL OR ce.review_status = %(rev)s)
          AND (%(picker)s::text IS NULL OR ce.picker_id = %(picker)s)
          AND (%(prop)s::text   IS NULL OR ce.property_id = %(prop)s)
          AND (%(hours)s::int   IS NULL OR ce.collection_time >= now()
                                 - (%(hours)s::text || ' hours')::interval)
          AND (%(q)s::text      IS NULL OR ce.property_id ILIKE %(q)s OR p.owner_name ILIKE %(q)s
                                  OR p.house_number ILIKE %(q)s)
        ORDER BY ce.collection_time DESC
        LIMIT %(limit)s
        """,
        params,
    )


@router.post("", response_model=CollectionEventOut, status_code=201)
def create_event(req: CollectionEventCreate):
    """Create a collection event.

    Either pass `property_id` directly, or pass latitude/longitude and let
    PostGIS decide. An AMBIGUOUS or NO_MATCH lookup is rejected with 409 -
    the engine will not invent an association.
    """
    property_id = req.property_id
    confidence = req.association_confidence
    review_status = "AUTO_CONFIRMED"

    if property_id is None:
        if req.latitude is None or req.longitude is None:
            raise HTTPException(
                status_code=422,
                detail="Provide either property_id, or latitude and longitude.",
            )
        result = lookup_property(req.latitude, req.longitude)
        if result["decision"] != "AUTO_ASSOCIATED":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Property could not be associated unambiguously.",
                    "lookup": result,
                },
            )
        property_id = result["property_id"]
        confidence = result["confidence"]

    if not fetch_one("SELECT 1 AS ok FROM properties WHERE property_id = %s", (property_id,)):
        raise HTTPException(status_code=404, detail=f"Unknown property {property_id}")

    if req.picker_id and not fetch_one(
        "SELECT 1 AS ok FROM pickers WHERE picker_id = %s", (req.picker_id,)
    ):
        raise HTTPException(status_code=404, detail=f"Unknown picker {req.picker_id}")

    if confidence is not None and confidence < 0.85:
        review_status = "NEEDS_REVIEW"

    event_id = next_event_id()
    row = execute(
        """
        INSERT INTO collection_events (
            event_id, property_id, picker_id, track_id, collected,
            segregation_status, association_confidence, collection_time,
            rfid_triggered, review_status
        ) VALUES (
            %(event_id)s, %(property_id)s, %(picker_id)s, %(track_id)s, %(collected)s,
            %(segregation_status)s, %(confidence)s, COALESCE(%(collection_time)s, now()),
            %(rfid)s, %(review_status)s
        )
        RETURNING *;
        """,
        {
            "event_id": event_id,
            "property_id": property_id,
            "picker_id": req.picker_id,
            "track_id": req.track_id,
            "collected": req.collected,
            "segregation_status": req.segregation_status,
            "confidence": confidence,
            "collection_time": req.collection_time,
            "rfid": req.segregation_status == "NOT_SEGREGATED",
            "review_status": review_status,
        },
    )
    # Label the trace with the identifiers a dispute would arrive quoting.
    # No-op when tracing is off.
    annotate_run(
        event_id=event_id,
        property_id=property_id,
        picker_id=req.picker_id,
        segregation_status=req.segregation_status,
        association_confidence=confidence,
        review_status=review_status,
        associated_by="lookup" if req.property_id is None else "caller",
    )
    return row


@router.get("/{event_id}", response_model=CollectionEventWithEvidence)
def get_event(event_id: str):
    row = fetch_one("SELECT * FROM collection_events WHERE event_id = %s", (event_id,))
    if not row:
        raise HTTPException(status_code=404, detail=f"Unknown event {event_id}")
    row["evidence"] = fetch_all(
        "SELECT * FROM evidence WHERE event_id = %s ORDER BY captured_at", (event_id,)
    )
    return row


@router.post("/{event_id}/non-segregated", response_model=CollectionEventWithEvidence)
def mark_non_segregated(event_id: str, req: NonSegregationRequest):
    """The picker's exception action.

    Flips the event to NOT_SEGREGATED, records that RFID raised it, and links
    an evidence record. The original event row is preserved and updated in
    place - nothing is deleted or re-created.
    """
    event = fetch_one("SELECT * FROM collection_events WHERE event_id = %s", (event_id,))
    if not event:
        raise HTTPException(status_code=404, detail=f"Unknown event {event_id}")

    picker_id = req.picker_id or event["picker_id"]
    if req.rfid_uid:
        picker = fetch_one("SELECT * FROM pickers WHERE rfid_uid = %s", (req.rfid_uid,))
        if not picker:
            raise HTTPException(status_code=404, detail=f"Unknown RFID tag {req.rfid_uid}")
        picker_id = picker["picker_id"]

    updated = execute(
        """
        UPDATE collection_events
           SET segregation_status = 'NOT_SEGREGATED',
               rfid_triggered     = TRUE,
               picker_id          = COALESCE(%(picker_id)s, picker_id),
               review_status      = CASE
                                      WHEN review_status IN ('REVIEWED_OK','REVIEWED_REJECTED')
                                      THEN review_status ELSE 'NEEDS_REVIEW'
                                    END
         WHERE event_id = %(event_id)s
        RETURNING *;
        """,
        {"event_id": event_id, "picker_id": picker_id},
    )

    if req.create_evidence:
        now = datetime.now(timezone.utc)
        execute(
            """
            INSERT INTO evidence (evidence_id, event_id, evidence_type, file_path, captured_at, verified)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            """,
            (
                next_evidence_id(),
                event_id,
                req.evidence_type,
                req.file_path or fake_evidence_path(event_id, req.evidence_type, now),
                now,
            ),
        )

    # -----------------------------------------------------------------
    # VIDEO EVIDENCE
    #
    # Freeze the rolling camera buffer and save the ~15 s around this
    # decision. trigger() returns in about a millisecond; the MP4 is
    # encoded on a worker thread and lands ~3 s later (the post-roll), so
    # the row's file_path is written before the file exists. That is what
    # verified=FALSE already means here.
    #
    # This is ADDITIVE: the NON_SEGREGATION_PROOF row above is unchanged,
    # and the clip is a second row of type VIDEO_CLIP.
    # -----------------------------------------------------------------
    clip = None
    if req.capture_video and evidence_recorder is not None:
        try:
            clip = evidence_recorder.trigger(
                property_id=updated.get("property_id"),
                picker_id=picker_id,
                event_type="NOT_SEGREGATED",
                extra={"event_id": event_id, "note": req.note},
            )
        except Exception as exc:  # noqa: BLE001
            clip = {"status": "FAILED", "error": repr(exc)}

        if clip.get("status") != "FAILED":
            execute(
                """
                INSERT INTO evidence (evidence_id, event_id, evidence_type, file_path, captured_at, verified)
                VALUES (%s, %s, %s, %s, %s, FALSE)
                """,
                (
                    next_evidence_id(),
                    event_id,
                    "VIDEO_CLIP",
                    clip["video_path"],
                    datetime.now(timezone.utc),
                ),
            )

    updated["evidence"] = fetch_all(
        "SELECT * FROM evidence WHERE event_id = %s ORDER BY captured_at", (event_id,)
    )
    annotate_run(
        event_id=event_id,
        property_id=updated.get("property_id"),
        picker_id=picker_id,
        action="mark_non_segregated",
        rfid_uid=req.rfid_uid,
        evidence_created=bool(req.create_evidence),
        evidence_clip=(clip or {}).get("evidence_id"),
    )
    return updated
