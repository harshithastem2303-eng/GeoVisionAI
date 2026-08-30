"""The collection-episode engine.

WASTRAQ's answer to "which house was just serviced, by whom, and was the
waste segregated". It is the only place that answer is produced.

Inputs, in the order they matter
--------------------------------
``WORKER_TRACK_BOUND``   which camera track IS the collector, for this
                         capture session. Nothing else creates episodes:
                         an unbound track is a passer-by.
``TRACK_UPDATE``         where that track is, in CAMERA metres.
``NON_SEGREGATION_TRIGGER``  a second RFID tap. A signal, never a verdict.
``EVIDENCE_READY``       a clip exists on the edge; attach it to the
                         episode it actually belongs to, or to nothing.

The ladder, per observation of the bound track
----------------------------------------------
1. camera metres -> WGS84, through the surveyed camera origin and heading
   (``transform.py``). No GPS is involved; the phone fix is never used.
2. WGS84 -> property, through the PostGIS service-zone ladder
   (``app.gis.lookup_property``). It may refuse, and a refusal is honoured.
3. A single property held for ``dwell_s`` becomes an EPISODE. Below the
   dwell it is a candidate and nothing is written: walking past a gate is
   not a collection.
4. The episode is mirrored to GeoVision so a second tap has something to
   point at. Advisory - see ``mirror.py``.
5. Leaving the zone for ``leave_grace_s`` closes the episode, which writes
   the collection event.

The two defaults that ARE the product rule
------------------------------------------
* An episode that closes without an accepted trigger is **SEGREGATED**. The
  collector acts only on the exception.
* An ambiguous position creates **nothing**. Not a guess, not a "nearest",
  not a provisional row someone will forget to check. The whole system
  exists to avoid flagging the wrong house.

Threading
---------
Called from FastAPI request threads (ingestion) and from one sweeper thread
(leave-grace expiry when the track simply vanishes). All state changes hold
``self._lock``; the lock is never held across an HTTP call to Windows, only
across the queue append.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .transform import CameraOrigin, camera_to_wgs84

# Edge statuses that are the edge saying "I could not resolve this tap".
# They may VETO an application; they can never cause one.
EDGE_UNRESOLVED_STATUSES = frozenset({
    "NO_ACTIVE_EPISODE",
    "EPISODE_NOT_ACTIONABLE",
    "UNKNOWN_RFID",
    "NO_BINDING",
    "NOT_BOUND",
    "AMBIGUOUS",
    "FAILED",
    "ERROR",
})


@dataclass
class EpisodeConfig:
    enabled: bool = True
    dwell_s: float = 3.0
    leave_grace_s: float = 4.0
    min_assoc_interval_s: float = 0.4
    max_duration_s: float = 180.0
    review_confidence: float = 0.85
    binding_ttl_s: float = 900.0
    trigger_late_grace_s: float = 30.0
    evidence_link_window_s: float = 120.0
    require_depth_valid: bool = True
    camera: CameraOrigin = CameraOrigin(0.0, 0.0, 0.0)
    camera_configured: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> "EpisodeConfig":
        return cls(
            enabled=settings.EPISODE_ENGINE_ENABLED,
            dwell_s=settings.EPISODE_DWELL_S,
            leave_grace_s=settings.EPISODE_LEAVE_GRACE_S,
            min_assoc_interval_s=settings.EPISODE_MIN_ASSOC_INTERVAL_S,
            max_duration_s=settings.EPISODE_MAX_DURATION_S,
            review_confidence=settings.EPISODE_REVIEW_CONFIDENCE,
            binding_ttl_s=settings.EPISODE_BINDING_TTL_S,
            trigger_late_grace_s=settings.EPISODE_TRIGGER_LATE_GRACE_S,
            evidence_link_window_s=settings.EPISODE_EVIDENCE_LINK_WINDOW_S,
            require_depth_valid=settings.EPISODE_REQUIRE_DEPTH_VALID,
            camera=CameraOrigin(
                latitude=settings.CAMERA_ORIGIN_LAT,
                longitude=settings.CAMERA_ORIGIN_LON,
                heading_deg=settings.CAMERA_HEADING_DEG,
                offset_right_m=settings.CAMERA_OFFSET_RIGHT_M,
                offset_forward_m=settings.CAMERA_OFFSET_FORWARD_M,
            ),
            camera_configured=bool(settings.CAMERA_ORIGIN_LAT
                                   and settings.CAMERA_ORIGIN_LON),
        )


@dataclass
class Binding:
    collector_id: str
    source_id: str
    session_id: str
    track_id: int
    rfid_uid: str | None
    bound_at: datetime
    event_id: str


@dataclass
class Candidate:
    property_id: str
    first_seen: datetime
    last_seen: datetime
    confidence: float
    method: str | None
    observations: int = 1


@dataclass
class Active:
    episode_id: str
    property_id: str
    collector_id: str
    source_id: str
    session_id: str
    track_id: int
    started_at: datetime
    last_seen: datetime
    last_inside: datetime
    last_inside_wall: float
    observations: int = 0
    segregation_status: str = "SEGREGATED"
    association_status: str = "AUTO_ASSOCIATED"
    association_confidence: float | None = None
    picker_id: str | None = None
    pending_clip_event_ids: list[str] = field(default_factory=list)


class EpisodeEngine:
    """Bindings, dwell, episodes, triggers, evidence. One per process."""

    def __init__(self, config: EpisodeConfig, store: Any,
                 associator: Callable[[float, float], dict],
                 mirror: Any) -> None:
        self.config = config
        self.store = store
        self.associate = associator
        self.mirror = mirror
        # The mirror runs on its own thread and only learns the outcome after
        # the fact. Wiring the callback HERE - rather than where the mirror is
        # built - means the status is written through THIS engine's store, so
        # a test rig and the live process record it the same way. Without it
        # every episode stays "PENDING" forever and a dead laptop looks
        # exactly like a healthy one.
        if getattr(mirror, "on_result", None) is None:
            mirror.on_result = self._on_mirror_result

        self._lock = threading.RLock()
        self._bindings: dict[str, Binding] = {}          # collector_id -> binding
        self._track_owner: dict[tuple, str] = {}          # track key -> collector_id
        self._candidates: dict[str, Candidate] = {}
        self._active: dict[str, Active] = {}
        self._last_assoc: dict[tuple, float] = {}
        self.stats: dict[str, int] = {
            "bindings": 0, "observations": 0, "associations": 0,
            "episodes_opened": 0, "episodes_closed": 0,
            "ambiguous": 0, "unbound_skipped": 0,
            "triggers_applied": 0, "triggers_preserved": 0,
            "clips_linked": 0,
        }
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()

    def _on_mirror_result(self, episode_id: str, action: str, ok: bool,
                          error: str | None) -> None:
        """Mirror worker thread -> collection_episodes.mirror_status."""
        if action == "PUBLISH":
            status = "MIRRORED" if ok else "MIRROR_FAILED"
        else:
            status = "REMOVED" if ok else "REMOVE_FAILED"
        try:
            self.store.set_mirror_status(episode_id, status, error)
        except Exception:  # noqa: BLE001
            # Bookkeeping about a laptop in the other room. It must never
            # take down the worker thread or the episode.
            pass

    # -- keys ---------------------------------------------------------------
    @staticmethod
    def _key(source_id: str, session_id: str | None, track_id: int) -> tuple:
        return (source_id, session_id or "", int(track_id))

    # =======================================================================
    # WORKER_TRACK_BOUND
    # =======================================================================
    def on_worker_bound(self, event: Any) -> dict[str, Any]:
        """A collector now owns a camera track. Everything else follows this."""
        if not self.config.enabled:
            return {"handled": False, "reason": "ENGINE_DISABLED"}

        collector = event.collector_id
        key = self._key(event.source_id, event.session_id, event.track_id)

        with self._lock:
            previous = self._bindings.get(collector)
            if previous is not None:
                # Rebinding to a different track ends whatever was in flight
                # on the old one. A collector cannot be servicing two houses.
                old_key = self._key(previous.source_id, previous.session_id,
                                    previous.track_id)
                if old_key != key:
                    self._track_owner.pop(old_key, None)
                    self._candidates.pop(collector, None)
                    active = self._active.get(collector)
                    if active is not None:
                        self._close(collector, active, event.timestamp,
                                    reason="REBOUND")

            # A track can belong to exactly one collector.
            stale_owner = self._track_owner.get(key)
            if stale_owner and stale_owner != collector:
                self._bindings.pop(stale_owner, None)
                self._candidates.pop(stale_owner, None)

            self._bindings[collector] = Binding(
                collector_id=collector,
                source_id=event.source_id,
                session_id=event.session_id or "",
                track_id=int(event.track_id),
                rfid_uid=getattr(event, "rfid_uid", None),
                bound_at=event.timestamp,
                event_id=event.event_id,
            )
            self._track_owner[key] = collector
            self.stats["bindings"] += 1

        return {"handled": True, "collector_id": collector,
                "track_id": int(event.track_id)}

    # =======================================================================
    # TRACK_UPDATE
    # =======================================================================
    def on_track_update(self, event: Any) -> dict[str, Any]:
        """One observation of one track. Cheap unless the track is bound."""
        if not self.config.enabled:
            return {"handled": False, "reason": "ENGINE_DISABLED"}

        key = self._key(event.source_id, event.session_id, event.track_id)
        with self._lock:
            collector = self._track_owner.get(key)
            if collector is None:
                # The overwhelmingly common case, and the cheapest: a person
                # in frame who never tapped a card creates nothing.
                self.stats["unbound_skipped"] += 1
                return {"handled": False, "reason": "UNBOUND_TRACK"}

            binding = self._bindings.get(collector)
            if binding is None:
                self._track_owner.pop(key, None)
                return {"handled": False, "reason": "STALE_TRACK_OWNER"}

            age = (event.timestamp - binding.bound_at).total_seconds()
            if age > self.config.binding_ttl_s:
                self._release_binding(collector, event.timestamp,
                                      reason="BINDING_EXPIRED")
                return {"handled": False, "reason": "BINDING_EXPIRED"}

            self.stats["observations"] += 1

            active = self._active.get(collector)
            if active is not None:
                active.last_seen = event.timestamp

            # Throttle the PostGIS work. The edge publishes ~5 Hz per track;
            # running the association ladder on every frame would put a
            # spatial query on a sensor stream's critical path for no gain -
            # a person does not cross a service zone in 200 ms.
            now_wall = time.monotonic()
            last = self._last_assoc.get(key, 0.0)
            if now_wall - last < self.config.min_assoc_interval_s:
                return {"handled": True, "reason": "THROTTLED",
                        "collector_id": collector}
            self._last_assoc[key] = now_wall

            position = self._position(event)

        if position is None:
            # No usable depth. Not an observation of absence: it neither
            # extends nor ends anything. If depth never returns, leave grace
            # closes the episode on its own.
            return {"handled": True, "reason": "NO_VALID_POSITION",
                    "collector_id": collector}

        lat, lon = position
        # Outside the lock on purpose: this is the PostGIS round trip.
        result = self.associate(lat, lon)
        self.stats["associations"] += 1

        with self._lock:
            return self._observe(collector, event, lat, lon, result)

    def _position(self, event: Any) -> tuple[float, float] | None:
        if not self.config.camera_configured:
            return None
        right = getattr(event, "relative_x_m", None)
        forward = getattr(event, "relative_forward_m", None)
        if right is None or forward is None:
            # Fall back to the raw camera frame: x right, z forward. Same
            # axes, one fewer assumption than trusting a missing field.
            right = getattr(event, "camera_x_m", None)
            forward = getattr(event, "camera_z_m", None)
        if right is None or forward is None:
            return None
        if self.config.require_depth_valid and not getattr(event, "depth_valid", False):
            # A depth reading the edge itself does not vouch for must not
            # decide which house was served.
            return None
        return camera_to_wgs84(self.config.camera, float(right), float(forward))

    # -- the state machine ---------------------------------------------------
    def _observe(self, collector: str, event: Any, lat: float, lon: float,
                 result: dict) -> dict[str, Any]:
        ts = event.timestamp
        decision = result.get("decision")
        property_id = result.get("property_id")
        active = self._active.get(collector)

        if decision != "AUTO_ASSOCIATED" or not property_id:
            if decision == "AMBIGUOUS":
                self.stats["ambiguous"] += 1
            # Ambiguity clears a candidate outright. A half-built dwell on a
            # property we are no longer sure about is worse than none.
            self._candidates.pop(collector, None)
            if active is not None:
                self._maybe_close_on_leave(collector, active, ts)
            return {"handled": True, "decision": decision,
                    "collector_id": collector,
                    "position": {"latitude": lat, "longitude": lon},
                    "episode_id": active.episode_id if active else None,
                    "reason": result.get("reason")}

        confidence = float(result.get("confidence") or 0.0)

        if active is not None:
            if active.property_id == property_id:
                active.observations += 1
                active.last_seen = ts
                active.last_inside = ts
                active.last_inside_wall = time.monotonic()
                self.store.touch_episode(active.episode_id, ts, active.observations)
                return {"handled": True, "episode_id": active.episode_id,
                        "property_id": property_id, "state": "ACTIVE",
                        "collector_id": collector}
            # A different property, unambiguously. The collector moved on;
            # close this one at the last moment we were sure of it.
            self._close(collector, active, active.last_inside, reason="MOVED_ON")
            active = None

        candidate = self._candidates.get(collector)
        if candidate is None or candidate.property_id != property_id:
            self._candidates[collector] = Candidate(
                property_id=property_id, first_seen=ts, last_seen=ts,
                confidence=confidence, method=result.get("method"))
            return {"handled": True, "state": "CANDIDATE",
                    "property_id": property_id, "collector_id": collector,
                    "dwell_s": 0.0}

        candidate.last_seen = ts
        candidate.observations += 1
        candidate.confidence = max(candidate.confidence, confidence)
        dwell = (candidate.last_seen - candidate.first_seen).total_seconds()
        if dwell < self.config.dwell_s:
            return {"handled": True, "state": "CANDIDATE",
                    "property_id": property_id, "collector_id": collector,
                    "dwell_s": round(dwell, 2)}

        return self._open(collector, candidate, event)

    def _open(self, collector: str, candidate: Candidate,
              event: Any) -> dict[str, Any]:
        """Promote a sustained candidate into a real, mirrored episode."""
        binding = self._bindings.get(collector)
        session_id = binding.session_id if binding else (event.session_id or "")
        rfid_uid = binding.rfid_uid if binding else None

        picker_id = None
        try:
            picker_id = self.store.picker_for(collector, rfid_uid)
        except Exception:  # noqa: BLE001
            picker_id = None

        association_status = ("AUTO_ASSOCIATED"
                              if candidate.confidence >= self.config.review_confidence
                              else "REVIEW")
        episode_id = self.store.next_episode_id()
        row = self.store.create_episode(
            episode_id=episode_id,
            property_id=candidate.property_id,
            collector_id=collector,
            picker_id=picker_id,
            source_id=event.source_id,
            session_id=session_id,
            track_id=int(event.track_id),
            association_status=association_status,
            association_confidence=candidate.confidence,
            association_method=candidate.method,
            started_at=candidate.first_seen,
            last_seen_at=candidate.last_seen,
            observations=candidate.observations,
        )
        if row is None:
            # The partial unique index refused it: this collector already has
            # a live episode the engine does not know about (a restart). Do
            # not force it; the sweeper will not have it either, so leave the
            # candidate standing and try again on the next observation.
            return {"handled": False, "reason": "ACTIVE_EPISODE_EXISTS",
                    "collector_id": collector}

        active = Active(
            episode_id=episode_id,
            property_id=candidate.property_id,
            collector_id=collector,
            source_id=event.source_id,
            session_id=session_id,
            track_id=int(event.track_id),
            started_at=candidate.first_seen,
            last_seen=candidate.last_seen,
            last_inside=candidate.last_seen,
            last_inside_wall=time.monotonic(),
            observations=candidate.observations,
            association_status=association_status,
            association_confidence=candidate.confidence,
            picker_id=picker_id,
        )
        self._active[collector] = active
        self._candidates.pop(collector, None)
        self.stats["episodes_opened"] += 1

        status = self.mirror.publish_active({
            "episode_id": episode_id,
            "track_id": active.track_id,
            "association_status": association_status,
            "collector_id": collector,
            "session_id": session_id or None,
        })
        if status == "DISABLED":
            # Settled immediately: there is no edge to answer. Everything
            # else stays PENDING until the worker thread reports back.
            try:
                self.store.set_mirror_status(episode_id, status)
            except Exception:  # noqa: BLE001
                pass

        return {"handled": True, "state": "EPISODE_OPENED",
                "episode_id": episode_id, "property_id": candidate.property_id,
                "collector_id": collector, "picker_id": picker_id,
                "association_status": association_status,
                "association_confidence": candidate.confidence,
                "mirror_status": status,
                "dwell_s": round(
                    (candidate.last_seen - candidate.first_seen).total_seconds(), 2)}

    def _maybe_close_on_leave(self, collector: str, active: Active,
                              ts: datetime) -> None:
        gap = (ts - active.last_inside).total_seconds()
        if gap >= self.config.leave_grace_s:
            self._close(collector, active, active.last_inside, reason="LEFT_ZONE")

    def _close(self, collector: str, active: Active, ended_at: datetime,
               *, reason: str, state: str = "CLOSED") -> dict[str, Any]:
        """End an episode and write the collection event it earned."""
        self._active.pop(collector, None)
        dwell = max((ended_at - active.started_at).total_seconds(), 0.0)

        row = None
        try:
            row = self.store.close_episode(
                active.episode_id, ended_at=ended_at, dwell_s=dwell,
                observations=active.observations, state=state)
        except Exception:  # noqa: BLE001
            row = None

        result: dict[str, Any] = {
            "episode_id": active.episode_id,
            "property_id": active.property_id,
            "collector_id": collector,
            "reason": reason,
            "state": state,
            "dwell_s": round(dwell, 2),
        }

        if row is not None and state == "CLOSED":
            # SEGREGATED is the default and it is a decision, not a fallback:
            # nothing raised an exception, so the waste was segregated.
            segregation = row.get("segregation_status", "SEGREGATED")
            review = ("NEEDS_REVIEW"
                      if segregation == "NOT_SEGREGATED"
                      or row.get("association_status") == "REVIEW"
                      else "AUTO_CONFIRMED")
            try:
                event_row = self.store.create_collection_event(
                    episode=row, collection_time=ended_at, review_status=review)
            except Exception as exc:  # noqa: BLE001
                event_row = None
                result["collection_event_error"] = repr(exc)

            if event_row:
                result["collection_event_id"] = event_row["event_id"]
                result["segregation_status"] = segregation
                result["review_status"] = review
                if segregation == "NOT_SEGREGATED":
                    try:
                        self.store.add_evidence(
                            event_row["event_id"], "NON_SEGREGATION_PROOF",
                            None, ended_at)
                    except Exception:  # noqa: BLE001
                        pass
                self._link_pending_clips(active.episode_id,
                                         event_row["event_id"], ended_at)

        self.stats["episodes_closed"] += 1
        # Only DISABLED is written here. Every other outcome is settled by the
        # worker thread through _on_mirror_result; writing "PENDING" over it
        # would race the answer we are waiting for.
        status = self.mirror.remove(active.episode_id)
        if status == "DISABLED":
            try:
                self.store.set_mirror_status(active.episode_id, status)
            except Exception:  # noqa: BLE001
                pass
        result["mirror_remove"] = status
        return result

    def _link_pending_clips(self, episode_id: str, event_id: str,
                            when: datetime) -> None:
        try:
            clips = self.store.pending_clips_for_episode(episode_id)
        except Exception:  # noqa: BLE001
            return
        for clip in clips:
            try:
                self.store.attach_clip(
                    event_id, episode_id, clip_event_id=clip["event_id"],
                    file_path=clip["file_path"],
                    captured_at=clip.get("event_time") or when)
                self.stats["clips_linked"] += 1
            except Exception:  # noqa: BLE001
                continue

    def _release_binding(self, collector: str, ts: datetime, *,
                         reason: str) -> None:
        binding = self._bindings.pop(collector, None)
        if binding is not None:
            self._track_owner.pop(
                self._key(binding.source_id, binding.session_id,
                          binding.track_id), None)
        self._candidates.pop(collector, None)
        active = self._active.get(collector)
        if active is not None:
            self._close(collector, active, active.last_inside, reason=reason)

    # =======================================================================
    # NON_SEGREGATION_TRIGGER  -  the sixth event
    # =======================================================================
    def on_non_segregation_trigger(self, event: Any) -> dict[str, Any]:
        """Apply a second-tap signal to WASTRAQ's OWN episode, or to nothing.

        The trigger carries no property and is not permitted to. Which house
        gets flagged is read off the episode WASTRAQ created from its own
        service-zone association. Every path that cannot land the signal
        preserves it for review instead of applying it somewhere plausible.
        """
        trigger_id = event.trigger_id
        claimed = getattr(event, "episode_id", None)

        claimed_fresh = self.store.claim_trigger(
            trigger_id=trigger_id,
            event_id=event.event_id,
            source_id=event.source_id,
            session_id=event.session_id,
            event_time=event.timestamp,
            claimed_episode_id=claimed,
            collector_id=getattr(event, "collector_id", None),
            rfid_uid=getattr(event, "rfid_uid", None),
            track_id=getattr(event, "track_id", None),
            trigger_status=getattr(event, "trigger_status", None),
            edge_duplicate=bool(getattr(event, "duplicate", False)),
            rfid_event_id=getattr(event, "rfid_event_id", None),
        )
        if not claimed_fresh:
            # Semantic idempotency. The same decision re-announced under a
            # fresh envelope must not mark a second house.
            existing = self.store.get_trigger(trigger_id) or {}
            return {"handled": True, "resolution": "DUPLICATE",
                    "trigger_id": trigger_id,
                    "episode_id": existing.get("applied_episode_id"),
                    "applied": False}

        if not self.config.enabled:
            return self._resolve(trigger_id, "ENGINE_DISABLED",
                                 "Episode engine is switched off.", review=True)

        status = (getattr(event, "trigger_status", None) or "").upper()
        if status in EDGE_UNRESOLVED_STATUSES:
            # The edge is telling us it had nothing to point at. Believe the
            # veto; never substitute a guess of our own.
            return self._resolve(
                trigger_id, "EDGE_UNRESOLVED",
                f"GeoVision reported trigger_status={status}; no property changed.",
                review=True)

        if not claimed:
            return self._resolve(trigger_id, "UNKNOWN_EPISODE",
                                 "Trigger carried no episode_id.", review=True)

        episode = self.store.get_episode(claimed)
        if episode is None:
            return self._resolve(
                trigger_id, "UNKNOWN_EPISODE",
                f"No WASTRAQ episode {claimed}. Signal preserved; no property changed.",
                review=True)

        mismatch = self._identity_mismatch(event, episode)
        if mismatch:
            return self._resolve(trigger_id, "IDENTITY_MISMATCH", mismatch,
                                 review=True)

        if episode.get("state") == "ACTIVE":
            pass
        elif episode.get("state") == "CLOSED":
            ended = episode.get("ended_at")
            late = ((event.timestamp - ended).total_seconds()
                    if isinstance(ended, datetime) else 1e9)
            if late > self.config.trigger_late_grace_s:
                return self._resolve(
                    trigger_id, "EPISODE_NOT_ACTIONABLE",
                    f"Episode {claimed} closed {late:.0f}s before this trigger "
                    f"(grace {self.config.trigger_late_grace_s:g}s).",
                    review=True, episode_id=claimed)
            return self._apply_to_closed(trigger_id, episode, event)
        else:
            return self._resolve(
                trigger_id, "EPISODE_NOT_ACTIONABLE",
                f"Episode {claimed} is {episode.get('state')}.",
                review=True, episode_id=claimed)

        updated = self.store.mark_non_segregated(claimed, trigger_id,
                                                 event.timestamp)
        if updated is None:
            return self._resolve(
                trigger_id, "EPISODE_NOT_ACTIONABLE",
                f"Episode {claimed} was already flagged or no longer active.",
                review=True, episode_id=claimed)

        with self._lock:
            for active in self._active.values():
                if active.episode_id == claimed:
                    active.segregation_status = "NOT_SEGREGATED"
                    break

        self.stats["triggers_applied"] += 1
        return self._resolve(
            trigger_id, "APPLIED",
            f"Episode {claimed} -> NOT_SEGREGATED (property {updated['property_id']}).",
            episode_id=claimed, applied=True,
            property_id=updated["property_id"])

    def _apply_to_closed(self, trigger_id: str, episode: dict,
                         event: Any) -> dict[str, Any]:
        """A trigger that lost a race with the collector walking away.

        Within the late grace it still belongs to that episode, so the
        episode AND the collection event it already produced are corrected -
        in one statement each, on the episode WASTRAQ itself chose.
        """
        episode_id = episode["episode_id"]
        updated = None
        try:
            updated = self.store.mark_closed_non_segregated(
                episode_id, trigger_id, event.timestamp)
        except Exception as exc:  # noqa: BLE001
            return self._resolve(
                trigger_id, "ERROR", f"Correcting closed episode failed: {exc!r}",
                review=True, episode_id=episode_id)

        if updated is None:
            return self._resolve(
                trigger_id, "EPISODE_NOT_ACTIONABLE",
                f"Episode {episode_id} is closed and was already resolved.",
                review=True, episode_id=episode_id)

        self.stats["triggers_applied"] += 1
        return self._resolve(
            trigger_id, "APPLIED",
            f"Closed episode {episode_id} corrected to NOT_SEGREGATED.",
            episode_id=episode_id, applied=True,
            property_id=updated.get("property_id"))

    def _identity_mismatch(self, event: Any, episode: dict) -> str | None:
        """Refuse a trigger whose identity does not match our own episode.

        Checked only where BOTH sides state a value: a field the edge omitted
        is missing information, not a contradiction.
        """
        collector = getattr(event, "collector_id", None)
        if collector and episode.get("collector_id") and \
                collector != episode["collector_id"]:
            return (f"collector {collector!r} does not own episode "
                    f"{episode['episode_id']} ({episode['collector_id']!r}).")

        track = getattr(event, "track_id", None)
        if track is not None and episode.get("track_id") is not None and \
                int(track) != int(episode["track_id"]):
            return (f"track {track} is not the episode's track "
                    f"{episode['track_id']}.")

        session = getattr(event, "session_id", None)
        ep_session = episode.get("session_id")
        if session and ep_session and session != ep_session:
            return (f"session {session!r} is not the episode's session "
                    f"{ep_session!r}.")

        if event.source_id and episode.get("source_id") and \
                event.source_id != episode["source_id"]:
            return (f"source {event.source_id!r} did not create episode "
                    f"{episode['episode_id']}.")
        return None

    def _resolve(self, trigger_id: str, resolution: str, detail: str, *,
                 review: bool = False, episode_id: str | None = None,
                 applied: bool = False,
                 property_id: str | None = None) -> dict[str, Any]:
        try:
            self.store.resolve_trigger(
                trigger_id, resolution=resolution, detail=detail,
                applied_episode_id=episode_id if applied else None,
                needs_review=review)
        except Exception:  # noqa: BLE001
            pass
        if not applied:
            self.stats["triggers_preserved"] += 1
        return {"handled": True, "trigger_id": trigger_id,
                "resolution": resolution, "detail": detail,
                "episode_id": episode_id, "applied": applied,
                "property_id": property_id, "needs_review": review}

    # =======================================================================
    # EVIDENCE_READY
    # =======================================================================
    def on_evidence_ready(self, event: Any) -> dict[str, Any]:
        """Attach a clip to the episode it belongs to - or to nothing."""
        if not self.config.enabled:
            return {"handled": False, "reason": "ENGINE_DISABLED"}

        episode_id: str | None = None

        rfid_event_id = getattr(event, "rfid_event_id", None)
        if rfid_event_id:
            # Strongest key: the tap that caused the clip is the tap that
            # caused the trigger, and the trigger already resolved to an
            # episode WASTRAQ chose.
            try:
                episode_id = self.store.episode_for_rfid_event(rfid_event_id)
            except Exception:  # noqa: BLE001
                episode_id = None

        if episode_id is None and getattr(event, "track_id", None) is not None:
            with self._lock:
                for active in self._active.values():
                    if (active.source_id == event.source_id
                            and active.session_id == (event.session_id or "")
                            and active.track_id == int(event.track_id)
                            and active.segregation_status == "NOT_SEGREGATED"):
                        episode_id = active.episode_id
                        break

        if episode_id is None:
            try:
                row = self.store.episode_for_clip(
                    source_id=event.source_id, session_id=event.session_id,
                    track_id=getattr(event, "track_id", None),
                    clip_time=event.timestamp,
                    window_s=self.config.evidence_link_window_s)
            except Exception:  # noqa: BLE001
                row = None
            episode_id = row["episode_id"] if row else None

        # Resolve the clip by IDENTITY, not by the envelope we are holding.
        #
        # `_insert_clip` keeps the first row for a given (source_id, clip_id)
        # and drops a re-announcement, so on a second EVIDENCE_READY the
        # envelope's own event_id has no row at all. Using it here would tag
        # zero rows and then insert a SECOND evidence record for one clip -
        # the operator would see the same fifteen seconds of footage twice
        # and have no way to tell which was real. The canonical row is the
        # one the first announcement created.
        clip_event_id = event.event_id
        try:
            canonical = self.store.clip_by_identity(event.source_id, event.clip_id)
            if canonical and canonical.get("event_id"):
                clip_event_id = canonical["event_id"]
        except Exception:  # noqa: BLE001
            canonical = None

        # Pull the bytes across regardless of whether an episode claims the
        # clip. Evidence WASTRAQ holds but has not yet attributed is worth
        # far more than evidence it attributed but does not hold, and this
        # runs on its own thread so a slow or absent edge cannot reach the
        # ingestion path.
        try:
            from ..evidence_media import fetch_clip_in_background
            fetch_clip_in_background(clip_event_id)
        except Exception:  # noqa: BLE001
            pass

        if episode_id is None:
            return {"handled": True, "linked": False,
                    "clip_event_id": clip_event_id,
                    "reason": "NO_MATCHING_EPISODE"}

        try:
            episode = self.store.get_episode(episode_id) or {}
            self.store.tag_clip_episode(clip_event_id, episode_id)
            collection_event_id = episode.get("collection_event_id")
            if collection_event_id:
                evidence_id = self.store.attach_clip(
                    collection_event_id, episode_id,
                    clip_event_id=clip_event_id,
                    file_path=event.file_path, captured_at=event.timestamp)
                self.stats["clips_linked"] += 1
                return {"handled": True, "linked": True,
                        "episode_id": episode_id,
                        "clip_event_id": clip_event_id,
                        "collection_event_id": collection_event_id,
                        "evidence_id": evidence_id}
        except Exception as exc:  # noqa: BLE001
            return {"handled": True, "linked": False,
                    "clip_event_id": clip_event_id, "error": repr(exc)}

        # The episode is still open: the clip is tagged and will be linked
        # the moment the collection event exists.
        return {"handled": True, "linked": False, "deferred": True,
                "clip_event_id": clip_event_id,
                "episode_id": episode_id}

    # =======================================================================
    # housekeeping
    # =======================================================================
    def sweep(self) -> list[dict[str, Any]]:
        """Close episodes whose track simply stopped reporting."""
        closed: list[dict[str, Any]] = []
        now_wall = time.monotonic()
        with self._lock:
            for collector, active in list(self._active.items()):
                idle = now_wall - active.last_inside_wall
                duration = (active.last_seen - active.started_at).total_seconds()
                if idle >= self.config.leave_grace_s:
                    closed.append(self._close(collector, active,
                                              active.last_inside,
                                              reason="LEAVE_GRACE_EXPIRED"))
                elif duration >= self.config.max_duration_s:
                    closed.append(self._close(collector, active,
                                              active.last_seen,
                                              reason="MAX_DURATION"))
        return closed

    def start_sweeper(self, interval_s: float = 1.0) -> None:
        if not self.config.enabled or self._sweeper is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001
                    continue

        self._sweeper = threading.Thread(target=_loop, name="episode-sweeper",
                                         daemon=True)
        self._sweeper.start()

    def stop_sweeper(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=timeout)
            self._sweeper = None

    def reset(self, *, abort_db_episodes: bool = True) -> dict[str, Any]:
        """Clear transient state. Mapped properties are never touched.

        Bindings, track ownership, dwell candidates, live episodes and the
        Windows mirrors go. Properties, service zones, entrances, frontages,
        past collection events and stored evidence stay exactly as they are.
        """
        with self._lock:
            episode_ids = [a.episode_id for a in self._active.values()]
            self._bindings.clear()
            self._track_owner.clear()
            self._candidates.clear()
            self._active.clear()
            self._last_assoc.clear()

        aborted: list[str] = []
        if abort_db_episodes:
            try:
                aborted = self.store.abort_active()
            except Exception:  # noqa: BLE001
                aborted = []

        for episode_id in set(episode_ids) | set(aborted) | set(
                getattr(self.mirror, "mirrored", set())):
            self.mirror.remove(episode_id)

        return {"cleared_in_memory_episodes": episode_ids,
                "aborted_db_episodes": aborted,
                "bindings_cleared": True,
                "properties_touched": 0}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "camera_configured": self.config.camera_configured,
                "camera": {
                    "latitude": self.config.camera.latitude,
                    "longitude": self.config.camera.longitude,
                    "heading_deg": self.config.camera.heading_deg,
                },
                "dwell_s": self.config.dwell_s,
                "leave_grace_s": self.config.leave_grace_s,
                # The window in which a second tap can still correct an
                # episode that has just closed. Exposed because it is the
                # only thing that separates "the tap was too late" from
                # "the tap was refused" on demo day.
                "trigger_late_grace_s": self.config.trigger_late_grace_s,
                "bindings": [
                    {"collector_id": b.collector_id, "track_id": b.track_id,
                     "source_id": b.source_id, "session_id": b.session_id,
                     "rfid_uid": b.rfid_uid, "bound_at": b.bound_at}
                    for b in self._bindings.values()
                ],
                "candidates": [
                    {"collector_id": c, "property_id": cd.property_id,
                     "dwell_s": round((cd.last_seen - cd.first_seen).total_seconds(), 2),
                     "confidence": cd.confidence}
                    for c, cd in self._candidates.items()
                ],
                "active_episodes": [
                    {"collector_id": c, "episode_id": a.episode_id,
                     "property_id": a.property_id, "track_id": a.track_id,
                     "segregation_status": a.segregation_status,
                     "association_status": a.association_status,
                     "started_at": a.started_at, "last_inside": a.last_inside,
                     "observations": a.observations}
                    for c, a in self._active.items()
                ],
                "stats": dict(self.stats),
            }


# --- process-wide instance ---------------------------------------------------
_engine: EpisodeEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> EpisodeEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            from ..config import settings
            from ..gis import lookup_property
            from .mirror import get_mirror
            from .store import EpisodeStore
            _engine = EpisodeEngine(
                config=EpisodeConfig.from_settings(settings),
                store=EpisodeStore(),
                associator=lookup_property,
                mirror=get_mirror(),
            )
        return _engine


def set_engine(engine: EpisodeEngine | None) -> None:
    """Replace the process engine. Tests only."""
    global _engine
    with _engine_lock:
        _engine = engine
