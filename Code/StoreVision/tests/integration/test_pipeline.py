"""End-to-end pipeline + UI smoke."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from store_vision.config import StoreConfig
from store_vision.pipeline import run_pipeline


ROOT = Path(__file__).resolve().parents[2]


def test_pipeline_full(fixture_dir, tmp_path):
    cfg = StoreConfig()
    chosen_output = tmp_path / "user-chosen-output"
    result = run_pipeline(fixture_dir, cfg, output_dir=chosen_output)
    assert result.output_dir == chosen_output
    assert result.output_dir.exists()
    assert (result.output_dir / "calibration" / "distortion.json").exists()
    assert (result.output_dir / "calibration" / "homography.json").exists()
    assert (result.output_dir / "calibration" / "bundle.json").exists()
    assert (result.output_dir / "overlap" / "overlaps.json").exists()
    assert (result.output_dir / "map25d.geojson").exists()
    assert (result.output_dir / "detection" / "table_summary.json").exists()
    assert (result.output_dir / "stitch" / "stitch_mosaic.jpg").exists()

    geo = json.loads((result.output_dir / "map25d.geojson").read_text(encoding="utf-8"))
    assert geo["type"] == "FeatureCollection"


def test_pipeline_skip_detection(fixture_dir, tmp_path):
    from store_vision.data import load_store_folder

    cfg = StoreConfig()
    # 桌面端传入的是已加载数据集，而不是仅传整合目录路径。
    result = run_pipeline(
        load_store_folder(fixture_dir),
        cfg,
        skip_detection=True,
        output_dir=tmp_path / "selected-by-user",
    )
    assert result.objects == []
    assert (result.output_dir / "stitch" / "stitch_mosaic.jpg").exists()


def test_pipeline_supports_native_mixed_resolution_cameras(tmp_path):
    """核心标定和拼接逐图读取尺寸，不要求源截图宽高相同。"""
    import cv2
    import numpy as np

    from store_vision.data.models import (
        CameraCalibration,
        Point2D,
        StoreDataset,
    )

    floor_path = tmp_path / "floorplan.png"
    assert cv2.imwrite(
        str(floor_path), np.full((450, 800, 3), 220, np.uint8)
    )
    cameras = {}
    for serial, size in (("MIXED-A", (640, 360)), ("MIXED-B", (1280, 720))):
        width, height = size
        image_path = tmp_path / f"{serial}.jpg"
        assert cv2.imwrite(
            str(image_path), np.full((height, width, 3), 120, np.uint8)
        )
        cameras[serial] = CameraCalibration(
            device_serial=serial,
            name=serial,
            serialnum=f"{serial}-101",
            camera_points=[
                Point2D(width * 0.2, height * 0.2),
                Point2D(width * 0.8, height * 0.2),
                Point2D(width * 0.8, height * 0.8),
                Point2D(width * 0.2, height * 0.8),
            ],
            map_points=[
                Point2D(20, 20),
                Point2D(80, 20),
                Point2D(80, 80),
                Point2D(20, 80),
            ],
            image_path=str(image_path),
            calibration_size=size,
            image_size=size,
            coordinate_mode="pixel_exact",
        )
    dataset = StoreDataset(
        root=str(tmp_path),
        floor_plan_path=str(floor_path),
        floor_plan_size=(800, 450),
        cameras=cameras,
    )

    result = run_pipeline(
        dataset,
        StoreConfig(),
        skip_detection=True,
        output_dir=tmp_path / "mixed-output",
    )

    assert result.stitch_image is not None
    assert result.stitch_image.shape[:2] == (450, 800)
    resolution_report = json.loads(
        (
            tmp_path / "mixed-output" / "calibration" / "input_resolution.json"
        ).read_text(encoding="utf-8")
    )
    assert resolution_report["status"] == "ready"


@pytest.mark.timeout(30)
def test_ui_smoke(qapp, fixture_dir):
    from store_vision.data import load_store_folder
    from store_vision.ui.main_window import MainWindow

    cfg = StoreConfig()
    cfg.output_root = ROOT / "output_ui_smoke"
    win = MainWindow(cfg)
    ds = load_store_folder(fixture_dir)
    win.tab_map25d.set_dataset(ds)
    win.tab_map.set_geojson(
        {"type": "FeatureCollection", "features": []},
        floor_plan_path=ds.floor_plan_path,
    )
    assert win.tab_calib.dataset is ds
    assert win.tabs.count() == 3
    win.close()
