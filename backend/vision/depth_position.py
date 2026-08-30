"""Camera-relative XYZ for a detected person, from RealSense depth.

Optional by design. Depth is a *bonus* signal: if the camera is a plain
webcam, if the depth stream is disabled, or if the pixels under a person are
holes, every function here returns ``None`` and the rest of the application
carries on unchanged.

Intrinsics are represented by a small local dataclass rather than an
``rs.intrinsics`` object so this module -- and its tests -- import with
pyrealsense2 absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraIntrinsics:
    """Pinhole intrinsics, in pixels."""

    width: int
    height: int
    fx: float
    fy: float
    ppx: float
    ppy: float

    @classmethod
    def from_realsense(cls, intrinsics) -> "CameraIntrinsics":
        """Adapt an ``rs.intrinsics``. Kept here so callers never import rs."""

        return cls(
            width=int(intrinsics.width),
            height=int(intrinsics.height),
            fx=float(intrinsics.fx),
            fy=float(intrinsics.fy),
            ppx=float(intrinsics.ppx),
            ppy=float(intrinsics.ppy),
        )

    def is_valid(self) -> bool:
        return self.fx > 0 and self.fy > 0

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "fx": self.fx,
            "fy": self.fy,
            "ppx": self.ppx,
            "ppy": self.ppy,
        }


def representative_point(bbox: Sequence[int]) -> Tuple[float, float]:
    """Bottom-centre of a box: ``u = (x1+x2)/2``, ``v = y2``.

    Where the person meets the ground, which is both the most stable point on
    a walking human and the one least likely to sample background sky.
    """

    x1, _y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, float(y2))


def sample_depth_m(
    depth_image,
    u: float,
    v: float,
    depth_scale: float,
    patch_radius: int = 4,
    min_m: float = 0.2,
    max_m: float = 12.0,
) -> Optional[float]:
    """Median depth in metres over a small patch around ``(u, v)``.

    A single depth pixel is frequently zero (a hole, a reflective surface, a
    shadow in the IR pattern). The median of a small neighbourhood, taken
    over valid samples only, is far more robust and still cheap.

    Returns ``None`` when there is no usable depth rather than a sentinel
    number that could be mistaken for a measurement.
    """

    if depth_image is None or depth_scale is None or depth_scale <= 0:
        return None

    try:
        height, width = depth_image.shape[:2]
    except (AttributeError, ValueError):
        return None

    if height <= 0 or width <= 0:
        return None

    cu = int(round(u))
    cv = int(round(v))

    # The bottom-centre point of a box touching the frame edge sits exactly on
    # (or just past) the boundary; clamp instead of discarding the detection.
    cu = max(0, min(width - 1, cu))
    cv = max(0, min(height - 1, cv))

    radius = max(0, int(patch_radius))
    u0, u1 = max(0, cu - radius), min(width, cu + radius + 1)
    v0, v1 = max(0, cv - radius), min(height, cv + radius + 1)

    try:
        patch = depth_image[v0:v1, u0:u1]
    except Exception:  # pragma: no cover - defensive against odd array types
        return None

    values = []
    try:
        for row in patch:
            for raw in row:
                metres = float(raw) * depth_scale
                if min_m <= metres <= max_m:
                    values.append(metres)
    except TypeError:
        return None

    if not values:
        return None

    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def deproject(
    intrinsics: CameraIntrinsics,
    u: float,
    v: float,
    depth_m: float,
) -> Optional[dict]:
    """Image point + depth -> camera-relative XYZ in metres.

    Standard pinhole de-projection. ``x`` is right, ``y`` is down, ``z`` is
    forward along the optical axis -- the RealSense camera convention.
    """

    if intrinsics is None or not intrinsics.is_valid():
        return None
    if depth_m is None or depth_m <= 0:
        return None

    x = (u - intrinsics.ppx) / intrinsics.fx * depth_m
    y = (v - intrinsics.ppy) / intrinsics.fy * depth_m
    return {
        "x": round(float(x), 3),
        "y": round(float(y), 3),
        "z": round(float(depth_m), 3),
    }


#: Shape returned by :func:`describe_person_depth` when nothing is measurable.
#: Every key is always present so consumers never branch on missing fields --
#: ``depth_valid`` is the single flag that says whether the numbers are real.
_EMPTY_DEPTH = {
    "depth_m": None,
    "camera_x_m": None,
    "camera_y_m": None,
    "camera_z_m": None,
    "relative_x_m": None,
    "relative_forward_m": None,
    "depth_valid": False,
    "depth_status": "NO_DEPTH",
}


def describe_person_depth(
    bbox: Sequence[int],
    depth_image,
    intrinsics: Optional[CameraIntrinsics],
    depth_scale: Optional[float],
    patch_radius: int = 4,
    min_m: float = 0.2,
    max_m: float = 12.0,
) -> dict:
    """Per-track depth in the flat form WASTRAQ consumes.

    Always returns a dict with the same keys. ``depth_valid`` is false and the
    numbers are ``None`` whenever depth could not be measured -- there is no
    sentinel distance that could be mistaken for a real one.

    ``depth_status`` says *why*, which is what makes a field failure
    diagnosable instead of merely absent:

    * ``OK``               -- measured
    * ``NO_DEPTH_FRAME``   -- no depth image this frame (or depth disabled)
    * ``NO_INTRINSICS``    -- camera did not report usable intrinsics
    * ``NO_DEPTH_SCALE``   -- camera did not report a depth scale
    * ``NO_VALID_SAMPLES`` -- every pixel in the patch was a hole or
      out of the [min_m, max_m] range
    * ``DEPROJECTION_FAILED`` -- depth measured but XYZ could not be derived
    """

    result = dict(_EMPTY_DEPTH)

    if depth_image is None:
        result["depth_status"] = "NO_DEPTH_FRAME"
        return result
    if intrinsics is None or not intrinsics.is_valid():
        result["depth_status"] = "NO_INTRINSICS"
        return result
    if not depth_scale:
        result["depth_status"] = "NO_DEPTH_SCALE"
        return result

    u, v = representative_point(bbox)
    depth_m = sample_depth_m(
        depth_image,
        u,
        v,
        depth_scale=depth_scale,
        patch_radius=patch_radius,
        min_m=min_m,
        max_m=max_m,
    )
    if depth_m is None:
        result["depth_status"] = "NO_VALID_SAMPLES"
        return result

    point = deproject(intrinsics, u, v, depth_m)
    if point is None:
        result["depth_m"] = round(float(depth_m), 3)
        result["depth_status"] = "DEPROJECTION_FAILED"
        return result

    return {
        "depth_m": round(float(depth_m), 3),
        "camera_x_m": point["x"],
        "camera_y_m": point["y"],
        "camera_z_m": point["z"],
        # Aliases WASTRAQ reads directly. Same numbers, named for the
        # consumer's frame of reference: +x right of the camera, +z forward.
        "relative_x_m": point["x"],
        "relative_forward_m": point["z"],
        "depth_valid": True,
        "depth_status": "OK",
    }


def estimate_camera_position(
    bbox: Sequence[int],
    depth_image,
    intrinsics: Optional[CameraIntrinsics],
    depth_scale: Optional[float],
    patch_radius: int = 4,
    min_m: float = 0.2,
    max_m: float = 12.0,
) -> Optional[dict]:
    """Full path: box -> representative point -> depth -> XYZ.

    ``None`` at any missing input. Depth is never mandatory.

    Kept in its original ``{x, y, z}`` shape because the dashboard renders it;
    :func:`describe_person_depth` is the richer form used for integration.
    """

    described = describe_person_depth(
        bbox=bbox,
        depth_image=depth_image,
        intrinsics=intrinsics,
        depth_scale=depth_scale,
        patch_radius=patch_radius,
        min_m=min_m,
        max_m=max_m,
    )
    if not described["depth_valid"]:
        return None
    return {
        "x": described["camera_x_m"],
        "y": described["camera_y_m"],
        "z": described["camera_z_m"],
    }
