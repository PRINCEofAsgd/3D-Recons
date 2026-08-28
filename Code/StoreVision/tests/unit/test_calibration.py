"""Calibration tests: shared fisheye / homography / BA / floor↔camera roundtrip."""

from __future__ import annotations

import numpy as np

from store_vision.calibration import (
    calibrate_shared_fisheye,
    calibrate_homographies,
    floor_polygon_to_camera_polygon,
    image_polygon_to_floor_px,
    image_polygon_to_height_plane_px,
)
from store_vision.data import load_store_folder
from store_vision.data.models import CameraCalibration


def test_shared_fisheye_yields_common_k_dist(fixture_dir):
    ds = load_store_folder(fixture_dir)
    result = calibrate_shared_fisheye(ds)
    assert result["camera_model"] == "OPENCV_FISHEYE"
    assert result["bundle_adjustment"]["performed"]
    for c in ds.camera_list():
        assert c.K is not None and c.K.shape == (3, 3)
        assert c.dist_coeffs is not None and c.dist_coeffs.shape == (4,)
        assert c.camera_model == "OPENCV_FISHEYE"


def test_homography_low_rmse(fixture_dir):
    ds = load_store_folder(fixture_dir)
    calibrate_shared_fisheye(ds)
    rmses = calibrate_homographies(ds)
    assert len(rmses) == len(ds.cameras)
    for v in rmses.values():
        assert v < 5.0


def test_fitted_initialization_drives_joint_ba(fixture_dir):
    ds = load_store_folder(fixture_dir)
    result = calibrate_shared_fisheye(ds)
    assert result["fitted_initialization"]["fit"]["success"]
    assert result["bundle_adjustment"]["initialization_source"] == "fitted_initialization"
    assert result["bundle_adjustment"]["uses_fitted_calibration_observations"]
    for c in ds.camera_list():
        assert np.isfinite(c.reprojection_rmse)


def test_floor_camera_roundtrip(fixture_dir):
    ds = load_store_folder(fixture_dir)
    # 用可逆的共享鱼眼参数单独验证投影辅助函数；夹具的四点仅用于
    # 流程回归，不承担鱼眼参数真值。
    for calibration in ds.camera_list():
        width, height = calibration.image_size or (2560, 1440)
        focal = 0.85 * max(width, height)
        calibration.K = np.array(
            [[focal, 0.0, width / 2], [0.0, focal, height / 2], [0.0, 0.0, 1.0]]
        )
        calibration.dist_coeffs = np.array([0.01, -0.001, 0.0001, 0.0])
    calibrate_homographies(ds)
    from store_vision.geometry.coords import map_percent_to_floor_px

    fw, fh = ds.floor_plan_size
    hits = 0
    for calib in ds.camera_list():
        # 使用每台相机真实覆盖的控制区域，避免把远离标定区域的外推误差
        # 混入鱼眼畸变/去畸变互逆测试。
        rect_arr = map_percent_to_floor_px(
            calib.map_points_pct_array(), fw, fh
        )
        rect = [tuple(point) for point in rect_arr]
        cam_poly = floor_polygon_to_camera_polygon(calib, rect)
        if cam_poly is None:
            continue
        back = image_polygon_to_floor_px(calib, cam_poly)
        if back.size == 0:
            continue
        # Roundtrip should be close to original.
        diff = np.linalg.norm(back - rect_arr, axis=1).mean()
        if diff < 5.0:
            hits += 1
    assert hits >= 1, "no roundtrip succeeded"


def test_height_plane_projection_uses_rt_and_preserves_plan_anchor():
    """同一像素投到 70 cm 桌面时应比投到地面更靠近相机正下方。"""

    focal = 1000.0
    calibration = CameraCalibration(
        device_serial="synthetic",
        name="synthetic",
        serialnum="synthetic",
        camera_points=[],
        map_points=[],
        K=np.array(
            [[focal, 0.0, 500.0], [0.0, focal, 400.0], [0.0, 0.0, 1.0]]
        ),
        dist_coeffs=np.zeros(4),
        R=np.diag([1.0, -1.0, -1.0]),
        # 相机中心位于世界 Z=2 m，因此 t=-R*C=[0,0,2]。
        t=np.array([0.0, 0.0, 2.0]),
        homography=np.array(
            [[2.0 / focal, 0.0, -1.0], [0.0, -2.0 / focal, 0.8], [0.0, 0.0, 1.0]]
        ),
    )
    pixels = np.array([[1000.0, 400.0], [500.0, 900.0]])

    ground = image_polygon_to_floor_px(calibration, pixels)
    tabletop, method = image_polygon_to_height_plane_px(
        calibration, pixels, 0.70
    )

    assert method == "height_aware_rt_plan_anchor"
    np.testing.assert_allclose(tabletop, ground * 0.65, atol=1e-8)
