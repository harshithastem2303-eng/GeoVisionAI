"""RFID tap ingestion, worker binding, and the resulting WASTRAQ events.

Current support level, stated plainly: **there is no RFID reader driver in
this repository.** Nothing here reads a serial port, a USB HID device or an
MQTT topic. Taps arrive as HTTP POSTs from whatever bridges the physical
reader. ``/integration/status`` reports ``rfid_available: false`` and
``rfid_mode: API_INGEST_ONLY`` so no one mistakes this for hardware
acquisition -- when a real reader is wired in, it posts to the same endpoint
and nothing below changes.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

import config
from integration import (
    non_segregation_trigger_event,
    rfid_tap_event,
    worker_track_bound_event,
)
from location.base import parse_timestamp
from schemas import RFIDEventIn
from services import (
    clip_buffer,
    episode_registry,
    integration_state,
    publish_evidence_ready,
    publisher,
    rfid_service,
    worker_registry,
)
from vision.types import BindingStatus, TapIntent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rfid"])

#: Fired on the evidence thread once the clip file is closed and in place.
#: The event it publishes carries the retrieval URL, so it must not run a
#: moment earlier. Shared with the manual capture endpoint so both announce
#: the same shape.
_publish_evidence_ready = publish_evidence_ready


@router.post("/rfid/events")
def rfid_event(event: RFIDEventIn):
    """One RFID tap. What it means depends on the state it lands in.

    **No collector bound** (``intent: BIND``) -- identify the tapper and lock
    them to a camera track:

    * ``BOUND`` -- collector and track resolved unambiguously; track locked.
      A repeat read of the same card within ``RFID_BIND_ECHO_S`` answers
      ``BOUND`` again with ``duplicate: true`` and changes nothing
    * ``AMBIGUOUS`` -- two people equally plausible; nothing bound
    * ``NO_TRACK_IN_READER_ZONE`` -- nobody at the reader and no depth to
      rank people by distance
    * ``NO_TRACK_DATA`` -- the camera was not running around that timestamp
    * ``UNKNOWN_RFID`` -- the tag is not assigned to a collector

    **Collector already bound** (``intent: NON_SEGREGATION``) -- the same card
    now means "the waste at the property I am servicing is not segregated":

    * ``NON_SEGREGATION`` -- flagged; evidence clip requested
    * ``DUPLICATE_TRIGGER`` -- already flagged; nothing changed
    * ``NO_ACTIVE_EPISODE`` -- no collection open on that track. **Nothing is
      marked** -- not the previous property, not a nearby one
    * ``EPISODE_NOT_ACTIONABLE`` -- WASTRAQ has not associated the episode
      confidently enough to act on

    Always HTTP 200: an unresolved tap is a real, informative outcome, not a
    transport error, and the reader bridge should record it rather than retry
    it.

    Every outcome is forwarded to WASTRAQ as an ``RFID_TAP`` event --
    including the unresolved ones, with ``track_id: null`` and the candidate
    list attached. Ambiguity is data WASTRAQ needs, not a problem to hide.
    """

    try:
        timestamp = parse_timestamp(event.timestamp)
    except Exception:
        timestamp = time.time()

    result = rfid_service.handle_tap(event.uid, timestamp)
    status = result["status"]
    intent = result.get("intent")
    bound = status == BindingStatus.BOUND
    flagged = status in (
        BindingStatus.NON_SEGREGATION,
        BindingStatus.DUPLICATE_TRIGGER,
    )
    result["resolved"] = bound or flagged

    # --- outbound: the tap itself ---------------------------------------
    tap_event = rfid_tap_event(
        source_id=config.RFID_SOURCE_ID,
        rfid_uid=event.uid,
        collector_id=result.get("collector_id"),
        track_id=result.get("track_id"),
        status=status,
        timestamp=timestamp,
        candidate_track_ids=result.get("candidate_track_ids"),
        confidence=result.get("confidence", 0.0),
        session_id=worker_registry.session_id,
        reason=result.get("reason", ""),
    )
    publisher.publish(tap_event)
    integration_state.record_rfid(tap_event["event_id"], status, when=timestamp)
    result["event_id"] = tap_event["event_id"]

    if intent == TapIntent.NON_SEGREGATION:
        return _non_segregation(event, result, tap_event, timestamp)

    if not bound:
        return result

    if result.get("duplicate"):
        # The identifying tap arriving twice. The tap itself is reported above
        # -- WASTRAQ should see that the reader fired again -- but nothing
        # downstream of the binding runs a second time: no second
        # WORKER_TRACK_BOUND, no second evidence clip.
        logger.info(
            "Duplicate identifying tap for %s on track %s; binding unchanged",
            result.get("collector_id"),
            result.get("track_id"),
        )
        return result

    # --- outbound: the binding it produced -------------------------------
    publisher.publish(
        worker_track_bound_event(
            source_id=config.SOURCE_ID,
            collector_id=result["collector_id"],
            rfid_uid=event.uid,
            track_id=result["track_id"],
            confidence=result.get("confidence", 0.0),
            session_id=result.get("session_id", worker_registry.session_id),
            timestamp=timestamp,
            rfid_event_id=tap_event["event_id"],
        )
    )

    # --- evidence --------------------------------------------------------
    if config.EVIDENCE_AUTO_ON_RFID:
        clip = clip_buffer.request_clip(
            trigger_time=timestamp,
            track_id=result["track_id"],
            rfid_event_id=tap_event["event_id"],
            session_id=worker_registry.session_id,
            on_ready=_publish_evidence_ready,
        )
        result["evidence"] = clip.to_dict()

    return result


def _non_segregation(event, result: dict, tap_event: dict, timestamp: float) -> dict:
    """Publish a re-tap outcome, and capture evidence if it was accepted.

    The unresolved outcomes are published too, and deliberately. WASTRAQ owns
    the authoritative episode table; this node holds a mirror that can be
    behind it. A ``NO_ACTIVE_EPISODE`` reaching the Mac is a fact WASTRAQ can
    act on -- or correct -- rather than a tap that silently vanished.
    """

    status = result["status"]
    accepted = status == BindingStatus.NON_SEGREGATION

    publisher.publish(
        non_segregation_trigger_event(
            source_id=config.RFID_SOURCE_ID,
            # No local trigger id when nothing was flagged; the tap's own
            # event id is still the join key.
            trigger_id=result.get("trigger_id") or tap_event["event_id"],
            episode_id=result.get("episode_id"),
            collector_id=result.get("collector_id"),
            rfid_uid=event.uid,
            track_id=result.get("track_id"),
            status=status,
            timestamp=timestamp,
            session_id=result.get("session_id", worker_registry.session_id),
            rfid_event_id=tap_event["event_id"],
            duplicate=status == BindingStatus.DUPLICATE_TRIGGER,
            reason=result.get("reason", ""),
        )
    )

    # A clip only for the flag that actually landed. A duplicate already has
    # one, and a tap that marked nothing has nothing to evidence.
    if accepted and config.EVIDENCE_AUTO_ON_NON_SEGREGATION:
        clip = clip_buffer.request_clip(
            trigger_time=timestamp,
            track_id=result.get("track_id"),
            rfid_event_id=tap_event["event_id"],
            # The episode WASTRAQ had open on this track. Carried so the clip
            # arrives already joined to the collection it evidences, rather
            # than only to the tap.
            episode_id=result.get("episode_id"),
            session_id=worker_registry.session_id,
            on_ready=_publish_evidence_ready,
        )
        result["evidence"] = clip.to_dict()

    return result


@router.get("/rfid/zone")
def rfid_zone():
    """The configured evidence zone, so the dashboard can draw it."""

    return {
        "zone": rfid_service.zone.to_dict(),
        "valid": rfid_service.zone.is_valid(),
        "match_window_s": rfid_service.match_window_s,
        "min_overlap": rfid_service.min_overlap,
        "ambiguity_margin": rfid_service.ambiguity_margin,
        "depth_margin_m": rfid_service.depth_margin_m,
        # Repeat reads of the same card inside this window are the same tap.
        "bind_echo_s": rfid_service.bind_echo_s,
        # Strongest evidence first. The zone is preferred, not required: if it
        # singles nobody out, proximity decides and the result says so.
        "selection_order": ["DEPTH_IN_ZONE", "DEPTH_ANY", "ZONE_OVERLAP"],
    }


@router.get("/worker-bindings")
def worker_bindings():
    """Live ``collector_id <-> track_id`` bindings for this session."""

    worker_registry.expire()
    bindings = worker_registry.to_list()
    return {
        "session_id": worker_registry.session_id,
        "count": len(bindings),
        "grace_s": worker_registry.grace_s,
        # The tracks collection logic follows exclusively.
        "active_picker_tracks": worker_registry.active_track_ids(),
        "bindings": bindings,
    }


@router.delete("/worker-bindings/{collector_id}")
def release_binding(collector_id: str):
    """Manually release a binding, e.g. at end of shift.

    Also drops the mirrored episode on that track: without a locked picker
    there is nobody a re-tap could belong to, and a stranded episode would
    only wait to absorb the wrong one.
    """

    binding = worker_registry.binding_for_collector(collector_id)
    released = worker_registry.release(collector_id)
    if binding is not None:
        episode_registry.close(track_id=binding.track_id)
    return {
        "released": released,
        "collector_id": collector_id,
        "track_id": binding.track_id if binding else None,
    }
