"""Depth sampling and de-projection, including every missing-input path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.depth_position import (  # noqa: E402
    CameraIntrinsics,
    deproject,
    estimate_camera_position,
    representative_point,
    sample_depth_m,
)

INTRINSICS = CameraIntrinsics(
    width=640, height=480, fx=615.0, fy=615.0, ppx=320.0, ppy=240.0
)
SCALE = 0.001  # RealSense millimetres


def flat_depth(value_mm=3000, width=640, height=480):
    """A depth image as a plain nested list -- no numpy needed."""

    return _Grid([[value_mm] * width for _ in range(height)])


class _Grid(list):
    """Minimal stand-in exposing the ``.shape`` and slicing sample_depth uses."""

    @property
    def shape(self):
        return (len(self), len(self[0]) if self else 0)

    def __getitem__(self, key):
        if isinstance(key, tuple):
            row_slice, col_slice = key
            return [row[col_slice] for row in list.__getitem__(self, row_slice)]
        return list.__getitem__(self, key)


class RepresentativePointTests(unittest.TestCase):
    def test_bottom_center_of_the_box(self):
        self.assertEqual(representative_point((420, 150, 540, 460)), (480.0, 460.0))


class SampleDepthTests(unittest.TestCase):
    def test_median_over_a_uniform_patch(self):
        depth = flat_depth(3000)
        self.assertAlmostEqual(
            sample_depth_m(depth, 320, 240, SCALE, patch_radius=2), 3.0
        )

    def test_12_all_zero_depth_returns_none(self):
        depth = flat_depth(0)
        self.assertIsNone(sample_depth_m(depth, 320, 240, SCALE, patch_radius=2))

    def test_12_missing_depth_image_returns_none(self):
        self.assertIsNone(sample_depth_m(None, 320, 240, SCALE))

    def test_12_missing_or_zero_scale_returns_none(self):
        depth = flat_depth(3000)
        self.assertIsNone(sample_depth_m(depth, 320, 240, None))
        self.assertIsNone(sample_depth_m(depth, 320, 240, 0.0))

    def test_12_out_of_range_values_are_discarded(self):
        # 50 metres is beyond max_m; nothing valid remains.
        depth = flat_depth(50000)
        self.assertIsNone(
            sample_depth_m(depth, 320, 240, SCALE, patch_radius=2, max_m=12.0)
        )

    def test_point_on_the_frame_edge_is_clamped_not_discarded(self):
        depth = flat_depth(3000)
        # A person whose feet touch the bottom edge: v == height.
        self.assertAlmostEqual(
            sample_depth_m(depth, 639, 480, SCALE, patch_radius=3), 3.0
        )

    def test_holes_are_skipped_and_the_valid_median_survives(self):
        rows = [[0] * 640 for _ in range(480)]
        for v in range(238, 243):
            for u in range(318, 323):
                rows[v][u] = 2500
        depth = _Grid(rows)
        self.assertAlmostEqual(
            sample_depth_m(depth, 320, 240, SCALE, patch_radius=2), 2.5
        )


class DeprojectTests(unittest.TestCase):
    def test_principal_point_maps_to_the_optical_axis(self):
        position = deproject(INTRINSICS, 320.0, 240.0, 3.9)
        self.assertEqual(position, {"x": 0.0, "y": 0.0, "z": 3.9})

    def test_offset_point_gives_a_plausible_lateral_offset(self):
        position = deproject(INTRINSICS, 420.0, 240.0, 3.0)
        # (420 - 320) / 615 * 3.0
        self.assertAlmostEqual(position["x"], 0.488, places=3)
        self.assertEqual(position["z"], 3.0)

    def test_12_invalid_intrinsics_return_none(self):
        broken = CameraIntrinsics(640, 480, 0.0, 0.0, 320.0, 240.0)
        self.assertIsNone(deproject(broken, 320.0, 240.0, 3.0))

    def test_12_missing_intrinsics_or_depth_return_none(self):
        self.assertIsNone(deproject(None, 320.0, 240.0, 3.0))
        self.assertIsNone(deproject(INTRINSICS, 320.0, 240.0, 0.0))
        self.assertIsNone(deproject(INTRINSICS, 320.0, 240.0, None))


class EstimatePositionTests(unittest.TestCase):
    def test_full_path_produces_xyz(self):
        position = estimate_camera_position(
            bbox=(300, 100, 340, 240),
            depth_image=flat_depth(3900),
            intrinsics=INTRINSICS,
            depth_scale=SCALE,
        )
        self.assertIsNotNone(position)
        self.assertEqual(position["z"], 3.9)

    def test_13_depth_is_optional_everywhere(self):
        bbox = (300, 100, 340, 240)
        self.assertIsNone(
            estimate_camera_position(bbox, None, INTRINSICS, SCALE)
        )
        self.assertIsNone(
            estimate_camera_position(bbox, flat_depth(3000), None, SCALE)
        )
        self.assertIsNone(
            estimate_camera_position(bbox, flat_depth(3000), INTRINSICS, None)
        )
        # Depth present but entirely holes.
        self.assertIsNone(
            estimate_camera_position(bbox, flat_depth(0), INTRINSICS, SCALE)
        )


if __name__ == "__main__":
    unittest.main()
