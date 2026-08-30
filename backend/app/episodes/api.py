"""Read and control surface for the episode engine.

    GET    /episodes                 recent episodes (the dashboard feed)
    GET    /episodes/status          live engine state: bindings, dwell, mirror
    GET    /episodes/triggers        second-tap signals and their verdicts
    GET    /episodes/{episode_id}    one episode
    POST   /episodes/reset           clear transient state for a fresh run

Deliberately no endpoint that sets a property on an episode by hand. The
association is made by the PostGIS ladder or it is not made; an override
route would be the exact hole this architecture exists to close. Correcting
a wrong outcome is a review action on the collection event, where it is
recorded as a human decision.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .engine import get_engine
from .mirror import get_mirror
from .store import EpisodeStore

router = APIRouter(prefix="/episodes", tags=["episodes"])
store = EpisodeStore()


class ResetRequest(BaseModel):
    """What a reset is allowed to touch.

    Nothing here can delete a property, a service zone, an entrance, a
    frontage or a past collection event. Those are the surveyed and recorded
    facts; a demo restart is not a reason to lose them.
    """

    abort_active_episodes: bool = Field(
        True, description="Mark live episodes ABORTED (they write no collection event).")
    clear_edge_tracks: bool = Field(
        False, description="Also delete geovision_track_updates rows (perception cache).")


@router.get("")
def list_episodes(
    limit: int = Query(50, ge=1, le=500),
    property_id: str | None = Query(None),
):
    return {"episodes": store.recent_episodes(limit=limit, property_id=property_id)}


@router.get("/status")
def engine_status():
    """Everything you need to tell "not bound yet" from "bound but not in a
    zone" from "in a zone but not long enough" - the three states that look
    identical from a dashboard that only shows results."""
    engine = get_engine()
    snapshot = engine.snapshot()
    return {
        "engine": snapshot,
        "mirror": get_mirror().status(),
        "authority": {
            "property_decided_by": "WASTRAQ PostGIS service-zone association",
            "geovision_supplies": ["identity (RFID)", "camera-frame position",
                                   "non-segregation signal", "evidence clips"],
            "geovision_never_supplies": ["property_id", "service_zone_id",
                                         "segregation_status"],
        },
        "db_active_episodes": store.active_episodes(),
    }


@router.get("/triggers")
def list_triggers(limit: int = Query(25, ge=1, le=200)):
    return {"triggers": store.recent_triggers(limit=limit)}


@router.post("/reset")
def reset(body: ResetRequest | None = None):
    """Clear transient state between demo runs.

    Bindings, track ownership, dwell candidates, live episodes and the
    Windows mirrors. Mapped properties and their geometry are untouched, and
    the response says so with a count you can check.
    """
    body = body or ResetRequest()
    engine = get_engine()
    result = engine.reset(abort_db_episodes=body.abort_active_episodes)
    if body.clear_edge_tracks:
        try:
            result["edge_tracks"] = store.clear_edge_state()
        except Exception as exc:  # noqa: BLE001
            result["edge_tracks_error"] = repr(exc)
    result["mirror"] = get_mirror().status()
    return result


@router.get("/{episode_id}")
def get_episode(episode_id: str):
    row = store.get_episode(episode_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Unknown episode {episode_id}")
    row["triggers"] = [
        t for t in store.recent_triggers(limit=200)
        if t.get("claimed_episode_id") == episode_id
        or t.get("applied_episode_id") == episode_id
    ]
    return row
