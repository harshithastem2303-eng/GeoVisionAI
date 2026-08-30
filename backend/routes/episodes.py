"""Collection episodes, mirrored from WASTRAQ.

GeoVision does not decide which property a picker is servicing. It has no
Property Master, no PostGIS service zones and no dwell logic, and adding any
of those here would duplicate the authoritative backend on the wrong machine.

But a second RFID tap from a bound collector means "the waste *here* is not
segregated", and that sentence needs a subject. So WASTRAQ -- which does know
-- pushes the open episode down::

    POST   /episodes/active     WASTRAQ: track 35 has episode E-12 open
    DELETE /episodes/{id}       WASTRAQ: that collection is finished
    GET    /episodes            what this edge node currently believes

What crosses is an opaque ``episode_id``, a ``track_id`` and a confidence
word. No property id, no house number, no service zone -- and any that arrive
anyway are dropped before storage.

The mirror is advisory. When it shows no open episode the tap flags nothing,
and the outcome is still published to WASTRAQ, which holds the real episode
table and has the final word.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from location.base import parse_timestamp
from schemas import EpisodeIn
from services import episode_registry, worker_registry
from vision.types import AssociationStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["episodes"])


@router.post("/episodes/active")
def open_episode(episode: EpisodeIn):
    """WASTRAQ declares a collection episode open on a camera track.

    Always HTTP 200. A rejected push is a state disagreement worth recording
    on both sides, not a transport error for WASTRAQ to retry.
    """

    session_id = worker_registry.session_id

    if episode.session_id and episode.session_id != session_id:
        # A track id from a previous capture run names a different human.
        logger.info(
            "Episode %s rejected: session %s is not the live session %s",
            episode.episode_id,
            episode.session_id,
            session_id,
        )
        return {
            "accepted": False,
            "reason": "STALE_SESSION",
            "session_id": session_id,
            "episode_id": episode.episode_id,
        }

    binding = worker_registry.binding_for_track(episode.track_id)
    if (
        episode.collector_id
        and binding is not None
        and binding.collector_id != episode.collector_id
    ):
        logger.info(
            "Episode %s rejected: track %s is locked to %s, not %s",
            episode.episode_id,
            episode.track_id,
            binding.collector_id,
            episode.collector_id,
        )
        return {
            "accepted": False,
            "reason": "COLLECTOR_TRACK_MISMATCH",
            "session_id": session_id,
            "episode_id": episode.episode_id,
            "bound_collector_id": binding.collector_id,
        }

    try:
        opened_at = (
            parse_timestamp(episode.opened_at) if episode.opened_at else None
        )
    except Exception:
        opened_at = None

    stored = episode_registry.open(
        episode_id=episode.episode_id,
        track_id=episode.track_id,
        session_id=session_id,
        association_status=episode.association_status,
        collector_id=episode.collector_id or (binding.collector_id if binding else None),
        opened_at=opened_at,
    )

    return {
        "accepted": True,
        # Recorded either way: a non-actionable episode is still worth
        # mirroring so a re-tap can say *why* it did nothing.
        "actionable": stored.is_actionable,
        "session_id": session_id,
        "episode": stored.to_dict(),
        "locked_track": binding.track_id if binding else None,
    }


@router.delete("/episodes/{episode_id}")
def close_episode(episode_id: str):
    """WASTRAQ declares a collection episode finished."""

    closed = episode_registry.close(episode_id=episode_id)
    return {
        "closed": closed is not None,
        "episode": closed.to_dict() if closed else None,
    }


@router.get("/episodes")
def list_episodes():
    """What this edge node believes is open right now.

    Diagnostic. If a re-tap reports NO_ACTIVE_EPISODE during the demo, this
    is the endpoint that says whether WASTRAQ ever pushed the episode.
    """

    episode_registry.expire()
    episodes = episode_registry.to_list()
    return {
        "session_id": worker_registry.session_id,
        "count": len(episodes),
        "max_age_s": episode_registry.max_age_s,
        "actionable_statuses": sorted(AssociationStatus.ACTIONABLE),
        "source": "WASTRAQ",
        "episodes": episodes,
    }
