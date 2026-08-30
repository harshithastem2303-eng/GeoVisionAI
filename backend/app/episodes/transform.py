"""Fixed-camera frame -> WGS84.

GeoVision reports a person in CAMERA metres: ``relative_x_m`` to the right of
the optical axis, ``relative_forward_m`` along it. Those numbers mean nothing
on a map until they are anchored to where the camera actually is and which
way it points.

This module is that anchor, and nothing else. It is deliberately pure
stdlib - no config import, no database, no pydantic - so the arithmetic can
be tested on its own and so a mistake here can never be hidden behind a
mocked service.

Convention
----------
``heading_deg`` is the compass bearing of the camera's FORWARD (+z) axis:
degrees clockwise from true north. 0 = camera looks north, 90 = east.

With that heading, in the local east/north tangent plane:

    forward unit vector = ( sin H,  cos H )
    right   unit vector = ( cos H, -sin H )      (forward rotated +90 deg)

so

    east  = forward_m * sin H + right_m * cos H
    north = forward_m * cos H - right_m * sin H

Accuracy
--------
A local flat-earth (equirectangular) approximation about the camera origin.
Over the tens of metres a fixed camera can actually see, the error against a
proper geodesic is well under a centimetre - far below the depth sensor's own
error, and far below the width of a service zone. It is NOT suitable for
kilometre-scale work, and is not used for any.

This produces a POSITION. It does not produce a property: the position is
handed to the PostGIS service-zone ladder, which decides, and may refuse.
"""

from __future__ import annotations

import math
from typing import NamedTuple

#: Metres per degree of latitude. WGS84 mean; the variation with latitude is
#: ~0.6 % pole to equator and irrelevant at this range.
_M_PER_DEG_LAT = 111_320.0


class CameraOrigin(NamedTuple):
    """Where the camera is and which way it looks."""

    latitude: float
    longitude: float
    heading_deg: float
    #: Optional lever arm from the survey mark to the optical centre, in the
    #: camera's own frame (right, forward). Usually zero.
    offset_right_m: float = 0.0
    offset_forward_m: float = 0.0

    @property
    def configured(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def offset_to_wgs84(
    origin_lat: float,
    origin_lon: float,
    east_m: float,
    north_m: float,
) -> tuple[float, float]:
    """Move (east, north) metres from a lat/lon. Returns (lat, lon)."""
    lat = origin_lat + north_m / _M_PER_DEG_LAT
    # Longitude degrees shrink with latitude. Evaluated at the MIDPOINT
    # latitude rather than the origin: over a few tens of metres the
    # difference is microscopic, but it costs nothing and keeps the function
    # symmetric (going out and coming back lands where you started).
    mid = math.radians((origin_lat + lat) / 2.0)
    metres_per_deg_lon = _M_PER_DEG_LAT * math.cos(mid)
    if abs(metres_per_deg_lon) < 1e-9:  # a pole; not a demo lane
        return lat, origin_lon
    lon = origin_lon + east_m / metres_per_deg_lon
    return lat, lon


def camera_to_east_north(
    right_m: float, forward_m: float, heading_deg: float
) -> tuple[float, float]:
    """Rotate a camera-frame offset into the local east/north plane."""
    h = math.radians(heading_deg)
    sin_h, cos_h = math.sin(h), math.cos(h)
    east = forward_m * sin_h + right_m * cos_h
    north = forward_m * cos_h - right_m * sin_h
    return east, north


def camera_to_wgs84(
    origin: CameraOrigin, right_m: float, forward_m: float
) -> tuple[float, float]:
    """A camera-frame observation as a map position. Returns (lat, lon)."""
    east, north = camera_to_east_north(
        right_m + origin.offset_right_m,
        forward_m + origin.offset_forward_m,
        origin.heading_deg,
    )
    return offset_to_wgs84(origin.latitude, origin.longitude, east, north)


def wgs84_to_camera(
    origin: CameraOrigin, lat: float, lon: float
) -> tuple[float, float]:
    """Inverse of :func:`camera_to_wgs84`. Returns (right_m, forward_m).

    Exists so a surveyed property coordinate can be expressed in the camera's
    own numbers - which is how you check a heading by eye instead of by
    faith, and how the tests assert the round trip.
    """
    north = (lat - origin.latitude) * _M_PER_DEG_LAT
    mid = math.radians((origin.latitude + lat) / 2.0)
    east = (lon - origin.longitude) * _M_PER_DEG_LAT * math.cos(mid)

    h = math.radians(origin.heading_deg)
    sin_h, cos_h = math.sin(h), math.cos(h)
    # Inverse rotation (the matrix is orthonormal, so transpose == inverse).
    forward = east * sin_h + north * cos_h
    right = east * cos_h - north * sin_h
    return right - origin.offset_right_m, forward - origin.offset_forward_m
