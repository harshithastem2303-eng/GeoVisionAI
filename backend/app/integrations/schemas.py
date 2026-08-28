"""The GeoVision -> WASTRAQ event contract, as WASTRAQ receives it.

The authority for these shapes is the sender:
``geovision-darshan/docs/WASTRAQ_INTEGRATION.md`` and the builders in
``geovision-darshan/backend/integration/events.py``. Five event types, one
envelope.

Two validation policies, on purpose
-----------------------------------
*Known fields are strict.* A missing ``event_id``, an unknown
``event_type``, a naive timestamp or an ``RFID_TAP`` that claims a
``track_id`` while reporting ``AMBIGUOUS`` is rejected with 422. Garbage
that reaches the database is far more expensive than garbage refused at
the door.

*Unknown fields are tolerated.* The edge is under active development and
will grow fields; refusing a whole event because it carries one field this
build has not heard of would turn every GeoVision release into a WASTRAQ
outage. Extras survive verbatim in ``geovision_raw_events.payload``.

The one exception is the property-association guard below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter,
                      field_validator, model_validator)

EVENT_TYPES = (
    "TRACK_UPDATE",
    "RFID_TAP",
    "WORKER_TRACK_BOUND",
    "EVIDENCE_READY",
    "HEARTBEAT",
)

BINDING_STATUSES = (
    "BOUND",
    "AMBIGUOUS",
    "NO_TRACK_IN_READER_ZONE",
    "NO_TRACK_DATA",
    "UNKNOWN_RFID",
)

#: Keys that would assert a property association. GeoVision cannot see a
#: service zone, so it has no basis for any of them. The sender strips these
#: before publishing (``events.PROPERTY_FIELDS``); the receiver refuses them
#: as well. Two independent guards, because the failure they prevent -
#: WASTRAQ quietly trusting a camera's guess about which house was served -
#: is the one failure this whole design exists to avoid.
FORBIDDEN_PROPERTY_FIELDS = frozenset(
    {
        "property_id",
        "property_name",
        "properties",
        "property_candidates",
        "authority_property_id",
        "house_number",
        "service_zone_id",
        "zone_id",
        "entrance_id",
        "frontage_id",
        "segregation_status",
        "collection_event_id",
    }
)


def find_property_fields(payload: Any, _path: str = "") -> list[str]:
    """Every path in ``payload`` whose key asserts a property association."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{_path}.{key}" if _path else str(key)
            if key in FORBIDDEN_PROPERTY_FIELDS:
                found.append(path)
            found.extend(find_property_fields(value, path))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(find_property_fields(item, f"{_path}[{index}]"))
    return found


