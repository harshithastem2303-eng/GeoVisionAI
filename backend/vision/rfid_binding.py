"""RFID tap -> camera track attribution, and what a tap means.

This is the subsystem that establishes *worker identity*. The rule it exists
to enforce:

    A person is never a garbage worker because they were detected first.
    RFID says *who*. The camera says *which track*.

A tap means one of two things, decided entirely by the state the system is in
when it lands:

**No collector bound** -- "identify me and lock me to a camera track."
The tag resolves to a collector; among the people the camera was tracking at
the tap instant, the one **closest to the camera** with a valid depth reading
is the one who reached out and tapped. Tracks overlapping the reader zone are
preferred when the zone singles any out, because a surveyed zone is stronger
evidence than proximity alone; when it does not, every valid-depth track in
the window is eligible and the result says so. With no depth at all -- a plain
webcam, depth disabled, a frame of holes -- the original zone-overlap rule
still applies, so a depth outage degrades the decision instead of stopping it.

**A bound collector taps again** -- "the waste at the property I am servicing
right now is not segregated." That path lives in
:class:`RFIDBindingService` and depends on the episode registry, not on the
camera.

Once bound, a track is **locked**. It is not re-chosen frame by frame, and a
stranger who later walks closer to the camera does not take it over. The lock
is released only by the binding grace timeout, a new capture session, or an
explicit release.

When the evidence does not single out one person the answer is AMBIGUOUS and
nothing is bound. Guessing here would silently mislabel a pedestrian as an
authorised picker, which is the exact failure this module replaces.

Imports stdlib only -- no numpy, no OpenCV -- so the logic is testable with
no hardware and no vision stack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .types import (
    BBox,
    BindingResolution,
    BindingStatus,
    SelectionRule,
    TapIntent,
    TrackCandidate,
    TrackSnapshot,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence zone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RFIDEvidenceZone:
    """Rectangle in image coordinates covering the physical RFID reader.

    Coordinates come from configuration, never from detection code, so the
    installation can be re-surveyed without touching the pipeline.
    """

    x1: int
    y1: int
    x2: int
    y2: int

    @classmethod
    def from_tuple(cls, values: Sequence[int]) -> "RFIDEvidenceZone":
        x1, y1, x2, y2 = values
        return cls(
            x1=int(min(x1, x2)),
            y1=int(min(y1, y2)),
            x2=int(max(x1, x2)),
            y2=int(max(y1, y2)),
        )

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)

    def is_valid(self) -> bool:
        return self.area > 0

    def overlap_ratio(self, bbox: BBox) -> float:
        """Fraction of ``bbox`` that falls inside the zone (0.0 .. 1.0).

        Normalised by the *person's* area rather than the zone's: a person
        standing squarely at the reader is fully inside the zone even if they
        are small in frame, which is the property we actually want.
        """

        bx1, by1, bx2, by2 = bbox
        box_area = max(0, bx2 - bx1) * max(0, by2 - by1)
        if box_area <= 0:
            return 0.0

        inter_w = max(0, min(bx2, self.x2) - max(bx1, self.x1))
        inter_h = max(0, min(by2, self.y2) - max(by1, self.y1))
        return (inter_w * inter_h) / float(box_area)

    def to_dict(self) -> dict:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------


#: Default metre gap the closest person must beat the runner-up by before the
#: tap is attributed to them. Two people standing shoulder to shoulder at the
#: reader are within noise of each other and must stay AMBIGUOUS.
DEFAULT_DEPTH_MARGIN_M = 0.5

#: Default window in which a repeated read of the same card by the same
#: collector is the *same* tap rather than a new one. The reader holds a UID
#: while the card sits on it and the bridge polls faster than a hand moves, so
#: one physical tap routinely arrives two or three times.
DEFAULT_BIND_ECHO_S = 2.0


def summarize_tracks(
    zone: RFIDEvidenceZone,
    snapshots: Sequence[TrackSnapshot],
) -> List[TrackCandidate]:
    """One candidate per track id, summarising the whole window.

    Two summaries per track, each taken over its *best* frame rather than its
    mean. A tap is an instant: a worker who steps in, taps and steps back
    should not be diluted by the frames either side.

    * ``overlap`` -- the strongest occupation of the reader zone.
    * ``depth_m`` -- the **closest** valid depth reading, i.e. the nearest
      that person came to the camera while the tap was happening.
    """

    best: Dict[int, TrackCandidate] = {}

    for snapshot in snapshots:
        for detection in snapshot.detections:
            if detection.track_id is None:
                continue

            overlap = zone.overlap_ratio(detection.bbox)
            current = best.get(detection.track_id)
            if current is None:
                current = TrackCandidate(
                    track_id=detection.track_id,
                    overlap=overlap,
                    bbox=detection.bbox,
                )
                best[detection.track_id] = current
            elif overlap > current.overlap:
                current.overlap = overlap
                current.bbox = detection.bbox

            if detection.depth_valid and detection.depth_m is not None:
                depth = float(detection.depth_m)
                if not current.depth_valid or depth < current.depth_m:
                    current.depth_m = depth
                    current.depth_valid = True

    return list(best.values())


def collect_candidates(
    zone: RFIDEvidenceZone,
    snapshots: Sequence[TrackSnapshot],
    min_overlap: float,
) -> List[TrackCandidate]:
    """Tracks that occupied the reader zone, best first.

    Retained with its original signature: the zone remains the preferred
    evidence and the fallback rule when no depth exists.
    """

    candidates = [
        c for c in summarize_tracks(zone, snapshots) if c.overlap >= min_overlap
    ]
    candidates.sort(key=lambda c: (-c.overlap, c.track_id))
    return candidates


def _confidence(best: float, runner_up: float) -> float:
    """Blend how *well* the winner occupied the zone with how *clearly* it won.

    Both halves matter. High coverage with a close rival is not confident;
    a lone candidate barely clipping the zone is not confident either.
    """

    if best <= 0:
        return 0.0
    coverage = min(1.0, best)
    separation = (best - runner_up) / best
    separation = max(0.0, min(1.0, separation))
    return round(min(0.99, 0.5 * coverage + 0.5 * separation), 3)


def _depth_confidence(
    winner: TrackCandidate,
    runner_up: Optional[TrackCandidate],
    depth_margin_m: float,
    in_zone: bool,
) -> float:
    """How sure we are that the closest person is the one who tapped.

    Two halves again, and both are honest about what is missing. *Coverage*
    is the reader zone's corroboration -- full weight when the winner is
    inside it, heavily discounted when the zone singled nobody out and
    proximity is carrying the decision alone. *Separation* is how much
    clear air there is behind the winner, measured in metres against the
    ambiguity margin.
    """

    if in_zone:
        coverage = min(1.0, max(0.0, winner.overlap))
    else:
        # Nothing in the reader zone. Proximity alone is real evidence, but
        # it is weaker evidence, and the number should say so.
        coverage = 0.35

    if runner_up is None or runner_up.depth_m is None or winner.depth_m is None:
        separation = 1.0
    else:
        margin = max(1e-6, float(depth_margin_m))
        separation = (runner_up.depth_m - winner.depth_m) / (2.0 * margin)
        separation = max(0.0, min(1.0, separation))

    return round(min(0.99, 0.5 * coverage + 0.5 * separation), 3)


def _resolve_by_zone(
    candidates: List[TrackCandidate],
    min_overlap: float,
    ambiguity_margin: float,
) -> BindingResolution:
    """The original rule: strongest occupier of the reader zone wins.

    Reached when no track in the window carried a usable depth reading.
    """

    if not candidates:
        return BindingResolution(
            status=BindingStatus.NO_TRACK_IN_READER_ZONE,
            selection_rule=SelectionRule.ZONE_OVERLAP,
            reason=(
                "No tracked person occupied at least "
                f"{min_overlap:.0%} of the reader zone, and no depth reading "
                "was available to rank people by distance."
            ),
        )

    if len(candidates) == 1:
        winner = candidates[0]
        return BindingResolution(
            status=BindingStatus.BOUND,
            track_id=winner.track_id,
            confidence=_confidence(winner.overlap, 0.0),
            candidates=candidates,
            selection_rule=SelectionRule.ZONE_OVERLAP,
        )

    winner, runner_up = candidates[0], candidates[1]
    if (winner.overlap - runner_up.overlap) < ambiguity_margin:
        return BindingResolution(
            status=BindingStatus.AMBIGUOUS,
            candidates=candidates,
            selection_rule=SelectionRule.ZONE_OVERLAP,
            reason=(
                f"Tracks {winner.track_id} and {runner_up.track_id} both occupy "
                f"the reader zone ({winner.overlap:.2f} vs "
                f"{runner_up.overlap:.2f}); separation is below the "
                f"{ambiguity_margin:.2f} margin, and no depth was available "
                "to separate them."
            ),
        )

    return BindingResolution(
        status=BindingStatus.BOUND,
        track_id=winner.track_id,
        confidence=_confidence(winner.overlap, runner_up.overlap),
        candidates=candidates,
        selection_rule=SelectionRule.ZONE_OVERLAP,
    )


def resolve_tap(
    zone: RFIDEvidenceZone,
    snapshots: Sequence[TrackSnapshot],
    min_overlap: float,
    ambiguity_margin: float,
    depth_margin_m: float = DEFAULT_DEPTH_MARGIN_M,
) -> BindingResolution:
    """Decide which track, if any, produced an RFID tap.

    The order of evidence, strongest first:

    1. Valid-depth tracks **inside the reader zone** -- closest to the camera
       wins (``DEPTH_IN_ZONE``).
    2. If the zone singled nobody out, every valid-depth track in the window
       -- closest to the camera wins (``DEPTH_ANY``), at reduced confidence.
    3. If nothing in the window had usable depth, the original zone-overlap
       rule (``ZONE_OVERLAP``).

    Returns a resolution whose ``status`` is one of
    ``BOUND`` / ``AMBIGUOUS`` / ``NO_TRACK_IN_READER_ZONE`` / ``NO_TRACK_DATA``.
    It never picks a winner on weak evidence.
    """

    if not zone.is_valid():
        return BindingResolution(
            status=BindingStatus.NO_TRACK_DATA,
            reason="RFID evidence zone is not configured (zero area).",
        )

    if not snapshots:
        return BindingResolution(
            status=BindingStatus.NO_TRACK_DATA,
            reason="No tracking frames recorded around the tap timestamp.",
        )

    tracks = summarize_tracks(zone, snapshots)
    in_zone = [c for c in tracks if c.overlap >= min_overlap]
    in_zone.sort(key=lambda c: (-c.overlap, c.track_id))

    pool = [c for c in in_zone if c.depth_valid]
    rule = SelectionRule.DEPTH_IN_ZONE
    if not pool:
        pool = [c for c in tracks if c.depth_valid]
        rule = SelectionRule.DEPTH_ANY
    if not pool:
        # No depth anywhere in the window. Fall back to the zone rule rather
        # than refusing to bind -- a depth outage must not end the shift.
        return _resolve_by_zone(in_zone, min_overlap, ambiguity_margin)

    # Closest to the camera first; track id only to keep ordering stable.
    pool.sort(key=lambda c: (c.depth_m, c.track_id))
    winner = pool[0]
    runner_up = pool[1] if len(pool) > 1 else None
    zone_backed = rule == SelectionRule.DEPTH_IN_ZONE

    if runner_up is not None:
        gap = runner_up.depth_m - winner.depth_m
        if gap < depth_margin_m:
            return BindingResolution(
                status=BindingStatus.AMBIGUOUS,
                candidates=pool,
                selection_rule=rule,
                reason=(
                    f"Tracks {winner.track_id} and {runner_up.track_id} are "
                    f"{winner.depth_m:.2f} m and {runner_up.depth_m:.2f} m from "
                    f"the camera; the {gap:.2f} m gap is under the "
                    f"{depth_margin_m:.2f} m margin, so which of them tapped "
                    "cannot be told from depth."
                ),
            )

    reason = ""
    if not zone_backed:
        reason = (
            "No tracked person occupied the reader zone; the tap was "
            f"attributed to track {winner.track_id} at {winner.depth_m:.2f} m, "
            "the closest person to the camera."
        )

    return BindingResolution(
        status=BindingStatus.BOUND,
        track_id=winner.track_id,
        confidence=_depth_confidence(winner, runner_up, depth_margin_m, zone_backed),
        candidates=pool,
        selection_rule=rule,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

#: Maps an RFID tag to a collector id, or ``None`` if the tag is unknown.
#: Injected so this module never imports the database layer.
CollectorLookup = Callable[[str], Optional[str]]


class RFIDBindingService:
    """Wires RFID identity, camera evidence and the worker registry together.

    Also decides what a tap *meant*. The branch is on system state, not on
    anything in the tap itself -- the reader sends the same eight hex digits
    either way:

    * collector not bound  -> :const:`TapIntent.BIND`
    * collector bound      -> :const:`TapIntent.NON_SEGREGATION`

    The second branch consults the episode mirror WASTRAQ maintains here. It
    is a mirror and nothing more: GeoVision never opens an episode itself,
    never infers one from dwell, and never decides which property it belongs
    to. When the mirror shows no open episode the tap marks nothing at all --
    not the previous property, not a nearby one, not a guess -- and the
    outcome still travels to WASTRAQ, which holds the authoritative episode
    table and has the final word.
    """

    def __init__(
        self,
        zone: RFIDEvidenceZone,
        history,
        registry,
        collector_lookup: CollectorLookup,
        match_window_s: float,
        min_overlap: float,
        ambiguity_margin: float,
        depth_margin_m: float = DEFAULT_DEPTH_MARGIN_M,
        episodes=None,
        retap_debounce_s: float = 10.0,
        bind_echo_s: float = DEFAULT_BIND_ECHO_S,
    ) -> None:
        self.zone = zone
        self.history = history
        self.registry = registry
        self.collector_lookup = collector_lookup
        self.match_window_s = match_window_s
        self.min_overlap = min_overlap
        self.ambiguity_margin = ambiguity_margin
        self.depth_margin_m = depth_margin_m
        #: Optional. Without it a bound collector's re-tap always reports
        #: NO_ACTIVE_EPISODE, which is the safe answer, not a crash.
        self.episodes = episodes
        self.retap_debounce_s = retap_debounce_s
        #: How long after a binding a repeat read of the same card is treated
        #: as the same tap rather than a new instruction. See
        #: :meth:`_is_bind_echo`.
        self.bind_echo_s = bind_echo_s

    # -- entry point ------------------------------------------------------

    def handle_tap(self, rfid_id: str, timestamp: float) -> dict:
        """Process one RFID tap. Returns the API-shaped result."""

        rfid_id = (rfid_id or "").strip()
        if not rfid_id:
            return self._unknown(rfid_id, "Empty RFID id.")

        # 1. RFID -> collector. Identity first; an unknown tag does nothing,
        #    neither binding nor triggering.
        collector_id = self.collector_lookup(rfid_id)
        if not collector_id:
            logger.info("RFID tap from unknown tag %s rejected", rfid_id)
            return self._unknown(
                rfid_id, "RFID tag is not assigned to any collector."
            )

        # 2. Which of the two meanings does this tap carry? Decided by whether
        #    this collector already holds a locked track.
        existing = self.registry.binding_for_collector(collector_id, now=timestamp)
        if existing is not None:
            # 2a. ...unless this is the identifying tap arriving twice. One
            #     physical tap is several reads; re-reading a card the instant
            #     after it bound is not a second instruction.
            if self._is_bind_echo(existing, rfid_id, timestamp):
                return self._bind_echo(existing, rfid_id, timestamp)
            return self._handle_retap(rfid_id, collector_id, existing, timestamp)
        return self._handle_bind(rfid_id, collector_id, timestamp)

    # -- Case 1b: the identifying tap, arriving twice ----------------------

    def _is_bind_echo(self, binding, rfid_id: str, timestamp: float) -> bool:
        """Is this read the same physical tap that produced ``binding``?

        Two conditions, both required. The card must be the one the binding
        was made with -- a collector carrying a second tag is giving a real
        second instruction -- and the read must land within
        ``bind_echo_s`` of the binding being made.

        ``abs`` rather than a forward-only comparison: the reader bridge
        stamps its own time and two reads of one tap can arrive out of order.
        A read a fraction of a second *before* the binding is still that same
        tap, and treating it as a fresh instruction would be worse.
        """

        if self.bind_echo_s <= 0:
            return False
        if binding.rfid_id != rfid_id:
            return False
        return abs(timestamp - binding.bound_at) <= self.bind_echo_s

    def _bind_echo(self, binding, rfid_id: str, timestamp: float) -> dict:
        """Answer a duplicate identifying tap with the binding it already made.

        Deliberately the *same* answer as the tap it echoes -- same status,
        same track, same session, same confidence -- so a caller that saw the
        first response and a caller that saw only this one agree about the
        state of the world. ``duplicate`` is the single bit that says nothing
        new happened, and the route uses it to suppress a second
        WORKER_TRACK_BOUND event and a second evidence clip.
        """

        logger.info(
            "Duplicate identifying tap %s (%s) %.2fs after binding to track %s "
            "-- returning the existing binding, nothing changed",
            rfid_id,
            binding.collector_id,
            abs(timestamp - binding.bound_at),
            binding.track_id,
        )
        return {
            "status": BindingStatus.BOUND,
            "intent": TapIntent.BIND,
            "rfid_id": rfid_id,
            "collector_id": binding.collector_id,
            "track_id": binding.track_id,
            "confidence": round(binding.confidence, 3),
            "candidate_track_ids": [binding.track_id],
            "candidates": [],
            "selection_rule": binding.selection_rule,
            "session_id": binding.session_id,
            "bound_at": binding.bound_at,
            "event_timestamp": timestamp,
            "frames_considered": 0,
            "zone": self.zone.to_dict(),
            "episode_id": None,
            "trigger_id": None,
            "locked": True,
            "duplicate": True,
            "reason": (
                f"Collector {binding.collector_id} was bound to track "
                f"{binding.track_id} "
                f"{abs(timestamp - binding.bound_at):.2f}s ago by this same "
                "card; this read is the same tap arriving again. The binding "
                "is unchanged and nothing else was triggered."
            ),
        }

    # -- Case 1: identify and lock ----------------------------------------

    def _handle_bind(
        self,
        rfid_id: str,
        collector_id: str,
        timestamp: float,
    ) -> dict:
        """No collector bound: identify the tapper and lock their track."""

        snapshots = self.history.window(timestamp, self.match_window_s)
        resolution = resolve_tap(
            zone=self.zone,
            snapshots=snapshots,
            min_overlap=self.min_overlap,
            ambiguity_margin=self.ambiguity_margin,
            depth_margin_m=self.depth_margin_m,
        )

        payload = resolution.to_dict()
        payload["intent"] = TapIntent.BIND
        payload["rfid_id"] = rfid_id
        payload["collector_id"] = collector_id
        payload["event_timestamp"] = timestamp
        payload["frames_considered"] = len(snapshots)
        payload["zone"] = self.zone.to_dict()
        payload["episode_id"] = None
        # This tap did the work. Its echoes, if the reader sends any, say
        # ``True`` here and change nothing.
        payload["duplicate"] = False

        if resolution.status != BindingStatus.BOUND:
            logger.info(
                "RFID tap %s (%s) unresolved: %s",
                rfid_id,
                collector_id,
                resolution.status,
            )
            payload["collector_id"] = collector_id
            payload["track_id"] = None
            payload["locked"] = False
            return payload

        binding = self.registry.bind(
            collector_id=collector_id,
            rfid_id=rfid_id,
            track_id=resolution.track_id,
            confidence=resolution.confidence,
            event_timestamp=timestamp,
            # One clock. The tap's timestamp, the frames it was matched
            # against and the binding's own lifetime must all be measured the
            # same way, or a binding made from a slightly late tap expires
            # against a different clock than it was born on.
            now=timestamp,
            selection_rule=resolution.selection_rule,
        )
        payload["session_id"] = binding.session_id
        payload["bound_at"] = binding.bound_at
        # From here the track is followed exclusively. It is not re-picked
        # every frame, and a nearer stranger does not take it.
        payload["locked"] = True
        logger.info(
            "Locked collector %s to track %s (%s, confidence %.2f)",
            collector_id,
            resolution.track_id,
            resolution.selection_rule,
            resolution.confidence,
        )
        return payload

    # -- Cases 2 and 3: a bound collector taps again ----------------------

    def _handle_retap(
        self,
        rfid_id: str,
        collector_id: str,
        binding,
        timestamp: float,
    ) -> dict:
        """Already bound: this tap says "the waste here is not segregated"."""

        payload = {
            "intent": TapIntent.NON_SEGREGATION,
            "rfid_id": rfid_id,
            "collector_id": collector_id,
            "track_id": binding.track_id,
            "session_id": binding.session_id,
            "event_timestamp": timestamp,
            "confidence": round(binding.confidence, 3),
            "candidate_track_ids": [],
            "candidates": [],
            "selection_rule": binding.selection_rule,
            "locked": True,
            "episode_id": None,
            "trigger_id": None,
        }

        episode = None
        if self.episodes is not None:
            episode = self.episodes.active_for_track(
                binding.track_id,
                session_id=binding.session_id,
                now=timestamp,
            )

        if episode is None:
            # Case 3. Nothing is marked. Reporting this is the whole point --
            # a tap with no subject must not fall back onto a previous
            # property or the nearest one.
            payload["status"] = BindingStatus.NO_ACTIVE_EPISODE
            payload["reason"] = (
                f"Collector {collector_id} is bound to track "
                f"{binding.track_id}, but WASTRAQ has no open collection "
                "episode for that track. No property was modified."
            )
            logger.info(
                "Re-tap by %s on track %s ignored: no active episode",
                collector_id,
                binding.track_id,
            )
            return payload

        payload["episode_id"] = episode.episode_id
        payload["association_status"] = episode.association_status

        if not episode.is_actionable:
            payload["status"] = BindingStatus.EPISODE_NOT_ACTIONABLE
            payload["reason"] = (
                f"Episode {episode.episode_id} is "
                f"{episode.association_status or 'UNSPECIFIED'}; only "
                "AUTO_ASSOCIATED or REVIEW episodes may be flagged. No "
                "property was modified."
            )
            return payload

        trigger_id, was_new = self.episodes.mark_non_segregated(
            episode,
            now=timestamp,
            debounce_s=self.retap_debounce_s,
        )
        payload["trigger_id"] = trigger_id
        payload["status"] = (
            BindingStatus.NON_SEGREGATION
            if was_new
            else BindingStatus.DUPLICATE_TRIGGER
        )
        if not was_new:
            payload["reason"] = (
                f"Episode {episode.episode_id} is already flagged by trigger "
                f"{trigger_id}. Repeating the tap changes nothing."
            )
        logger.info(
            "Non-segregation %s by %s on track %s, episode %s (trigger %s)",
            "raised" if was_new else "repeated",
            collector_id,
            binding.track_id,
            episode.episode_id,
            trigger_id,
        )
        return payload

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _unknown(rfid_id: str, reason: str) -> dict:
        return {
            "status": BindingStatus.UNKNOWN_RFID,
            "intent": None,
            "rfid_id": rfid_id,
            "collector_id": None,
            "track_id": None,
            "episode_id": None,
            "trigger_id": None,
            "reason": reason,
        }
