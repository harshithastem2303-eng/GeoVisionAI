"""Phase 3 - PostGIS property association.

CORE RULE
---------
A property is never chosen because it is the "nearest GPS point". Association
is decided against mapped service-zone polygons, and the engine refuses to
guess: if the evidence is ambiguous it says so.

Decision ladder
---------------
1. Build the picker position as a real geometry:
       ST_SetSRID(ST_MakePoint(lon, lat), 4326)
2. Containment first:  ST_Within / ST_Intersects against service zones.
   - exactly one containing zone  -> AUTO_ASSOCIATED (confidence 0.95+)
   - more than one                -> AMBIGUOUS (overlapping zones = mapping bug)
3. No containment -> proximity search with ST_DWithin over a metre radius.
   - nothing within the radius    -> NO_MATCH
   - nearest zone close enough AND clearly closer than the runner-up
                                  -> AUTO_ASSOCIATED (reduced confidence)
   - otherwise                    -> AMBIGUOUS, all candidates returned

CRS handling
------------
Degrees are not metres. Every distance here is computed by casting the 4326
geometry to `geography`, which measures on the spheroid in metres. The
equivalent projected form (ST_Transform to UTM 43N / EPSG:32643) is given in
`database/lookup_function.sql` for reference - both are correct, geography
is used because it stays valid anywhere in the country without having to
pick the right UTM zone first.
"""

from typing import Any

from .config import settings
from .database import fetch_all, fetch_one
from .tracing import traceable

# ---------------------------------------------------------------------------
# 1 + 2: containment
# ---------------------------------------------------------------------------
SQL_CONTAINING = """
WITH pt AS (
    SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS geom
)
SELECT
    z.property_id,
    z.zone_id,
    0.0::double precision AS distance_m,
    TRUE                  AS inside,
    -- how far inside the zone the point sits (metres from the zone boundary)
    ST_Distance(pt.geom::geography, ST_Boundary(z.geometry)::geography) AS margin_m,
    -- distance to the mapped entrance, useful as a secondary confidence signal
    (SELECT ST_Distance(pt.geom::geography, e.geometry::geography)
       FROM property_entrances e
      WHERE e.property_id = z.property_id
      LIMIT 1) AS entrance_distance_m
FROM property_service_zones z, pt
WHERE ST_Within(pt.geom, z.geometry)
ORDER BY z.property_id;
"""

# ---------------------------------------------------------------------------
# 3: proximity fallback
# ---------------------------------------------------------------------------
SQL_NEARBY = """
WITH pt AS (
    SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS geom
)
SELECT
    z.property_id,
    z.zone_id,
    ST_Distance(pt.geom::geography, z.geometry::geography) AS distance_m,
    FALSE AS inside,
    NULL::double precision AS margin_m,
    (SELECT ST_Distance(pt.geom::geography, e.geometry::geography)
       FROM property_entrances e
      WHERE e.property_id = z.property_id
      LIMIT 1) AS entrance_distance_m
FROM property_service_zones z, pt
WHERE ST_DWithin(pt.geom::geography, z.geometry::geography, %(radius_m)s)
ORDER BY distance_m ASC
LIMIT 10;
"""

SQL_PROPERTY = """
SELECT p.*,
       ST_AsGeoJSON(e.geometry)::json AS entrance_geojson,
       ST_AsGeoJSON(f.geometry)::json AS frontage_geojson,
       ST_AsGeoJSON(z.geometry)::json AS service_zone_geojson,
       f.road_side,
       z.zone_id,
       z.version AS zone_version,
       z.source  AS geometry_source,
       z.verified AS geometry_verified,
       ROUND(ST_Area(z.geometry::geography)::numeric, 2) AS service_zone_area_m2,
       ph.file_path AS frontage_photo_path,
       ph.photo_id  AS frontage_photo_id,
       p.admin_unit_id
FROM properties p
LEFT JOIN property_entrances     e ON e.property_id = p.property_id
LEFT JOIN property_frontages     f ON f.property_id = p.property_id
LEFT JOIN property_service_zones z ON z.property_id = p.property_id
-- LATERAL, not a plain join: a property can now carry several frontage
-- photos (one per survey), and a plain join would multiply the row.
LEFT JOIN LATERAL (
    SELECT file_path, photo_id FROM property_photos
     WHERE property_id = p.property_id AND photo_type = 'FRONTAGE'
     ORDER BY captured_at DESC NULLS LAST, photo_id DESC
     LIMIT 1
) ph ON TRUE
WHERE p.property_id = %(property_id)s;
"""


def _round(v: Any, n: int = 3) -> Any:
    return round(float(v), n) if v is not None else None


def _as_candidate(row: dict) -> dict:
    return {
        "property_id": row["property_id"],
        "zone_id": row["zone_id"],
        "distance_m": _round(row["distance_m"], 3),
        "inside": bool(row["inside"]),
        "entrance_distance_m": _round(row.get("entrance_distance_m"), 3),
    }


