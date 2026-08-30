"""Camera lifecycle, tracked people, worker observations, MJPEG stream."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from services import episode_registry, pipeline, worker_registry
from stream import video_stream
from vision.camera import CameraUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vision"])


@router.post("/connect")
def connect():
    """Open the camera. Reports the negotiated profile on success."""

    try:
        camera = pipeline.connect()
    except CameraUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"camera": pipeline.camera_connected, "detail": camera}


@router.post("/start")
def start():
    """Begin capture under a fresh identity session.

    Bindings, and with them the mirrored episodes, are dropped: BoT-SORT
    renumbers on restart, so track 35 in the new run is a different human and
    anything keyed on the old number is meaningless.
    """

    try:
        session_id = pipeline.start()
    except CameraUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    episode_registry.clear()
    return {"recording": True, "session_id": session_id}


@router.post("/stop")
def stop():
    pipeline.stop()
    return {"recording": False}


@router.post("/disconnect")
def disconnect():
    pipeline.disconnect()
    return {"camera": False}


@router.get("/stats")
def stats():
    return pipeline.stats()


@router.get("/people")
def people():
    """Every tracked person, authorised or not.

    Pedestrians are included with ``is_authorized_picker: false``. Filtering
    them out is the caller's decision, not the pipeline's.

    Each entry carries the normalised WASTRAQ track shape -- ``bbox`` as an
    object, ``depth_m`` and camera-relative XYZ, ``depth_valid`` -- plus
    ``id`` / ``confidence`` / ``bbox_xyxy`` aliases so older consumers keep
    working.
    """

    return pipeline.tracks()


@router.get("/tracks")
def tracks():
    """Alias of :func:`people`, named for what WASTRAQ ingests.

    Same payload, same envelope. Two names because the dashboard has always
    called it "people" and the integration contract calls it "tracks";
    renaming either would break something for no gain.
    """

    return pipeline.tracks()


@router.get("/observations")
def observations():
    """Full worker observations, shaped for ingestion by WASTRAQ."""

    return {
        "session_id": worker_registry.session_id,
        "observations": pipeline.observations(),
    }


@router.get("/video_feed")
def video_feed():
    return video_stream.response()


@router.get("/camera")
def camera():
    return pipeline.describe_camera()
