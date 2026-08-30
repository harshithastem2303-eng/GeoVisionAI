"""The normalised GPS payload WASTRAQ (and anything else) reads.

A *compatibility shape*, not a new sensor. GeoVision has no GNSS receiver and
no IMU; position comes from a phone or laptop browser pushed to
``POST /location``. The fields a real receiver would fill -- altitude, ground
speed, HDOP, satellite count -- are therefore always ``null``.

They are present-and-null rather than omitted on purpose. An absent key looks
like a serialisation bug and invites a consumer to guess; an explicit ``null``
says "this system does not measure that", which is the true statement.

Pure function over a :class:`~location.base.LocationFix`, so it is testable
with no FastAPI and no browser.
"""

from __future__ import annotations

from typing import Optional

# Borrowed rather than re-implemented: the whole point is that a GPS
# timestamp and a track timestamp are directly comparable, which they only
# are if both go through the same formatter. (No cycle: integration does not
# import location.)
from integration.events import iso_utc

from .base import LocationFix


def normalized_gps(
    fix: Optional[LocationFix],
    source_id: str,
    max_age_s: float,
) -> dict:
    """Shape one fix (or the absence of one) into the normalised GPS block.

    ``valid`` is true only for a fix that exists *and* is fresh. A stale fix
    is still returned -- "42 seconds old" is information, and hiding it would
    leave a consumer unable to tell a slow phone from a dead one -- but it is
    not marked valid.
    """

    stale = fix.is_stale(max_age_s) if fix is not None else True

    return {
        "timestamp": iso_utc(fix.timestamp) if fix is not None else iso_utc(),
        "source_id": source_id,
        "latitude": fix.latitude if fix is not None else None,
        "longitude": fix.longitude if fix is not None else None,
        # Not measured by anything in this system.
        "altitude_m": None,
        "speed_mps": None,
        "hdop": None,
        "satellites": None,
        "valid": bool(fix is not None and not stale),
        # What *is* known about the quality of the fix.
        "accuracy_m": fix.accuracy_m if fix is not None else None,
        "age_s": round(fix.age_s, 2) if fix is not None else None,
        "stale": stale,
        "provider": fix.source if fix is not None else None,
        "max_age_s": max_age_s,
        # Stated so no consumer assumes otherwise.
        "heading_deg": None,
        "imu_available": False,
        "gnss_receiver": False,
    }
