"""Which camera tracks are currently authorised garbage workers.

Replaces the old single global ``TARGET_ID``. Two or three collectors work a
lane simultaneously, so identity is a *map* of concurrent bindings, not one
slot.

Two lifecycle rules matter more than the rest:

* **Short occlusion must not destroy identity.** A worker who walks behind
  the vehicle for two seconds is still that worker. Bindings therefore
  survive ``grace`` seconds of not being seen.
* **A tracker id must never outlive its session.** BoT-SORT restarts its
  numbering when the pipeline restarts, so track 14 after a restart is a
  different human from track 14 before it. Every binding carries the
  ``session_id`` it was made in and is dropped when the session changes.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Dict, Iterable, List, Optional

from .types import WorkerBinding

logger = logging.getLogger(__name__)


class WorkerRegistry:
    """Thread-safe store of ``collector_id <-> track_id`` bindings."""

    def __init__(self, grace_s: float = 20.0, max_age_s: float = 3600.0) -> None:
        self.grace_s = grace_s
        self.max_age_s = max_age_s

        self._lock = threading.RLock()
        self._session_id: str = self._new_session_id()
        self._by_collector: Dict[str, WorkerBinding] = {}

    # -- session ----------------------------------------------------------

    @staticmethod
    def _new_session_id() -> str:
        return uuid.uuid4().hex[:12]

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def start_session(self) -> str:
        """Begin a new capture run and drop every binding from the old one.

        Called whenever the camera pipeline starts. Stale BoT-SORT ids from a
        previous run are not merely untrusted -- they are discarded.
        """

        with self._lock:
            previous = self._session_id
            dropped = len(self._by_collector)
            self._session_id = self._new_session_id()
            self._by_collector.clear()
            logger.info(
                "Vision session %s -> %s (%d binding(s) dropped)",
                previous,
                self._session_id,
                dropped,
            )
            return self._session_id

    # -- binding ----------------------------------------------------------

    def bind(
        self,
        collector_id: str,
        rfid_id: str,
        track_id: int,
        confidence: float,
        event_timestamp: float,
        now: Optional[float] = None,
        selection_rule: str = "",
    ) -> WorkerBinding:
        """Bind a collector to a track for the current session, and lock it.

        A collector holds at most one track and a track belongs to at most one
        collector; re-binding either side releases the previous association.

        The lock matters as much as the binding. Nothing re-runs the "who is
        closest to the camera" question while a binding is live -- callers ask
        :meth:`binding_for_collector` first and, finding one, treat the tap as
        a non-segregation trigger rather than a fresh identification.
        """

        now = self._now(now)
        with self._lock:
            # A track can only represent one human.
            for other_id, other in list(self._by_collector.items()):
                if other.track_id == track_id and other_id != collector_id:
                    logger.info(
                        "Track %s reassigned from %s to %s",
                        track_id,
                        other_id,
                        collector_id,
                    )
                    del self._by_collector[other_id]

            binding = WorkerBinding(
                collector_id=collector_id,
                rfid_id=rfid_id,
                track_id=int(track_id),
                session_id=self._session_id,
                event_timestamp=event_timestamp,
                bound_at=now,
                last_seen=now,
                confidence=confidence,
                selection_rule=selection_rule,
            )
            self._by_collector[collector_id] = binding
            return binding

    def release(self, collector_id: str) -> bool:
        with self._lock:
            return self._by_collector.pop(collector_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._by_collector.clear()

    # -- lifecycle --------------------------------------------------------

    def touch(self, track_ids: Iterable[int], now: Optional[float] = None) -> None:
        """Refresh ``last_seen`` for every binding whose track is in frame."""

        now = self._now(now)
        visible = {int(t) for t in track_ids if t is not None}
        if not visible:
            return
        with self._lock:
            for binding in self._by_collector.values():
                if binding.track_id in visible:
                    binding.last_seen = now

    def expire(self, now: Optional[float] = None) -> List[WorkerBinding]:
        """Drop bindings past the occlusion grace or the absolute ceiling.

        Returns the bindings removed, so callers can log or surface them.
        """

        now = self._now(now)
        removed: List[WorkerBinding] = []
        with self._lock:
            for collector_id, binding in list(self._by_collector.items()):
                unseen_for = now - binding.last_seen
                age = now - binding.bound_at
                if unseen_for > self.grace_s or age > self.max_age_s:
                    binding.status = "EXPIRED"
                    removed.append(binding)
                    del self._by_collector[collector_id]
        for binding in removed:
            logger.info(
                "Binding expired: %s <-> track %s",
                binding.collector_id,
                binding.track_id,
            )
        return removed

    # -- queries ----------------------------------------------------------

    def binding_for_track(
        self,
        track_id: Optional[int],
        now: Optional[float] = None,
    ) -> Optional[WorkerBinding]:
        """The live binding for a track, or ``None``.

        Session-scoped: a binding made in an earlier run never matches, even
        if the tracker happens to reuse the number.
        """

        if track_id is None:
            return None
        now = self._now(now)
        with self._lock:
            for binding in self._by_collector.values():
                if binding.track_id != int(track_id):
                    continue
                if binding.session_id != self._session_id:
                    continue
                if (now - binding.last_seen) > self.grace_s:
                    continue
                if (now - binding.bound_at) > self.max_age_s:
                    continue
                return binding
        return None

    def binding_for_collector(
        self,
        collector_id: Optional[str],
        now: Optional[float] = None,
    ) -> Optional[WorkerBinding]:
        """The live binding held by a collector, or ``None``.

        This is the question that decides what an RFID tap *means*. A live
        binding makes the tap a non-segregation trigger; its absence makes the
        tap an identification. Same expiry rules as
        :meth:`binding_for_track`, so a collector whose track was lost beyond
        the grace period is free to identify themselves again.
        """

        if not collector_id:
            return None
        now = self._now(now)
        with self._lock:
            binding = self._by_collector.get(collector_id)
            if binding is None:
                return None
            if binding.session_id != self._session_id:
                return None
            if (now - binding.last_seen) > self.grace_s:
                return None
            if (now - binding.bound_at) > self.max_age_s:
                return None
            return binding

    def active_track_ids(self, now: Optional[float] = None) -> List[int]:
        """Every locked picker track. Collection logic follows only these."""

        now = self._now(now)
        with self._lock:
            return [
                binding.track_id
                for binding in self._by_collector.values()
                if binding.session_id == self._session_id
                and (now - binding.last_seen) <= self.grace_s
                and (now - binding.bound_at) <= self.max_age_s
            ]

    def is_authorized(
        self,
        track_id: Optional[int],
        now: Optional[float] = None,
    ) -> bool:
        return self.binding_for_track(track_id, now=now) is not None

    def collector_for_track(
        self,
        track_id: Optional[int],
        now: Optional[float] = None,
    ) -> Optional[str]:
        binding = self.binding_for_track(track_id, now=now)
        return binding.collector_id if binding else None

    def bindings(self) -> List[WorkerBinding]:
        with self._lock:
            return list(self._by_collector.values())

    def to_list(self) -> List[dict]:
        return [binding.to_dict() for binding in self.bindings()]

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _now(now: Optional[float]) -> float:
        if now is not None:
            return now
        import time

        return time.time()
