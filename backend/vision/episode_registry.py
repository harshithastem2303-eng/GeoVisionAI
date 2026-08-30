"""Which bound tracks are currently servicing something.

An RFID tap from a collector who is *already bound* does not mean "identify
me" -- it means "the waste in front of me is not segregated". That sentence
only has a subject if the system knows which collection is in progress.

GeoVision cannot work that out for itself. It has no service zones, no
Property Master and no dwell logic; deciding which house a picker is at is
WASTRAQ's job and stays WASTRAQ's job. So WASTRAQ *tells* GeoVision, over
``POST /episodes/active``, that a track has an episode open -- and closes it
again when the picker leaves.

What crosses the boundary is deliberately thin::

    episode_id           WASTRAQ's opaque handle, echoed back untouched
    track_id             which camera track is servicing
    association_status   AUTO_ASSOCIATED / REVIEW -- the *word*, not the house

No property id, no house number, no service zone. GeoVision still cannot
name a property, and the guard in ``integration.events`` still refuses to
publish one.

Episodes expire on their own. If WASTRAQ crashes mid-lane, a stale episode
must not sit here waiting to absorb a tap an hour later and mark the wrong
house.

Stdlib only, so this is testable with no camera, no database and no network.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Dict, List, Optional

from .types import AssociationStatus, CollectionEpisode

logger = logging.getLogger(__name__)

#: Keys that would name a property. Accepted from WASTRAQ over the wire would
#: be bad enough; *stored* here they would eventually be published. Dropped on
#: arrival, loudly.
FORBIDDEN_EPISODE_FIELDS = frozenset(
    {
        "property_id",
        "authority_property_id",
        "property_name",
        "house_number",
        "formatted_address",
        "service_zone_id",
        "zone_id",
        "entrance_id",
        "frontage_id",
        "segregation_status",
    }
)


def new_trigger_id() -> str:
    """A fresh non-segregation trigger id, for idempotent ingestion."""

    return str(uuid.uuid4())


class EpisodeRegistry:
    """Thread-safe store of open collection episodes, keyed by track."""

    def __init__(self, max_age_s: float = 180.0) -> None:
        self.max_age_s = max_age_s
        self._lock = threading.RLock()
        self._by_track: Dict[int, CollectionEpisode] = {}

    # -- lifecycle --------------------------------------------------------

    def open(
        self,
        episode_id: str,
        track_id: int,
        session_id: str,
        association_status: str,
        collector_id: Optional[str] = None,
        opened_at: Optional[float] = None,
        now: Optional[float] = None,
    ) -> CollectionEpisode:
        """Record (or refresh) the open episode for one track.

        One track services one thing at a time, so opening an episode for a
        track that already has one *replaces* it: WASTRAQ has moved the picker
        on to the next house and the old episode is over by definition.
        """

        now = self._now(now)
        episode = CollectionEpisode(
            episode_id=str(episode_id),
            track_id=int(track_id),
            session_id=session_id,
            association_status=(association_status or "").strip().upper(),
            collector_id=collector_id,
            opened_at=self._now(opened_at),
            updated_at=now,
        )

        with self._lock:
            previous = self._by_track.get(episode.track_id)
            if previous is not None and previous.episode_id != episode.episode_id:
                logger.info(
                    "Episode %s replaced by %s on track %s",
                    previous.episode_id,
                    episode.episode_id,
                    episode.track_id,
                )
            elif previous is not None:
                # Same episode re-announced: keep any trigger already recorded
                # so a re-push from WASTRAQ cannot re-arm a spent trigger.
                episode.non_segregation_trigger_id = previous.non_segregation_trigger_id
                episode.non_segregation_at = previous.non_segregation_at
                episode.opened_at = previous.opened_at
            self._by_track[episode.track_id] = episode

        return episode

    def close(
        self,
        episode_id: Optional[str] = None,
        track_id: Optional[int] = None,
    ) -> Optional[CollectionEpisode]:
        """Close by episode id or by track. Returns what was closed, if any."""

        with self._lock:
            if track_id is not None:
                return self._by_track.pop(int(track_id), None)
            if episode_id is None:
                return None
            for key, episode in list(self._by_track.items()):
                if episode.episode_id == episode_id:
                    del self._by_track[key]
                    return episode
        return None

    def clear(self) -> None:
        with self._lock:
            self._by_track.clear()

    def expire(self, now: Optional[float] = None) -> List[CollectionEpisode]:
        """Drop episodes older than ``max_age_s``. Returns the ones removed."""

        now = self._now(now)
        removed: List[CollectionEpisode] = []
        with self._lock:
            for track_id, episode in list(self._by_track.items()):
                if (now - episode.updated_at) > self.max_age_s:
                    removed.append(episode)
                    del self._by_track[track_id]
        for episode in removed:
            logger.info(
                "Episode %s on track %s expired after %.0fs without an update",
                episode.episode_id,
                episode.track_id,
                self.max_age_s,
            )
        return removed

    # -- queries ----------------------------------------------------------

    def active_for_track(
        self,
        track_id: Optional[int],
        session_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[CollectionEpisode]:
        """The open episode for a track, or ``None``.

        Session-scoped like a worker binding: BoT-SORT renumbers on restart,
        so an episode from a previous run must never be matched by number.
        """

        if track_id is None:
            return None
        now = self._now(now)
        with self._lock:
            episode = self._by_track.get(int(track_id))
            if episode is None:
                return None
            if session_id is not None and episode.session_id != session_id:
                return None
            if (now - episode.updated_at) > self.max_age_s:
                return None
            return episode

    def episodes(self) -> List[CollectionEpisode]:
        with self._lock:
            return list(self._by_track.values())

    def to_list(self) -> List[dict]:
        return [episode.to_dict() for episode in self.episodes()]

    # -- the non-segregation trigger --------------------------------------

    def mark_non_segregated(
        self,
        episode: CollectionEpisode,
        now: Optional[float] = None,
        debounce_s: float = 10.0,
    ) -> tuple:
        """Record a non-segregation trigger against an episode, idempotently.

        Returns ``(trigger_id, was_new)``. A repeated tap inside
        ``debounce_s`` -- a card held a moment too long, a retried HTTP post,
        a bouncing reader -- returns the *original* trigger id with
        ``was_new=False``, so WASTRAQ sees one trigger no matter how many taps
        arrive. After the debounce a deliberate re-tap still does not create a
        second trigger: the episode is already flagged, and flagging it twice
        would say nothing new.
        """

        now = self._now(now)
        with self._lock:
            live = self._by_track.get(episode.track_id)
            if live is None or live.episode_id != episode.episode_id:
                # The episode moved on between the check and the mark.
                live = episode

            if live.non_segregation_trigger_id is not None:
                live.updated_at = now
                return live.non_segregation_trigger_id, False

            trigger_id = new_trigger_id()
            live.non_segregation_trigger_id = trigger_id
            live.non_segregation_at = now
            live.updated_at = now
            self._by_track[live.track_id] = live
            return trigger_id, True

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def sanitize(payload: dict) -> dict:
        """Drop any property-naming field WASTRAQ sent, loudly."""

        offending = sorted(set(payload or {}) & FORBIDDEN_EPISODE_FIELDS)
        if offending:
            logger.error(
                "Episode payload carried property field(s) %s -- dropped. "
                "GeoVision does not store which property is being serviced.",
                ", ".join(offending),
            )
        return {
            key: value
            for key, value in (payload or {}).items()
            if key not in FORBIDDEN_EPISODE_FIELDS
        }

    @staticmethod
    def is_actionable(status: Optional[str]) -> bool:
        return AssociationStatus.is_actionable(status)

    @staticmethod
    def _now(now: Optional[float]) -> float:
        if now is not None:
            return float(now)
        import time

        return time.time()