def to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# --- envelope ----------------------------------------------------------------
class GeoVisionEnvelope(BaseModel):
    """The four fields every event carries."""

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime
    source_id: str = Field(..., min_length=1, max_length=128)
    session_id: str | None = Field(None, max_length=128)

    @field_validator("timestamp")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        """Timezone-aware only, normalised to UTC.

        Two machines are involved and their clocks have to be comparable. A
        naive timestamp is a local reading from a laptop in another room; it
        is not a fact anyone can order against ours, so it is refused rather
        than assumed to be UTC.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "timestamp must be timezone-aware ISO-8601 UTC, e.g. "
                "2026-08-28T07:10:12.341Z"
            )
        return to_utc(value)

    @model_validator(mode="before")
    @classmethod
    def _refuse_property_association(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        offending = find_property_fields(data)
        if offending:
            raise ValueError(
                "GeoVision does not determine the serviced property; "
                f"refusing event carrying {', '.join(sorted(offending))}"
            )
        return data


# --- TRACK_UPDATE ------------------------------------------------------------
class BBox(BaseModel):
    """Detector box in pixels. No sign or ordering constraint: a clipped box
    at the frame edge is a real observation, not a malformed event."""

    model_config = ConfigDict(extra="allow")

    x1: int
    y1: int
    x2: int
    y2: int


class GpsFix(BaseModel):
    """The coarse phone fix that happened to be current.

    Its own object, never flattened into the observation. A camera depth
    reading and a browser geolocation fix measure different things with
    different error models; merging them into one "position" is how an 8 m
    fix ends up deciding which house was served.
    """

    model_config = ConfigDict(extra="allow")

    timestamp: datetime | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)
    source: str | None = None
    age_s: float | None = None
    stale: bool | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    hdop: float | None = None
    satellites: int | None = None
    heading_deg: float | None = None


class TrackUpdateEvent(GeoVisionEnvelope):
    """One tracked person, at one instant, in the camera's own frame.

    Camera-relative metres, RealSense convention: x right, y down, z forward.
    Deliberately absent: any world position, any property, any judgement that
    a collection happened.
    """

    event_type: Literal["TRACK_UPDATE"]

    track_id: int
    confidence: float | None = None
    bbox: BBox | None = None

    depth_m: float | None = None
    camera_x_m: float | None = None
    camera_y_m: float | None = None
    camera_z_m: float | None = None
    relative_x_m: float | None = None
    relative_forward_m: float | None = None
    depth_valid: bool = False
    depth_status: str | None = None

    is_authorized_picker: bool = False
    collector_id: str | None = None
    identity_confidence: float | None = None

    gps: GpsFix | None = None


# --- RFID_TAP ----------------------------------------------------------------
class RfidTapEvent(GeoVisionEnvelope):
    """One RFID tap: WHO and WHEN. Never WHERE."""

    event_type: Literal["RFID_TAP"]

    rfid_uid: str = Field(..., min_length=1, max_length=64)
    collector_id: str | None = None
    track_id: int | None = None
    binding_status: Literal[
        "BOUND",
        "AMBIGUOUS",
        "NO_TRACK_IN_READER_ZONE",
        "NO_TRACK_DATA",
        "UNKNOWN_RFID",
    ]
    binding_confidence: float | None = Field(None, ge=0, le=1)
    candidate_track_ids: list[int] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _track_only_when_bound(self) -> "RfidTapEvent":
        """``track_id`` is null unless the status is ``BOUND``.

        The contract states it, and it is the ambiguity rule in one line: if
        two people were at the reader, the tap belongs to a set, not to a
        person. An event that reports AMBIGUOUS *and* names a track has
        already resolved the ambiguity by guessing somewhere upstream, so it
        is refused rather than stored as fact.
        """
        if self.binding_status != "BOUND" and self.track_id is not None:
            raise ValueError(
                f"track_id must be null when binding_status is "
                f"{self.binding_status!r}; only BOUND may name a track"
            )
        if self.binding_status == "BOUND" and self.track_id is None:
            raise ValueError("binding_status BOUND requires a track_id")
        return self


# --- WORKER_TRACK_BOUND ------------------------------------------------------
class WorkerTrackBoundEvent(GeoVisionEnvelope):
    """A collector is now associated with a camera track for this session."""

    event_type: Literal["WORKER_TRACK_BOUND"]

    collector_id: str = Field(..., min_length=1, max_length=64)
    rfid_uid: str | None = Field(None, max_length=64)
    track_id: int
    confidence: float | None = Field(None, ge=0, le=1)
    rfid_event_id: str | None = Field(None, max_length=128)


# --- EVIDENCE_READY ----------------------------------------------------------
class EvidenceReadyEvent(GeoVisionEnvelope):
    """A clip exists on the GeoVision machine. A reference, never the bytes."""

    event_type: Literal["EVIDENCE_READY"]

    clip_id: str = Field(..., min_length=1, max_length=128)
    file_path: str = Field(..., min_length=1, max_length=1024)
    start_time: datetime | None = None
    end_time: datetime | None = None
    frame_count: int | None = Field(None, ge=0)
    track_id: int | None = None
    rfid_event_id: str | None = Field(None, max_length=128)

    @field_validator("start_time", "end_time")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clip times must be timezone-aware ISO-8601 UTC")
        return to_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> "EvidenceReadyEvent":
        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time is before start_time")
        return self


# --- HEARTBEAT ---------------------------------------------------------------
class HeartbeatEvent(GeoVisionEnvelope):
    """Liveness. Lets the dashboard tell "no pickers" from "no GeoVision"."""

    event_type: Literal["HEARTBEAT"]

    status: dict[str, Any] = Field(default_factory=dict)


GeoVisionEvent = Annotated[
    Union[
        TrackUpdateEvent,
        RfidTapEvent,
        WorkerTrackBoundEvent,
        EvidenceReadyEvent,
        HeartbeatEvent,
    ],
    Field(discriminator="event_type"),
]

#: Validate an arbitrary dict into exactly one of the five events.
EVENT_ADAPTER: TypeAdapter[GeoVisionEvent] = TypeAdapter(GeoVisionEvent)


# --- responses ---------------------------------------------------------------
class IngestAck(BaseModel):
    """Deliberately tiny. The sender's timeout is 2 s and it only reads the
    HTTP status; anything larger is bytes across site wifi for nothing."""

    status: Literal["ACCEPTED", "DUPLICATE"]
    event_id: str
    event_type: str
    duplicate: bool
    received_at: datetime
