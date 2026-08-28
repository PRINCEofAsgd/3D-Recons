"""Geometry helpers tests."""

from __future__ import annotations

import numpy as np

from store_vision.config import StoreConfig
from store_vision.geometry.coords import (
    floor_px_to_cm,
    map_percent_to_cm,
    map_percent_to_floor_px,
)
from store_vision.data.models import CameraCalibration, Point2D, StoreDataset
from store_vision.geometry.projection import (
    apply_homography,
    best_camera_for_floor_polygon,
    point_in_polygon,
    polygon_centroid,
)


def test_percent_to_floor_px():
    out = map_percent_to_floor_px([(0.0, 0.0), (100.0, 100.0)], 1600, 900)
    assert out[0, 0] == 0
    assert out[1, 0] == 1600
    assert out[1, 1] == 900


def test_percent_to_cm_default():
    cfg = StoreConfig()
    out = map_percent_to_cm([(50.0, 50.0)], cfg)
    assert abs(out[0, 0] - cfg.floor_plan_width_cm / 2) < 1e-6
    assert abs(out[0, 1] - cfg.floor_plan_height_cm / 2) < 1e-6


def test_floor_px_to_cm():
    cfg = StoreConfig()
    out = floor_px_to_cm([(800.0, 450.0)], 1600, 900, cfg)
    assert abs(out[0, 0] - cfg.floor_plan_width_cm / 2) < 1e-6


def test_apply_homography_identity():
    H = np.eye(3)
    pts = np.array([[3, 4], [5, 6]], dtype=np.float64)
    out = apply_homography(H, pts)
    np.testing.assert_allclose(out, pts, atol=1e-9)


def test_polygon_centroid():
    cx, cy = polygon_centroid([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert cx == 5.0 and cy == 5.0


def test_point_in_polygon():
    poly = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon(5, 5, poly)
    assert not point_in_polygon(15, 5, poly)


def _make_synthetic_camera(
    serial: str,
    cam_corners: list[tuple[float, float]],
    floor_corners: list[tuple[float, float]],
) -> CameraCalibration:
    """Build a CameraCalibration with H computed from given correspondences."""
    import cv2

    src = np.asarray(cam_corners, dtype=np.float64)
    dst = np.asarray(floor_corners, dtype=np.float64)
    H, _ = cv2.findHomography(src, dst)
    calib = CameraCalibration(
        device_serial=serial,
        name=serial,
        serialnum=serial,
        camera_points=[Point2D(*p) for p in cam_corners],
        map_points=[Point2D(*p) for p in floor_corners],
        homography=H,
        homography_inv=np.linalg.inv(H),
    )
    return calib


def test_best_camera_prefers_image_center():
    """Two cameras both cover a floor patch; the one whose image polygon lies
    near the image center wins over the one that sees it near the edge."""
    # Camera A: image 1000x1000, full image maps to floor [0,500]x[0,500]
    cam_a = _make_synthetic_camera(
        "A",
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
        [(0, 0), (500, 0), (500, 500), (0, 500)],
    )
    # Camera B: image 1000x1000, full image maps to floor [200,700]x[200,700]
    # → floor (250..300, 250..300) lands near image center (100, 100)?? Let's
    # compute: floor 250 → (250-200)/(700-200)*1000 = 100. So poly centre at
    # 250 in floor → 100 px in image (near corner). Camera A maps floor 250 →
    # 250/500*1000 = 500 px (image centre). So A should win.
    cam_b = _make_synthetic_camera(
        "B",
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
        [(200, 200), (700, 200), (700, 700), (200, 700)],
    )
    dataset = StoreDataset(
        root=".",
        floor_plan_path=None,
        cameras={"A": cam_a, "B": cam_b},
        floor_plan_size=(1000, 1000),
    )
    image_sizes = {"A": (1000, 1000), "B": (1000, 1000)}

    floor_poly = [(240, 240), (260, 240), (260, 260), (240, 260)]
    out = best_camera_for_floor_polygon(dataset, floor_poly, image_sizes)
    assert out is not None
    cam, score = out
    assert cam.device_serial == "A"
    assert score < 0.05  # very near A's image centre


def test_best_camera_returns_none_when_uncalibrated():
    cam = CameraCalibration(
        device_serial="X",
        name="X",
        serialnum="X",
        camera_points=[Point2D(0, 0)] * 4,
        map_points=[Point2D(0, 0)] * 4,
    )
    dataset = StoreDataset(
        root=".",
        floor_plan_path=None,
        cameras={"X": cam},
        floor_plan_size=(1000, 1000),
    )
    out = best_camera_for_floor_polygon(
        dataset, [(0, 0), (10, 0), (10, 10)], image_sizes={"X": (100, 100)}
    )
    assert out is None
