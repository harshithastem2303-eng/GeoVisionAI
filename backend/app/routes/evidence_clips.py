"""Rolling video-evidence clips.

Read/trigger API for `app.evidence_buffer`. Deliberately NOT under the
`/evidence` prefix beyond the one documented trigger path: `routes/evidence.py`
already owns `GET /evidence` (the database rows). These endpoints are about
the MP4 files, so they live on `/evidence-clips`, and the files themselves
are served from `/evidence-files/<name>` (mounted in main.py).

    POST /evidence/not-segregated        trigger a clip from the buffer
    GET  /evidence-clips/status          buffer health
    GET  /evidence-clips                 recent clips
    GET  /evidence-clips/{evidence_id}   one clip's metadata
    GET  /evidence-clips/{evidence_id}/file   the MP4 itself

The normal path is not this router: marking an event NOT_SEGREGATED via
POST /collection-events/{event_id}/non-segregated already triggers a clip
and links it as a VIDEO_CLIP evidence row. This router exists for the
camera-only case (no collection event yet) and for inspection.
"""

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..evidence_buffer import evidence_recorder

router = APIRouter(tags=["evidence-clips"])


class NotSegregatedClipRequest(BaseModel):
    """Everything optional. RFID only identifies the picker at the start of
    the demo and the GIS matcher may not have resolved a property yet, so an
    unknown field is stored as null rather than invented."""

    picker_id: str | None = None
    property_id: str | None = None
    camera_id: str | None = None
    event_id: str | None = None
    event_type: str = "NOT_SEGREGATED"
    note: str | None = None
    pre_seconds: float | None = None
    post_seconds: float | None = None
    # Block until the file is on disk. Off by default; useful in scripts.
    wait: bool = False


@router.post("/evidence/not-segregated")
def create_not_segregated_clip(body: NotSegregatedClipRequest | None = None):
    """Freeze the rolling buffer and write the seconds leading up to now.

    200 + status=RECORDING immediately (poll GET /evidence-clips/{id}),
    or status=SAVED when `wait` is true. 409 when there is no footage --
    the vision pipeline is not running.
    """

    body = body or NotSegregatedClipRequest()

    extra = {k: v for k, v in
             (("note", body.note), ("event_id", body.event_id)) if v}

    meta = evidence_recorder.trigger(
        property_id=body.property_id,
        picker_id=body.picker_id,
        camera_id=body.camera_id,
        event_type=body.event_type or "NOT_SEGREGATED",
        pre_seconds=body.pre_seconds,
        post_seconds=body.post_seconds,
        extra=extra or None,
    )

    if meta.get("status") == "FAILED":
        raise HTTPException(status_code=409, detail=meta.get("error"))

    if body.wait:
        meta = evidence_recorder.wait_for(meta["evidence_id"], timeout=30.0) or meta

    return meta


@router.get("/evidence-clips/status")
def clip_status():
    """Buffer health. Use this to confirm frames are actually arriving."""
    return evidence_recorder.status()


@router.get("/evidence-clips")
def list_clips(limit: int = Query(50, ge=1, le=500)):
    return {"clips": evidence_recorder.list(limit=limit)}


@router.get("/evidence-clips/{evidence_id}")
def clip_detail(evidence_id: str):
    record = evidence_recorder.get(evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown evidence clip")
    return record


@router.get("/evidence-clips/{evidence_id}/file")
def clip_file(evidence_id: str):
    record = evidence_recorder.get(evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown evidence clip")

    path = record.get("video_path")
    if not path or not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail=f"Clip is not on disk yet (status={record.get('status')})",
        )

    return FileResponse(path, media_type="video/mp4",
                        filename=os.path.basename(path))
