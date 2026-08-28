"""Detection: overlay filter + white table."""

from __future__ import annotations

import cv2
import numpy as np

from store_vision.calibration import calibrate_homographies, calibrate_shared_fisheye
from store_vision.config import StoreConfig
from store_vision.data import load_store_folder
from store_vision.detection.overlay_filter import remove_overlay_lines
from store_vision.detection.white_table import detect_white_tables


def test_overlay_filter_reduces_saturation(fixture_dir):
    ds = load_store_folder(fixture_dir)
    cfg = StoreConfig()
    calib = next(iter(ds.cameras.values()))
    img = cv2.imread(calib.image_path)
    assert img is not None
    clean, mask = remove_overlay_lines(img, calib, cfg)
    # Synthetic image has cyan/yellow lines; clean should have less saturation.
    s_before = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 1].mean()
    s_after = cv2.cvtColor(clean, cv2.COLOR_BGR2HSV)[..., 1].mean()
    assert s_after <= s_before + 1


def test_white_table_detects_synthetic(fixture_dir):
    from store_vision.data.models import Point2D

    ds = load_store_folder(fixture_dir)
    cfg = StoreConfig()
    # 测试图片中的白桌由生成器固定在 30%～70%、55%～85% 区域；给第一台
    # 相机提供与当前 2560×1440 图片一致的显式控制点，避免依赖旧测试夹具中
    # 3840×2160 标定坐标未经转换时的偶然投影。
    first = ds.camera_list()[0]
    first.camera_points = [
        Point2D(768, 792),
        Point2D(1792, 792),
        Point2D(1792, 1224),
        Point2D(768, 1224),
    ]
    first.map_points = [
        Point2D(45, 40),
        Point2D(55, 40),
        Point2D(55, 55),
        Point2D(45, 55),
    ]
    calibrate_shared_fisheye(ds)
    calibrate_homographies(ds)
    fw, fh = ds.floor_plan_size

    found_any = False
    for calib in ds.camera_list():
        if not calib.image_path:
            continue
        img = cv2.imread(calib.image_path)
        if img is None:
            continue
        clean, _ = remove_overlay_lines(img, calib, cfg)
        cands = detect_white_tables(clean, calib, cfg, fw, fh)
        if any(c.accepted for c in cands):
            found_any = True
            break
    assert found_any, "should detect at least one white table"


def test_white_table_rejects_solid_background():
    """Pure-grey image should yield zero accepted tables (no calib quad supplied)."""
    from store_vision.data.models import CameraCalibration, Point2D

    cfg = StoreConfig()
    img = np.full((1080, 1920, 3), 200, dtype=np.uint8)
    calib = CameraCalibration(
        device_serial="TEST-NULL",
        name="null",
        serialnum="TEST-NULL-101",
        camera_points=[
            Point2D(100, 100), Point2D(200, 100),
            Point2D(200, 200), Point2D(100, 200),
        ],
        map_points=[
            Point2D(10, 10), Point2D(20, 10),
            Point2D(20, 20), Point2D(10, 20),
        ],
    )
    cands = detect_white_tables(img, calib, cfg, 1600, 900)
    accepted = [c for c in cands if c.accepted]
    assert len(accepted) == 0
