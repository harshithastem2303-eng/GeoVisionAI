"""LEGACY / DEMO ONLY -- do not build on this.

The original GeoVision property matcher: take a GPS point, find properties
within 50 metres by great-circle distance, and confirm a candidate once it
repeats often enough.

**This is not WASTRAQ property identification and must not be treated as
such.** Authoritative association lives in the main WASTRAQ repository and
uses surveyed PostGIS geometry -- entrances, frontages and service-zone
polygons -- not proximity to a vehicle GPS coordinate. Nearest-coordinate
matching is exactly the approach WASTRAQ exists to replace.

It is preserved, isolated and clearly labelled because the existing frontend
still calls ``/latest_detection``. Nothing in the worker-tracking pipeline
depends on it, and it should not be deepened.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from database import db_cursor

logger = logging.getLogger(__name__)

#: Metres. Legacy constant, retained for behavioural compatibility only.
SEARCH_RADIUS_M = 50

#: Consecutive identical sole-candidate observations before "confirmed".
CONFIRMATION_REQUIRED = 5


_HAVERSINE_SQL = """
WITH property_distances AS (
    SELECT
        property_id,
        property_name,
        latitude,
        longitude,
        (
            6371000 * acos(
                LEAST(1, GREATEST(-1,
                    cos(radians(%s)) * cos(radians(latitude))
                    * cos(radians(longitude) - radians(%s))
                    + sin(radians(%s)) * sin(radians(latitude))
                ))
            )
        ) AS distance
    FROM properties
    WHERE latitude IS NOT NULL
      AND longitude IS NOT NULL
)
SELECT property_id, property_name, latitude, longitude, distance
FROM property_distances
WHERE distance <= %s
ORDER BY distance
"""


class LegacyConfirmationTracker:
    """Holds the confirmation streak across requests.

    In the original code these counters were re-initialised inside the
    request handler on every call, so the streak could never exceed 1 and
    ``confirmed`` was unreachable. Keeping the state here at least makes the
    legacy behaviour do what it claimed to.
    """

    def __init__(self, required: int = CONFIRMATION_REQUIRED) -> None:
        self.required = required
        self.last_candidate_id: Optional[int] = None
        self.streak = 0
        self.confirmed_property_id: Optional[int] = None

    def observe(self, candidates: List[dict]) -> dict:
        if len(candidates) == 1:
            current = candidates[0]["property_id"]
            if current == self.last_candidate_id:
                self.streak += 1
            else:
                self.last_candidate_id = current
                self.streak = 1
            if self.streak >= self.required:
                self.confirmed_property_id = current
        else:
            self.last_candidate_id = None
            self.streak = 0
            self.confirmed_property_id = None

        return {
            "confirmed": self.confirmed_property_id is not None,
            "property_id": self.confirmed_property_id,
            "streak": self.streak,
            "required": self.required,
            "note": (
                "LEGACY proximity matching. Not authoritative WASTRAQ "
                "property association."
            ),
        }

    def reset(self) -> None:
        self.last_candidate_id = None
        self.streak = 0
        self.confirmed_property_id = None


legacy_confirmation = LegacyConfirmationTracker()


def nearby_properties(
    latitude: float,
    longitude: float,
    radius_m: int = SEARCH_RADIUS_M,
) -> List[dict]:
    """Properties within ``radius_m`` of a point. Legacy demo query."""

    with db_cursor() as cursor:
        cursor.execute(
            _HAVERSINE_SQL,
            (latitude, longitude, latitude, radius_m),
        )
        rows = cursor.fetchall()

    return [
        {
            "property_id": row[0],
            "property_name": row[1],
            "latitude": row[2],
            "longitude": row[3],
            "distance": round(row[4], 2),
        }
        for row in rows
    ]
