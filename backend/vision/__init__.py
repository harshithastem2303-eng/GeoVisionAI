"""Camera perception and worker identity.

Module map::

    types.py           value objects, stdlib only
    camera.py          frame sources (RealSense, mock)
    detector.py        YOLO person detection + BoT-SORT tracking
    track_history.py   short time-indexed buffer of what was tracked
    rfid_binding.py    RFID tap -> which track tapped, and what the tap meant
    worker_registry.py which tracks are currently authorised workers
    episode_registry.py which bound tracks WASTRAQ says are mid-collection
    depth_position.py  optional camera-relative XYZ from RealSense depth
    pipeline.py        the capture loop that ties them together

The architectural rule the package exists to enforce: YOLO says "person",
RFID says "who", and the camera -- reader zone first, then distance to the
camera -- says "which track". Nobody is a garbage worker for having been
detected first, and once bound, a track is locked rather than re-chosen.

WASTRAQ still owns every property decision. The episode registry here is a
mirror of what WASTRAQ pushes, never a local re-implementation of it.
"""

from .episode_registry import EpisodeRegistry, new_trigger_id

from .depth_position import (
    CameraIntrinsics,
    describe_person_depth,
    estimate_camera_position,
)
from .rfid_binding import (
    RFIDBindingService,
    RFIDEvidenceZone,
    resolve_tap,
    summarize_tracks,
)
from .track_history import TrackHistory
from .types import (
    AssociationStatus,
    BindingResolution,
    BindingStatus,
    CollectionEpisode,
    PersonDetection,
    SelectionRule,
    TapIntent,
    TrackCandidate,
    TrackSnapshot,
    WorkerBinding,
)
from .worker_registry import WorkerRegistry

__all__ = [
    "CameraIntrinsics",
    "describe_person_depth",
    "estimate_camera_position",
    "RFIDBindingService",
    "RFIDEvidenceZone",
    "resolve_tap",
    "summarize_tracks",
    "TrackHistory",
    "EpisodeRegistry",
    "new_trigger_id",
    "AssociationStatus",
    "BindingResolution",
    "BindingStatus",
    "CollectionEpisode",
    "PersonDetection",
    "SelectionRule",
    "TapIntent",
    "TrackCandidate",
    "TrackSnapshot",
    "WorkerBinding",
    "WorkerRegistry",
]
