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
    evidence_id: str
    event_id: str
    evidence_type: str
    file_path: str
    captured_at: datetime
    verified: bool


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


class CollectionEventWithEvidence(CollectionEventOut):
    evidence: list[EvidenceOut] = []
