"""Shared value objects for the vision subsystem.

Kept free of numpy, OpenCV, ultralytics and pyrealsense2 so that the
identity logic can be imported and tested with nothing installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

BBox = Tuple[int, int, int, int]  # x1, y1, x2, y2 in image coordinates


@dataclass(frozen=True)
class PersonDetection:
    """One person in one frame.

    YOLO answers *only* "this is a person". It does not, and must not,
    decide that a person is a garbage worker -- that comes from RFID.

    ``depth_m`` is the distance from the camera to this person in this frame,
    when the depth stream produced a usable measurement. It is retained on the
    detection -- and therefore in :class:`TrackSnapshot` history -- because an
    RFID tap has to be attributed to the person *closest to the camera* at the
    tap instant, which cannot be recovered after the frame is gone. It stays
    optional: with a plain webcam every detection carries ``depth_valid=False``
    and identity resolution falls back to the reader zone alone.
    """

    track_id: Optional[int]
    bbox: BBox
    confidence: float
    depth_m: Optional[float] = None
    depth_valid: bool = False

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0, x2 - x1) * max(0, y2 - y1)

    def bottom_center(self) -> Tuple[float, float]:
        """Representative ground-contact point: ``u = (x1+x2)/2, v = y2``."""

        x1, _y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, float(y2))

    def with_depth(
        self,
        depth_m: Optional[float],
        depth_valid: bool,
    ) -> "PersonDetection":
        """A copy carrying a depth measurement. Frozen, so never mutated."""

        valid = bool(depth_valid) and depth_m is not None
        return PersonDetection(
            track_id=self.track_id,
            bbox=self.bbox,
            confidence=self.confidence,
            depth_m=float(depth_m) if valid else None,
            depth_valid=valid,
        )

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "bbox": list(self.bbox),
            "detection_confidence": round(self.confidence, 3),
            "depth_m": self.depth_m,
            "depth_valid": self.depth_valid,
        }


@dataclass(frozen=True)
class TrackSnapshot:
    """Every person the tracker reported in a single frame, with a timestamp.

    An RFID tap arrives with its own timestamp and has to be matched against
    what the camera saw *around* that moment, so frames must be retained
    rather than only the latest one.
    """

    timestamp: float
    detections: Sequence[PersonDetection] = field(default_factory=tuple)


@dataclass
class WorkerBinding:
    """A resolved ``collector_id <-> track_id`` association.

    Once made, a binding is a **lock**. The track it names stays the active
    picker track for that collector until the binding is released -- it is
    never re-selected frame by frame, and a stranger walking closer to the
    camera does not steal it.
    """

    collector_id: str
    rfid_id: str
    track_id: int
    session_id: str
    event_timestamp: float
    bound_at: float
    last_seen: float
    confidence: float
    status: str = "BOUND"
    selection_rule: str = ""

    def to_dict(self) -> dict:
        return {
            "collector_id": self.collector_id,
            "rfid_id": self.rfid_id,
            "track_id": self.track_id,
            "session_id": self.session_id,
            "event_timestamp": self.event_timestamp,
            "bound_at": self.bound_at,
            "last_seen": self.last_seen,
            "identity_confidence": round(self.confidence, 3),
            "status": self.status,
            "selection_rule": self.selection_rule,
            # The track is followed exclusively while this binding lives.
            "locked": self.status == "BOUND",
        }


class BindingStatus:
    """Outcomes of an RFID tap. Never guess between them."""

    # -- Case 1: identify a collector and lock them to a track -------------
    BOUND = "BOUND"
    AMBIGUOUS = "AMBIGUOUS"
    NO_TRACK_IN_READER_ZONE = "NO_TRACK_IN_READER_ZONE"
    NO_TRACK_DATA = "NO_TRACK_DATA"
    UNKNOWN_RFID = "UNKNOWN_RFID"
    EXPIRED = "EXPIRED"

    # -- Case 2/3: a bound collector taps again ----------------------------
    #: Accepted: the waste at the bound track's current episode is not
    #: segregated. GeoVision signals it; WASTRAQ sets the status.
    NON_SEGREGATION = "NON_SEGREGATION"
    #: The same trigger arrived twice. Nothing changed; safe to ignore.
    DUPLICATE_TRIGGER = "DUPLICATE_TRIGGER"
    #: Bound, but no collection episode is open for that track. Nothing is
    #: marked -- never a previous or a random property.
    NO_ACTIVE_EPISODE = "NO_ACTIVE_EPISODE"
    #: An episode exists but WASTRAQ has not associated it confidently
    #: enough to act on.
    EPISODE_NOT_ACTIONABLE = "EPISODE_NOT_ACTIONABLE"


class AssociationStatus:
    """How confidently WASTRAQ associated an episode with a property.

    GeoVision stores the *word*, never the property. Only these two mean the
    episode may be acted on by a second tap; anything else -- AMBIGUOUS,
    UNRESOLVED, a status invented later -- is not actionable here.
    """

    AUTO_ASSOCIATED = "AUTO_ASSOCIATED"
    REVIEW = "REVIEW"

    ACTIONABLE = frozenset({AUTO_ASSOCIATED, REVIEW})

    @classmethod
    def is_actionable(cls, status: Optional[str]) -> bool:
        return (status or "").strip().upper() in cls.ACTIONABLE


@dataclass
class CollectionEpisode:
    """An open collection episode, as told to GeoVision by WASTRAQ.

    Deliberately property-free. WASTRAQ knows which house this is; GeoVision
    only needs to know *that* a track is currently servicing something, so a
    second RFID tap has an unambiguous subject. ``episode_id`` is WASTRAQ's
    opaque handle and travels back untouched.
    """

    episode_id: str
    track_id: int
    session_id: str
    association_status: str
    opened_at: float
    updated_at: float
    collector_id: Optional[str] = None
    non_segregation_trigger_id: Optional[str] = None
    non_segregation_at: Optional[float] = None

    @property
    def is_actionable(self) -> bool:
        return AssociationStatus.is_actionable(self.association_status)

    @property
    def non_segregated(self) -> bool:
        return self.non_segregation_trigger_id is not None

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "track_id": self.track_id,
            "collector_id": self.collector_id,
            "session_id": self.session_id,
            "association_status": self.association_status,
            "actionable": self.is_actionable,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "non_segregation_trigger_id": self.non_segregation_trigger_id,
            "non_segregation_at": self.non_segregation_at,
            "non_segregated": self.non_segregated,
        }


class TapIntent:
    """What a tap *meant*, given the state the system was in when it landed."""

    #: No collector bound: "identify me and lock me to a camera track."
    BIND = "BIND"
    #: Collector already bound: "the waste here is not segregated."
    NON_SEGREGATION = "NON_SEGREGATION"


class SelectionRule:
    """How the tapping track was chosen. Recorded on every resolution."""

    #: Valid-depth tracks inside the reader zone; closest to camera wins.
    DEPTH_IN_ZONE = "DEPTH_IN_ZONE"
    #: No valid-depth track in the zone, so every valid-depth track in the
    #: window was eligible; closest to camera wins. Lower confidence.
    DEPTH_ANY = "DEPTH_ANY"
    #: No depth anywhere in the window (webcam, depth off, all holes).
    #: Falls back to the original zone-overlap rule.
    ZONE_OVERLAP = "ZONE_OVERLAP"


@dataclass
class TrackCandidate:
    """A track considered for an RFID tap.

    Carries both signals the decision uses: how strongly the track occupied
    the reader zone, and how close to the camera it came within the window.
    """

    track_id: int
    overlap: float
    bbox: BBox
    depth_m: Optional[float] = None
    depth_valid: bool = False

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "zone_overlap": round(self.overlap, 3),
            "bbox": list(self.bbox),
            "depth_m": (
                round(self.depth_m, 3)
                if self.depth_valid and self.depth_m is not None
                else None
            ),
            "depth_valid": self.depth_valid,
        }


@dataclass
class BindingResolution:
    """Result of trying to attribute one RFID tap to one camera track."""

    status: str
    track_id: Optional[int] = None
    confidence: float = 0.0
    candidates: List[TrackCandidate] = field(default_factory=list)
    reason: str = ""
    selection_rule: str = ""

    @property
    def candidate_track_ids(self) -> List[int]:
        return [candidate.track_id for candidate in self.candidates]

    def to_dict(self) -> dict:
        payload = {
            "status": self.status,
            "track_id": self.track_id,
            "confidence": round(self.confidence, 3),
            "candidate_track_ids": self.candidate_track_ids,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selection_rule": self.selection_rule,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload
