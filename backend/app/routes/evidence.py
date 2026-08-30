"""Evidence records, and the one endpoint that serves evidence bytes.

    GET  /collection-events/{event_id}/evidence   rows for one event
    POST /collection-events/{event_id}/evidence   attach a row by hand
    GET  /evidence                                recent rows
    GET  /evidence/{evidence_id}                  one row + media state
    GET  /evidence/{evidence_id}/media            the bytes, for a <video>
    POST /evidence/{evidence_id}/fetch            pull it from the edge now
    GET  /evidence-media/status                   what is held, what is not
    POST /evidence-media/retry                    re-attempt every unheld clip

The media endpoint is the only browser-facing way to reach evidence bytes,
and it takes an ``evidence_id`` - never a path. The path comes from the
database, is resolved inside the evidence root, and is refused if it lands
anywhere else (``evidence_media.safe_local_path``). There is no input a
caller can supply that names a file, so there is nothing to traverse with.

A GeoVision clip whose bytes are not on this Mac answers 409 with the
reason, not 200 with a Windows path. "We do not have it yet" is a true and
useful answer; a path the browser cannot open is neither.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import evidence_media
from ..database import execute, fetch_all, fetch_one
from ..models import fake_evidence_path, next_evidence_id
from ..schemas import EvidenceCreate, EvidenceOut

router = APIRouter(tags=["evidence"])


def _require_event(event_id: str) -> None:
    if not fetch_one("SELECT 1 AS ok FROM collection_events WHERE event_id = %s",
                     (event_id,)):
        raise HTTPException(status_code=404, detail=f"Unknown event {event_id}")


@router.get("/collection-events/{event_id}/evidence", response_model=list[EvidenceOut])
def list_evidence(event_id: str):
    _require_event(event_id)
    return evidence_media.enrich(evidence_media.evidence_for_event(event_id))


@router.post(
    "/collection-events/{event_id}/evidence", response_model=EvidenceOut, status_code=201
)
def add_evidence(event_id: str, req: EvidenceCreate):
    """Attach evidence to an event by hand.

    Still accepts a caller-supplied ``file_path`` - the demo's manual and
    simulated flows depend on it - but that string is only ever a label
    until it resolves to a real file inside the evidence root. It is not a
    way to make the media endpoint serve an arbitrary path: that endpoint
    re-resolves through ``safe_local_path`` on every request.
    """
    _require_event(event_id)
    captured = req.captured_at or datetime.now(timezone.utc)
    row = execute(
        """
        INSERT INTO evidence (evidence_id, event_id, evidence_type, file_path,
                              captured_at, verified)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING evidence_id;
        """,
        (
            next_evidence_id(),
            event_id,
            req.evidence_type,
            req.file_path or fake_evidence_path(event_id, req.evidence_type, captured),
            captured,
            req.verified,
        ),
    )
    return _one(row["evidence_id"])


@router.get("/evidence", response_model=list[EvidenceOut])
def all_evidence(limit: int = Query(200, ge=1, le=1000)):
    return evidence_media.enrich(
        fetch_all("SELECT * FROM v_evidence_media ORDER BY captured_at DESC LIMIT %s",
                  (limit,))
    )


# --- media -------------------------------------------------------------------
# Registered BEFORE /evidence/{evidence_id} so the literal path wins: FastAPI
# resolves in registration order and `/evidence-media/status` is a different
# prefix, but keeping the fixed routes first is the habit that stops the next
# addition from being swallowed by the parameterised one.
@router.get("/evidence-media/status", tags=["evidence-clips"])
def media_status():
    """What this Mac actually holds. The honest answer to "is evidence ready".

    Reads only; it makes no request to the edge. A status endpoint that
    blocks on an unreachable Windows machine is useless during exactly the
    outage it is meant to describe.
    """
    return evidence_media.media_status_summary()


@router.post("/evidence-media/retry", tags=["evidence-clips"])
def media_retry(limit: int = Query(20, ge=1, le=200)):
    """Pull every clip that was announced but is not on disk.

    This is the answer to "Windows was off when the clip was recorded".
    Nothing was lost: the announcement is stored, and the bytes come across
    the next time anyone asks.
    """
    results = evidence_media.retry_pending(limit=limit)
    return {
        "attempted": len(results),
        "stored": sum(1 for r in results if r.get("status") == "STORED"),
        "results": results,
    }


@router.get("/evidence/{evidence_id}", response_model=EvidenceOut)
def get_evidence(evidence_id: str):
    return _one(evidence_id)


@router.post("/evidence/{evidence_id}/fetch")
def fetch_evidence_media(evidence_id: str, force: bool = False):
    """Pull this one clip from the edge now, and report what happened."""
    row = evidence_media.evidence_media_row(evidence_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown evidence {evidence_id}")
    clip_event_id = row.get("clip_event_id")
    if not clip_event_id:
        raise HTTPException(
            status_code=409,
            detail=("This evidence record is not a GeoVision clip; there is "
                    "nothing to fetch from the edge."),
        )
    result = evidence_media.fetch_clip(clip_event_id, force=force)
    return {"evidence_id": evidence_id, **result,
            **evidence_media.describe(
                evidence_media.evidence_media_row(evidence_id) or row)}


@router.get("/evidence/{evidence_id}/media")
def evidence_media_file(evidence_id: str):
    """The bytes, for an ``<video>`` or ``<img>`` element.

    ``FileResponse`` handles HTTP Range, so seeking inside the clip works
    without this route knowing anything about byte ranges.

    A GeoVision clip that has not been pulled yet is fetched here, once, on
    the first request - so a demo that never went near the fetch endpoint
    still plays as long as the edge is reachable at the moment someone
    clicks. If it is not reachable, the answer is 409 and the reason.
    """
    path, described = evidence_media.playable_file(evidence_id)

    if not described:
        raise HTTPException(status_code=404, detail=f"Unknown evidence {evidence_id}")

    if path is None and described.get("clip_event_id"):
        # Lazy pull. Bounded by GEOVISION_CLIP_FETCH_TIMEOUT_S.
        evidence_media.fetch_clip(described["clip_event_id"])
        path, described = evidence_media.playable_file(evidence_id)

    if path is None or not path.is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "EVIDENCE_MEDIA_NOT_HELD",
                "evidence_id": evidence_id,
                "media_status": described.get("media_status"),
                "fetch_status": described.get("fetch_status"),
                "fetch_error": described.get("fetch_error"),
                # STEP 4C: identity, not location. This detail is rendered
                # verbatim by the dashboard when a <video> fails to load,
                # so it must not carry a path from the GeoVision machine -
                # an operator cannot act on one, and an error message is
                # exactly where a stray path ends up in a screenshot.
                "source_label": described.get("source_label"),
                "clip_id": described.get("clip_id"),
                "hint": ("The clip was announced by GeoVision but its bytes are "
                         "not on this Mac. POST /evidence/{id}/fetch, or "
                         "POST /evidence-media/retry once the edge is reachable."),
            },
        )

    # Derived from the file being opened, not only from the row: the header
    # has to describe the bytes actually leaving this process, and
    # `content_type_for` is what reconciles the edge's declared type with
    # the extension on disk. FileResponse still does Range itself.
    return FileResponse(
        path,
        media_type=evidence_media.content_type_for(
            path.name, described.get("media_content_type")),
        filename=path.name,
    )


def _one(evidence_id: str) -> dict:
    row = evidence_media.evidence_media_row(evidence_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown evidence {evidence_id}")
    return {**row, **evidence_media.describe(row)}
