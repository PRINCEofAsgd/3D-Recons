"""Per-camera homography (undistorted image px ↔ floor plan px)."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from store_vision.calibration.distortion import distort_points, undistort_points
from store_vision.data.models import CameraCalibration, StoreDataset
from store_vision.geometry.coords import map_percent_to_floor_px
from store_vision.geometry.projection import apply_homography


def undistort_camera_pts(calib: CameraCalibration) -> np.ndarray:
    """Camera calibration points after lens-distortion correction."""
    src = calib.camera_points_array()
    if calib.K is not None and calib.dist_coeffs is not None:
        return undistort_points(src, calib.K, calib.dist_coeffs)
    return src


def calibrate_homographies(
    dataset: StoreDataset,
    out_dir: Path | None = None,
) -> dict[str, float]:
    fw, fh = dataset.floor_plan_size or (1600, 900)
    rmses: dict[str, float] = {}
    dump: dict = {}
    for calib in dataset.camera_list():
        src = undistort_camera_pts(calib)
        dst = map_percent_to_floor_px(calib.map_points_pct_array(), fw, fh)
        H, _ = cv2.findHomography(src, dst, method=0)
        if H is None:
            continue
        calib.homography = H
        calib.homography_inv = np.linalg.inv(H)
        proj = apply_homography(H, src)
        rmse = float(np.sqrt(np.mean(np.sum((proj - dst) ** 2, axis=1))))
        calib.reprojection_rmse = rmse
        rmses[calib.device_serial] = rmse
        dump[calib.device_serial] = {
            "rmse_floor_px": rmse,
            "H": H.tolist(),
            "map_pts_pct": calib.map_points_pct_array().tolist(),
            "cam_pts_px": calib.camera_points_array().tolist(),
        }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "homography.json", "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2)
    return rmses


def image_polygon_to_floor_px(
    calib: CameraCalibration,
    poly_img: np.ndarray,
) -> np.ndarray:
    if calib.homography is None:
        return np.empty((0, 2))
    pts = np.asarray(poly_img, dtype=np.float64)
    if calib.K is not None and calib.dist_coeffs is not None:
        pts = undistort_points(pts, calib.K, calib.dist_coeffs)
    return apply_homography(calib.homography, pts)


def image_polygon_to_height_plane_px(
    calib: CameraCalibration,
    poly_img: np.ndarray,
    height_m: float,
) -> tuple[np.ndarray, str]:
    """把原始图像轮廓反投影到指定世界高度，再落到平面图像素系。

    现有 ``H`` 仍负责把世界地面锚定到门店平面图；``K/D/R/t`` 只负责
    把观测射线从 ``Z=0`` 修正到 ``Z=height_m``。因此高度为零时与旧路线
    完全等价，同时避免把约 70 cm 高的桌面错误地当作地面。
    """

    ground = image_polygon_to_floor_px(calib, poly_img)
    if height_m <= 1e-9:
        return ground, "ground_homography"
    if calib.homography is None:
        return np.empty((0, 2)), "unavailable:no_homography"
    if calib.K is None or calib.R is None or calib.t is None:
        return ground, "ground_homography_fallback:missing_K_R_t"

    k = np.asarray(calib.K, dtype=np.float64).reshape(3, 3)
    rotation = np.asarray(calib.R, dtype=np.float64).reshape(3, 3)
    translation = np.asarray(calib.t, dtype=np.float64).reshape(3)
    camera_centre = -rotation.T @ translation
    if not np.isfinite(camera_centre).all() or camera_centre[2] <= height_m + 1e-3:
        return ground, "ground_homography_fallback:invalid_camera_height"

    # 世界平面 Z=z 到去畸变针孔图像的单应为
    # K [r1 r2 (t + z*r3)]。H 已把 Z=0 图像锚定到平面图，组合后即可
    # 在不改变平面图坐标系的前提下修正桌面 XY。
    ground_world_to_image = k @ np.column_stack(
        [rotation[:, 0], rotation[:, 1], translation]
    )
    height_world_to_image = k @ np.column_stack(
        [
            rotation[:, 0],
            rotation[:, 1],
            translation + height_m * rotation[:, 2],
        ]
    )
    try:
        image_to_plan = (
            np.asarray(calib.homography, dtype=np.float64)
            @ ground_world_to_image
            @ np.linalg.inv(height_world_to_image)
        )
    except np.linalg.LinAlgError:
        return ground, "ground_homography_fallback:singular_plane_projection"
    if not np.isfinite(image_to_plan).all():
        return ground, "ground_homography_fallback:non_finite_plane_projection"

    points = np.asarray(poly_img, dtype=np.float64)
    if calib.dist_coeffs is not None:
        points = undistort_points(points, k, calib.dist_coeffs)
    projected = apply_homography(image_to_plan, points)
    if not np.isfinite(projected).all():
        return ground, "ground_homography_fallback:non_finite_result"
    return projected, "height_aware_rt_plan_anchor"


def floor_polygon_to_camera_polygon(
    calib: CameraCalibration,
    floor_poly: list[tuple[float, float]] | np.ndarray,
) -> np.ndarray | None:
    """Map floor-plan polygon to original (distorted) camera image pixels."""
    if calib.homography_inv is None:
        return None
    pts = np.asarray(floor_poly, dtype=np.float64)
    cam_und = apply_homography(calib.homography_inv, pts)
    if calib.K is not None and calib.dist_coeffs is not None:
        return distort_points(cam_und, calib.K, calib.dist_coeffs)
    return cam_und


def floor_rect_to_camera_polygon(
    calib: CameraCalibration,
    rect: tuple[float, float, float, float],
) -> np.ndarray | None:
    x0, y0, x1, y1 = rect
    return floor_polygon_to_camera_polygon(
        calib, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    )


def apply_image_to_floor(calib: CameraCalibration, pts: np.ndarray) -> np.ndarray:
    return image_polygon_to_floor_px(calib, pts)
