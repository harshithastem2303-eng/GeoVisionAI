"""LEGACY / DEMO-ONLY endpoints.

``/frames``, ``/latest_detection`` and ``/detection/not-segregated`` are the
original proximity-based demo flow. They are kept because the existing
dashboard calls them, and quarantined here so it is obvious that nothing in
the worker-tracking pipeline depends on them.

Authoritative property association is WASTRAQ's, using surveyed PostGIS
geometry. Do not extend these.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from database import DatabaseUnavailable, db_cursor
from legacy_property_match import (
    SEARCH_RADIUS_M,
    legacy_confirmation,
    nearby_properties,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["legacy"])

_LEGACY_NOTE = (
    "LEGACY demo endpoint. Proximity matching is not authoritative WASTRAQ "
    "property association."
)


@router.get("/frames")
def get_frames():
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT f.frame_id, f.image_name, f.image_path, f.capture_time,
                       f.latitude, f.longitude, f.altitude, f.processed,
                       COALESCE(d.status, 'Segregated') AS segregation_status
                FROM camera_frames f
                LEFT JOIN LATERAL (
                    SELECT status, confidence
                    FROM detections
                    WHERE frame_id = f.frame_id
                    ORDER BY confidence DESC
                    LIMIT 1
                ) d ON TRUE
                ORDER BY f.frame_id DESC
                """
            )
            rows = cursor.fetchall()
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("FRAMES ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [
        {
            "frame_id": row[0],
            "image_name": row[1],
            "image_path": row[2],
            "capture_time": str(row[3]),
            "latitude": row[4],
            "longitude": row[5],
            "altitude": row[6],
            "processed": row[7],
            "segregation_status": row[8],
        }
        for row in rows
    ]


@router.get("/latest_detection")
def latest_detection():
    """Latest stored frame plus nearby property candidates. Legacy."""

    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT frame_id, image_name, image_path, capture_time,
                       latitude, longitude, altitude
                FROM camera_frames
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY frame_id DESC
                LIMIT 1
                """
            )
            frame = cursor.fetchone()
            if frame is None:
                return {}

            (
                frame_id,
                image_name,
                image_path,
                capture_time,
                latitude,
                longitude,
                altitude,
            ) = frame

            cursor.execute(
                """
                SELECT latitude, longitude
                FROM camera_frames
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY frame_id DESC
                LIMIT 5
                """
            )
            readings = cursor.fetchall()

        if readings:
            latitudes = sorted(row[0] for row in readings)
            longitudes = sorted(row[1] for row in readings)
            latitude = latitudes[len(latitudes) // 2]
            longitude = longitudes[len(longitudes) // 2]

        candidates = nearby_properties(latitude, longitude)
        confirmation = legacy_confirmation.observe(candidates)

    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("LATEST DETECTION ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "frame_id": frame_id,
        "image_name": image_name,
        "image_path": image_path,
        "capture_time": str(capture_time),
        "gps": {
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude,
            "smoothed": True,
            "samples": len(readings),
        },
        "building_detected": True,
        "search_radius": SEARCH_RADIUS_M,
        "candidates": candidates,
        "confirmation": confirmation,
        "note": _LEGACY_NOTE,
    }


@router.post("/detection/not-segregated")
def mark_not_segregated(frame_id: int, property_id: int):
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE detections
                SET status = 'Not Segregated'
                WHERE frame_id = %s AND property_id = %s
                RETURNING frame_id, property_id, status
                """,
                (frame_id, property_id),
            )
            updated = cursor.fetchone()
            if updated is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No detection found for frame {frame_id} "
                        f"and property {property_id}"
                    ),
                )
    except HTTPException:
        raise
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("STATUS UPDATE ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "success": True,
        "frame_id": updated[0],
        "property_id": updated[1],
        "status": updated[2],
    }