@traceable(run_type="chain", name="gis.lookup_property")
def lookup_property(lat: float, lon: float, radius_m: float | None = None) -> dict:
    """Associate a picker coordinate with a property. Never guesses silently.

    Traced (when tracing is on) because this is THE decision the whole system
    exists to defend. The span records the coordinate that went in and the
    full result that came out - decision, confidence, method, the stated
    reason, and every candidate zone that was considered including the ones
    that lost. A refusal is as much of a result as an association, and both
    land in the same place.
    """
    radius = radius_m if radius_m is not None else settings.SEARCH_RADIUS_M
    params = {"lat": lat, "lon": lon, "radius_m": radius}

    inside_rows = fetch_all(SQL_CONTAINING, params)

    # --- case A: inside exactly one service zone ---------------------------
    if len(inside_rows) == 1:
        row = inside_rows[0]
        # Sitting well inside the polygon is stronger evidence than clipping
        # its edge. margin_m is the distance from the zone boundary.
        margin = float(row["margin_m"] or 0.0)
        confidence = 0.95 + min(margin / 20.0, 0.04)  # 0.95 .. 0.99
        return _result(
            decision="AUTO_ASSOCIATED",
            property_id=row["property_id"],
            confidence=round(min(confidence, 0.99), 3),
            candidates=[_as_candidate(row)],
            method="ST_WITHIN_SERVICE_ZONE",
            reason="Point lies inside exactly one mapped service zone.",
            lat=lat,
            lon=lon,
            radius=radius,
        )

    # --- case B: inside several zones (overlapping mapping) ----------------
    if len(inside_rows) > 1:
        return _result(
            decision="AMBIGUOUS",
            property_id=None,
            confidence=0.0,
            candidates=[_as_candidate(r) for r in inside_rows],
            method="ST_WITHIN_SERVICE_ZONE",
            reason=(
                f"Point lies inside {len(inside_rows)} overlapping service zones. "
                "Service-zone mapping needs correction in QGIS."
            ),
            lat=lat,
            lon=lon,
            radius=radius,
        )

    # --- case C: outside every zone -> proximity ---------------------------
    near_rows = fetch_all(SQL_NEARBY, params)

    if not near_rows:
        return _result(
            decision="NO_MATCH",
            property_id=None,
            confidence=0.0,
            candidates=[],
            method="ST_DWITHIN",
            reason=f"No service zone within {radius:g} m of the position.",
            lat=lat,
            lon=lon,
            radius=radius,
        )

    candidates = [_as_candidate(r) for r in near_rows]
    d1 = float(near_rows[0]["distance_m"])
    d2 = float(near_rows[1]["distance_m"]) if len(near_rows) > 1 else None
    margin = (d2 - d1) if d2 is not None else float("inf")

    # Distance decay: at 0 m -> 0.90, at AUTO_MAX_DISTANCE_M -> 0.75.
    # Kept above MIN_AUTO_CONFIDENCE across the whole auto range so the two
    # thresholds don't silently contradict each other.
    proximity_score = max(0.0, 0.90 - (d1 / max(settings.AUTO_MAX_DISTANCE_M, 0.001)) * 0.15)
    # Separation bonus: a clear winner is worth more than a photo finish
    separation = min(margin / max(settings.AMBIGUITY_MARGIN_M, 0.001), 1.0) if margin != float("inf") else 1.0
    confidence = round(proximity_score * (0.6 + 0.4 * separation), 3)

    too_far = d1 > settings.AUTO_MAX_DISTANCE_M
    too_close_to_call = margin < settings.AMBIGUITY_MARGIN_M
    low_confidence = confidence < settings.MIN_AUTO_CONFIDENCE

    if too_far or too_close_to_call or low_confidence:
        reasons = []
        if too_far:
            reasons.append(
                f"nearest zone is {d1:.2f} m away (auto limit {settings.AUTO_MAX_DISTANCE_M:g} m)"
            )
        if too_close_to_call:
            reasons.append(
                f"runner-up only {margin:.2f} m further (need {settings.AMBIGUITY_MARGIN_M:g} m separation)"
            )
        if low_confidence and not (too_far or too_close_to_call):
            reasons.append(f"confidence {confidence} below {settings.MIN_AUTO_CONFIDENCE}")
        return _result(
            decision="AMBIGUOUS",
            property_id=None,
            confidence=confidence,
            candidates=candidates,
            method="ST_DWITHIN",
            reason="Not auto-associated: " + "; ".join(reasons) + ".",
            lat=lat,
            lon=lon,
            radius=radius,
        )

    return _result(
        decision="AUTO_ASSOCIATED",
        property_id=near_rows[0]["property_id"],
        confidence=confidence,
        candidates=candidates,
        method="ST_DWITHIN",
        reason=(
            f"Outside all zones but {d1:.2f} m from a single clear nearest zone "
            f"({margin:.2f} m clear of the runner-up)."
        ),
        lat=lat,
        lon=lon,
        radius=radius,
    )


def _result(*, decision, property_id, confidence, candidates, method, reason, lat, lon, radius) -> dict:
    return {
        "property_id": property_id,
        "decision": decision,
        "confidence": confidence,
        "method": method,
        "reason": reason,
        "query": {"latitude": lat, "longitude": lon, "search_radius_m": radius},
        "candidates": candidates,
    }


def get_property_with_gis(property_id: str) -> dict | None:
    return fetch_one(SQL_PROPERTY, {"property_id": property_id})
