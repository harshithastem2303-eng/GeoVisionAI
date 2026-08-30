"""Request bodies for the upgraded endpoints.

The preserved collector / RFID CRUD endpoints keep their original
query-parameter signatures because the existing dashboard calls them that
way; only the new subsystems take JSON.
"""

from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field, model_validator


class RFIDEventIn(BaseModel):
    """One RFID tap, as reported by the reader bridge.

    Accepts either ``rfid_id`` (what the dashboard and the ``rfids`` table
    call it) or ``rfid_uid`` (what the WASTRAQ event schema calls it). They
    are the same value; carrying both spellings avoids forcing a rename on
    either side of an integration that is already in the field.
    """

    rfid_id: Optional[str] = Field(default=None, description="Tag UID as read")
    rfid_uid: Optional[str] = Field(default=None, description="Alias of rfid_id")
    timestamp: Optional[Union[str, float]] = Field(
        default=None,
        description=(
            "When the tap happened: ISO-8601, epoch seconds or epoch "
            "milliseconds. Defaults to now."
        ),
    )

    @model_validator(mode="after")
    def _require_one_uid(self) -> "RFIDEventIn":
        value = (self.rfid_id or self.rfid_uid or "").strip()
        if not value:
            raise ValueError("one of rfid_id / rfid_uid is required")
        self.rfid_id = value
        self.rfid_uid = value
        return self

    @property
    def uid(self) -> str:
        return (self.rfid_id or self.rfid_uid or "").strip()


class EpisodeIn(BaseModel):
    """An open collection episode, pushed here by WASTRAQ.

    Deliberately property-free, and the omission is the point: WASTRAQ knows
    which house track 35 is standing at, GeoVision only needs to know *that*
    it is standing at one, so a second RFID tap has a subject. Any
    property-naming field that arrives anyway is dropped by
    ``EpisodeRegistry.sanitize`` before storage.

    ``association_status`` carries WASTRAQ's own confidence wording. Only
    ``AUTO_ASSOCIATED`` and ``REVIEW`` may be flagged by a re-tap; anything
    else is reported back as ``EPISODE_NOT_ACTIONABLE`` rather than acted on.
    """

    episode_id: str = Field(description="WASTRAQ's opaque episode handle")
    track_id: int = Field(description="The camera track servicing the property")
    association_status: str = Field(
        default="AUTO_ASSOCIATED",
        description="AUTO_ASSOCIATED or REVIEW",
    )
    collector_id: Optional[str] = Field(
        default=None,
        description="Optional cross-check against the locked binding",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "GeoVision vision session the track id belongs to. Rejected if "
            "it names a session that has already ended."
        ),
    )
    opened_at: Optional[Union[str, float]] = None


class LocationIn(BaseModel):
    """A position pushed from a phone or laptop browser."""

    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    timestamp: Optional[Union[str, float]] = None
    source: str = Field(default="PHONE", description="PHONE or LAPTOP")


class EvidenceRequestIn(BaseModel):
    """Ask for an evidence clip around a moment.

    ``timestamp`` defaults to now. ``track_id`` and ``rfid_event_id`` are
    carried through to the resulting EVIDENCE_READY event so WASTRAQ can
    join the clip to whatever prompted it -- GeoVision does not decide what
    the clip means.
    """

    timestamp: Optional[Union[str, float]] = None
    track_id: Optional[int] = None
    rfid_event_id: Optional[str] = None
    episode_id: Optional[str] = Field(
        default=None,
        description=(
            "WASTRAQ's episode handle, if this capture belongs to one. "
            "Carried through to EVIDENCE_READY; never invented here."
        ),
    )
    reason: Optional[str] = None
