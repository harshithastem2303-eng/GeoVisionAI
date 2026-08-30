"""Live diagnostics and manual controls for the WASTRAQ integration.

Built for standing in a lane with a laptop: one GET that answers "is the
camera up, is the tracker running, is WASTRAQ reachable, is anything stuck
in the queue".
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

import config
from integration import heartbeat_event
from location.base import parse_timestamp
from schemas import EvidenceRequestIn
from services import (
    clip_buffer,
    integration_status,
    publish_evidence_ready,
    publisher,
    wastraq_client,
    worker_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["integration"])


@router.get("/integration/status")
def status():
    """Everything needed to triage a live POC run, in one payload.

    Performs no outbound request: ``wastraq_reachable`` is the outcome of the
    last real delivery (``null`` if nothing has been sent yet). Use
    ``POST /integration/ping`` to force an actual round trip.
    """

    return integration_status()


@router.post("/integration/ping")
def ping():
    """Send one HEARTBEAT now and report whether WASTRAQ accepted it.

    The only endpoint here that touches the network, and it does so with the
    configured short timeout.
    """

    if not wastraq_client.configured:
        raise HTTPException(
            status_code=409,
            detail=(
                "WASTRAQ integration is disabled or WASTRAQ_BASE_URL is unset. "
                "Set WASTRAQ_INTEGRATION_ENABLED=true and WASTRAQ_BASE_URL."
            ),
        )

    payload = heartbeat_event(config.SOURCE_ID, status=integration_status())
    try:
        http_status = wastraq_client.send(payload)
    except Exception as exc:
        return {
            "delivered": False,
            "endpoint": wastraq_client.endpoint,
            "error": str(exc),
            "event_id": payload["event_id"],
        }
    return {
        "delivered": True,
        "endpoint": wastraq_client.endpoint,
        "http_status": http_status,
        "event_id": payload["event_id"],
    }


@router.get("/integration/queue")
def queue():
    """Publisher counters: accepted, delivered, retried, dropped."""

    return publisher.stats()


@router.post("/integration/evidence")
def request_evidence(payload: EvidenceRequestIn):
    """Manually capture ``T-pre .. T+post`` from the rolling buffer.

    Returns immediately with a ``PENDING`` clip; the file appears once the
    trailing seconds have elapsed, and an ``EVIDENCE_READY`` event -- carrying
    the retrieval URL -- is published then, not now.

    Repeat calls are safe and produce independent clips: each gets its own
    ``clip_id``, so WASTRAQ's ``(source_id, clip_id)`` dedup keeps one row per
    clip rather than collapsing two genuine captures into one.
    """

    try:
        trigger = parse_timestamp(payload.timestamp)
    except Exception:
        trigger = time.time()

    clip = clip_buffer.request_clip(
        trigger_time=trigger,
        track_id=payload.track_id,
        rfid_event_id=payload.rfid_event_id,
        episode_id=payload.episode_id,
        session_id=worker_registry.session_id,
        on_ready=publish_evidence_ready,
    )
    return clip.to_dict()


@router.get("/integration/evidence")
def list_evidence():
    """Recent clip requests and the state each ended in."""

    return {"buffer": clip_buffer.stats(), "clips": clip_buffer.clips()}


@router.get("/integration/evidence/{clip_id}")
def get_evidence(clip_id: str):
    clip = clip_buffer.find(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Unknown clip id")
    return clip.to_dict()
