"""Per-track depth: the flat WASTRAQ form, and every way it can be absent.

Phase 1 requires that a missing, invalid or out-of-range depth degrades to
"no measurement" rather than a wrong one, and that the tracker keeps working
either way. These tests use plain nested lists as the depth image so nothing
here needs numpy or a camera.
"""

import unittest

from vision.depth_position import (
    CameraIntrinsics,
    describe_person_depth,
    estimate_camera_position,
)

INTRINSICS = CameraIntrinsics(width=640, height=480, fx=615.0, fy=615.0, ppx=320.0, ppy=240.0)
DEPTH_SCALE = 0.001  # millimetres, as on a RealSense


class FakeDepthImage(list):
    """A list-of-lists that slices like a numpy array.

    ``sample_depth_m`` does ``depth[v0:v1, u0:u1]``; a plain list raises on a
    tuple key, so the stand-in has to support it. Mirrors ``_Grid`` in
    ``test_depth_position.py`` -- no numpy required.
    """

    def __init__(self, rows, width, fill):
        super().__init__([[fill] * width for _ in range(rows)])
        self.shape = (rows, width)

    def poke(self, u, v, value):
        list.__getitem__(self, v)[u] = value

    def __getitem__(self, key):
        if isinstance(key, tuple):
            row_slice, col_slice = key
            return [row[col_slice] for row in list.__getitem__(self, row_slice)]
        return list.__getitem__(self, key)


def image(fill=3000, rows=480, width=640):
    return FakeDepthImage(rows, width, fill)


class ValidDepthTests(unittest.TestCase):
    """Phase 1 / Phase 12 item 2: the conversion helper on good input."""

    def setUp(self):
        # Person box whose bottom-centre lands at (320, 400).
        self.bbox = (270, 100, 370, 400)
        self.result = describe_person_depth(
            bbox=self.bbox,
            depth_image=image(3540),
            intrinsics=INTRINSICS,
            depth_scale=DEPTH_SCALE,
        )

    def test_depth_is_measured_in_metres(self):
        self.assertTrue(self.result["depth_valid"])
        self.assertEqual(self.result["depth_status"], "OK")
        self.assertAlmostEqual(self.result["depth_m"], 3.54, places=3)

    def test_deprojection_matches_the_pinhole_model(self):
        # u = 320 == ppx, so x must be exactly 0.
        self.assertAlmostEqual(self.result["camera_x_m"], 0.0, places=3)
        # v = 400, ppy = 240 -> y = (400-240)/615 * 3.54
        self.assertAlmostEqual(
            self.result["camera_y_m"], (400 - 240) / 615.0 * 3.54, places=2
        )
        self.assertAlmostEqual(self.result["camera_z_m"], 3.54, places=3)

    def test_wastraq_aliases_mirror_x_and_z(self):
        self.assertEqual(self.result["relative_x_m"], self.result["camera_x_m"])
        self.assertEqual(self.result["relative_forward_m"], self.result["camera_z_m"])

    def test_off_centre_person_gets_a_signed_lateral_offset(self):
        left = describe_person_depth(
            bbox=(20, 100, 120, 400),  # bottom-centre u = 70, left of ppx
            depth_image=image(3000),
            intrinsics=INTRINSICS,
            depth_scale=DEPTH_SCALE,
        )
        self.assertLess(left["camera_x_m"], 0.0)

    def test_a_hole_at_the_exact_centre_pixel_is_survived(self):
        # The whole point of sampling a patch rather than one pixel.
        depth = image(3000)
        depth.poke(320, 400, 0)
        result = describe_person_depth(
            bbox=(270, 100, 370, 400),
            depth_image=depth,
            intrinsics=INTRINSICS,
            depth_scale=DEPTH_SCALE,
        )
        self.assertTrue(result["depth_valid"])
        self.assertAlmostEqual(result["depth_m"], 3.0, places=3)


class InvalidDepthTests(unittest.TestCase):
    """Phase 12 item 3: every failure mode reports, none fabricates."""

    def assert_no_measurement(self, result, status):
        self.assertFalse(result["depth_valid"])
        self.assertEqual(result["depth_status"], status)
        for key in (
            "depth_m",
            "camera_x_m",
            "camera_y_m",
            "camera_z_m",
            "relative_x_m",
            "relative_forward_m",
        ):
            self.assertIsNone(result[key], f"{key} must be null, not a sentinel")

    def test_no_depth_frame(self):
        self.assert_no_measurement(
            describe_person_depth((0, 0, 10, 10), None, INTRINSICS, DEPTH_SCALE),
            "NO_DEPTH_FRAME",
        )

    def test_no_intrinsics(self):
        self.assert_no_measurement(
            describe_person_depth((0, 0, 10, 10), image(), None, DEPTH_SCALE),
            "NO_INTRINSICS",
        )

    def test_degenerate_intrinsics(self):
        broken = CameraIntrinsics(640, 480, 0.0, 0.0, 320.0, 240.0)
        self.assert_no_measurement(
            describe_person_depth((0, 0, 10, 10), image(), broken, DEPTH_SCALE),
            "NO_INTRINSICS",
        )

    def test_no_depth_scale(self):
        self.assert_no_measurement(
            describe_person_depth((0, 0, 10, 10), image(), INTRINSICS, None),
            "NO_DEPTH_SCALE",
        )

    def test_all_holes(self):
        self.assert_no_measurement(
            describe_person_depth(
                (270, 100, 370, 400), image(0), INTRINSICS, DEPTH_SCALE
            ),
            "NO_VALID_SAMPLES",
        )

    def test_out_of_range_far(self):
        # 60 m: beyond any plausible D455 reading, so not a measurement.
        self.assert_no_measurement(
            describe_person_depth(
                (270, 100, 370, 400), image(60000), INTRINSICS, DEPTH_SCALE
            ),
            "NO_VALID_SAMPLES",
        )

    def test_out_of_range_near(self):
        self.assert_no_measurement(
            describe_person_depth(
                (270, 100, 370, 400), image(50), INTRINSICS, DEPTH_SCALE
            ),
            "NO_VALID_SAMPLES",
        )

    def test_bbox_on_the_frame_edge_is_clamped_not_discarded(self):
        # Bottom-centre v = 480 is one past the last row.
        result = describe_person_depth(
            bbox=(600, 200, 640, 480),
            depth_image=image(2500),
            intrinsics=INTRINSICS,
            depth_scale=DEPTH_SCALE,
        )
        self.assertTrue(result["depth_valid"])

    def test_shape_is_stable_across_success_and_failure(self):
        good = describe_person_depth(
            (270, 100, 370, 400), image(3000), INTRINSICS, DEPTH_SCALE
        )
        bad = describe_person_depth((270, 100, 370, 400), None, INTRINSICS, DEPTH_SCALE)
        self.assertEqual(set(good), set(bad))


class BackwardCompatibilityTests(unittest.TestCase):
    """The dashboard still reads ``camera_position_m`` as ``{x, y, z}``."""

    def test_legacy_helper_still_returns_xyz(self):
        point = estimate_camera_position(
            (270, 100, 370, 400), image(3000), INTRINSICS, DEPTH_SCALE
        )
        self.assertEqual(set(point), {"x", "y", "z"})
        self.assertAlmostEqual(point["z"], 3.0, places=3)

    def test_legacy_helper_returns_none_when_depth_is_unusable(self):
        self.assertIsNone(
            estimate_camera_position((0, 0, 10, 10), None, INTRINSICS, DEPTH_SCALE)
        )


if __name__ == "__main__":
    unittest.main()
