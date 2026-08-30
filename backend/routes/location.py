"""Phone / laptop location ingestion, and the normalised GPS view of it.

A browser -- on a phone or on either laptop -- calls
``navigator.geolocation.watchPosition`` and POSTs each fix here. No native
app, no serial device, no OS-specific location call, so the same path works
on Windows and macOS.

Nothing in this module turns a position into a property. That conversion
needs surveyed service-zone geometry, which lives in WASTRAQ.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

import config
from location import InvalidLocation, normalized_gps
from schemas import LocationIn
from services import location_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["location"])


@router.post("/location")
def submit_location(payload: LocationIn):
    """Store one pushed fix. Rejects implausible or too-inaccurate values."""

    try:
        fix = location_service.submit(
            latitude=payload.latitude,
            longitude=payload.longitude,
            accuracy_m=payload.accuracy_m,
            source=payload.source,
            timestamp=payload.timestamp,
        )
    except InvalidLocation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"accepted": True, "fix": fix.to_dict(location_service.max_age_s)}


@router.get("/location")
def current_location():
    """Best available fix, with its source, accuracy and staleness.

    ``heading_deg`` is always ``null`` and ``imu_available`` always false.
    There is no IMU and no dedicated GNSS receiver in this system; a heading
    would be fabricated.
    """

    fix = location_service.current_fix()
    return {
        "available": fix is not None,
        "fix": fix.to_dict(location_service.max_age_s) if fix else None,
        "service": location_service.describe(),
    }


@router.get("/gps")
def gps():
    """The same position in the normalised GPS shape WASTRAQ expects.

    ``altitude_m``, ``speed_mps``, ``hdop`` and ``satellites`` are always
    ``null``: there is no GNSS receiver here to measure them. They are
    present-and-null rather than missing so the absence is a statement
    instead of looking like a parse failure.
    """

    return normalized_gps(
        fix=location_service.current_fix(),
        source_id=config.GPS_SOURCE_ID,
        max_age_s=location_service.max_age_s,
    )
