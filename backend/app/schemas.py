"""Pydantic request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- GIS lookup --------------------------------------------------------------
class LookupRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90, examples=[12.9700600])
    longitude: float = Field(..., ge=-180, le=180, examples=[77.5902765])
    search_radius_m: float | None = Field(
        None, gt=0, le=200, description="Override the default ST_DWithin radius."
    )


class CandidateOut(BaseModel):
    property_id: str
    zone_id: str
    distance_m: float
    inside: bool
    entrance_distance_m: float | None = None


class LookupResponse(BaseModel):
    property_id: str | None
    decision: Literal["AUTO_ASSOCIATED", "AMBIGUOUS", "NO_MATCH"]
    confidence: float
    method: str
    reason: str
    query: dict[str, Any]
    candidates: list[CandidateOut]


# --- properties --------------------------------------------------------------
class PropertyOut(BaseModel):
    property_id: str
    authority_property_id: str | None = None
    house_number: str | None = None
    owner_name: str | None = None
    formatted_address: str | None = None
    property_type: str
    route_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    mapping_confidence: float | None = None
    verification_status: str
    created_at: datetime


class PropertyDetailOut(PropertyOut):
    zone_id: str | None = None
    zone_version: int | None = None
    road_side: str | None = None
    service_zone_area_m2: float | None = None
    geometry_source: str | None = None
    geometry_verified: bool | None = None
    frontage_photo_id: str | None = None
    frontage_photo_path: str | None = None
    entrance_geojson: dict | None = None
    frontage_geojson: dict | None = None
    service_zone_geojson: dict | None = None


# --- collection events -------------------------------------------------------
class CollectionEventCreate(BaseModel):
    property_id: str | None = Field(
        None, description="Provide this OR latitude/longitude for GIS association."
    )
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    picker_id: str | None = None
    track_id: str | None = None
    collected: bool = True
    # Normal collection is SEGREGATED; the picker only acts on the exception.
    segregation_status: Literal["SEGREGATED", "NOT_SEGREGATED"] = "SEGREGATED"
    association_confidence: float | None = Field(None, ge=0, le=1)
    collection_time: datetime | None = None


class NonSegregationRequest(BaseModel):
    picker_id: str | None = None
    rfid_uid: str | None = Field(None, description="RFID tag that raised the exception.")
    note: str | None = None
    create_evidence: bool = True
    # Save the ~15 s of camera footage around the decision as an MP4 and
    # link it as a VIDEO_CLIP evidence row. Silently skipped when the
    # vision pipeline is not running.
    capture_video: bool = True
    evidence_type: Literal[
        "NON_SEGREGATION_PROOF", "CAMERA_FRAME", "VIDEO_CLIP", "COLLECTION_PROOF"
    ] = "NON_SEGREGATION_PROOF"
    file_path: str | None = Field(None, description="Omit to auto-generate a demo path.")


class EvidenceCreate(BaseModel):
    evidence_type: Literal[
        "COLLECTION_PROOF", "NON_SEGREGATION_PROOF", "VIDEO_CLIP", "CAMERA_FRAME"
    ]
    file_path: str | None = None
    captured_at: datetime | None = None
    verified: bool = False


class EvidenceOut(BaseModel):
    """One evidence record, plus whether anything can actually be played.

    ``file_path`` is kept for continuity and for the audit trail, but it is
    NOT a promise that a browser can open it: for a GeoVision clip it is a
    ``geovision://<source>/<clip>`` URI, and for the demo seed rows it is a
    placeholder that was never a file. The four media fields below are the
    ones the UI acts on, and ``media_url`` is non-null only when this Mac
    holds the bytes right now.
    """

    evidence_id: str
    event_id: str
    evidence_type: str
    file_path: str
    captured_at: datetime
    verified: bool

    # --- playability (STEP 4A) -------------------------------------------
    #: AVAILABLE  bytes are on this Mac and media_url will serve them
    #: PENDING    announced by the edge, not pulled across yet
    #: UNAVAILABLE the edge was asked and could not deliver - retryable
    #: NONE       this record has no playable artefact (demo placeholder)
    media_status: str = "NONE"
    media_kind: str = "none"          # video | image | none
    media_url: str | None = None      # always a WASTRAQ path, never the edge
    media_bytes: int | None = None
    media_content_type: str | None = None
    #: Where the artefact came from. A Windows path here is correct and
    #: expected - it is provenance for the audit trail, and it is never a
    #: link. STEP 4C stopped rendering it: the dashboard shows
    #: `source_label` instead, so no operator screen carries a path from
    #: another machine.
    source_ref: str | None = None
    #: Operator-facing provenance, built from identifiers only:
    #: "GeoVision GEOVISION-D455-01 · CLIP-3f2a1b" / "WASTRAQ capture ·
    #: EVENT-001.mp4" / "Demo placeholder ...". Never contains a path.
    source_label: str | None = None
    #: GEOVISION_EDGE | LOCAL_CAPTURE | PLACEHOLDER
    source_kind: str | None = None
    #: True for a demo seed row whose file_path was never a file. The
    #: dashboard counts these separately so an evidence action never
    #: promises footage that does not exist.
    is_placeholder: bool = False
    fetch_status: str | None = None
    fetch_error: str | None = None
    clip_event_id: str | None = None

    # --- clip metadata (STEP 4C) -----------------------------------------
    #: When the footage itself starts and ends - which is the question an
    #: operator actually asks of a clip, and the one a file path cannot
    #: answer. Null for anything that is not an edge clip.
    clip_id: str | None = None
    clip_source_id: str | None = None
    clip_start: datetime | None = None
    clip_end: datetime | None = None
    clip_seconds: float | None = None
    frame_count: int | None = None
    clip_track_id: int | None = None


class CollectionEventOut(BaseModel):
    event_id: str
    property_id: str
    picker_id: str | None = None
    track_id: str | None = None
    collected: bool
    segregation_status: str
    association_confidence: float | None = None
    collection_time: datetime
    rfid_triggered: bool
    review_status: str
    # Set when the event was produced by the episode engine (a bound
    # collector dwelling in a mapped service zone) rather than by a manual
    # POST. Null on hand-created events, and that difference is worth being
    # able to see in the API rather than only in the table.
    episode_id: str | None = None


class CollectionEventWithEvidence(CollectionEventOut):
    evidence: list[EvidenceOut] = []
    # STEP 4C. Denormalised onto the single-event read so the evidence
    # modal can label the footage - property, collector, time - from one
    # response. Null when the event has no picker bound, which is a real
    # state and reads as "unassigned" rather than as a missing field.
    picker_name: str | None = None
    owner_name: str | None = None
    house_number: str | None = None
