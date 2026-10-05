"""Loader & data parsing tests."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from store_vision.data import (
    load_gui_dataset_folder,
    load_store_folder,
    load_store_inputs,
)


def test_loader_parses_cameras(fixture_dir):
    ds = load_store_folder(fixture_dir)
    assert len(ds.cameras) >= 9
    assert ds.floor_plan_path is not None
    assert ds.floor_plan_size is not None
    for c in ds.camera_list():
        assert len(c.camera_points) == 4
        assert len(c.map_points) == 4


def test_loader_finds_images(fixture_dir):
    ds = load_store_folder(fixture_dir)
    matched = sum(1 for c in ds.camera_list() if c.image_path)
    assert matched == len(ds.cameras)


def test_loader_overlay_polygons(fixture_dir):
    ds = load_store_folder(fixture_dir)
    has_overlay = any(c.overlay_polygons_img for c in ds.camera_list())
    assert has_overlay, "expected at least one camera to have areaInfo polygons"


def test_loader_accepts_separately_selected_inputs(fixture_dir):
    """分开选择的文件不必位于同一个输入目录。"""
    camera_paths = list(fixture_dir.glob("*.jpg"))
    ds = load_store_inputs(
        fixture_dir / "cali.txt",
        fixture_dir / "footfallplan.png",
        camera_paths,
    )
    assert ds.floor_plan_path == str((fixture_dir / "footfallplan.png").resolve())
    assert sum(1 for camera in ds.camera_list() if camera.image_path) == len(ds.cameras)


def test_loader_accepts_canonical_store_layout(fixture_dir, tmp_path):
    import shutil

    root = tmp_path / "canonical-store"
    (root / "calibration").mkdir(parents=True)
    (root / "floorplan").mkdir()
    images = root / "cameras" / "images"
    images.mkdir(parents=True)
    shutil.copy2(fixture_dir / "cali.txt", root / "calibration" / "cali.txt")
    shutil.copy2(fixture_dir / "footfallplan.png", root / "floorplan" / "floorplan.png")
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, images / image.name)

    dataset = load_store_folder(root)
    assert dataset.root == str(root.resolve())
    assert dataset.floor_plan_path == str((root / "floorplan" / "floorplan.png").resolve())
    assert sum(camera.image_path is not None for camera in dataset.camera_list()) == len(dataset.cameras)


def test_gui_loader_uses_only_fixed_dataset_layout(fixture_dir, tmp_path):
    """桌面入口固定读取根目录三文件和 screenshots 中的全部图片。"""

    import shutil

    root = tmp_path / "gui-dataset"
    screenshots = root / "screenshots"
    screenshots.mkdir(parents=True)
    shutil.copy2(fixture_dir / "cali.txt", root / "cali.txt")
    (root / "scale.txt").write_text("{}", encoding="utf-8")
    shutil.copy2(fixture_dir / "footfallplan.png", root / "floorplan.png")
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, screenshots / image.name)
    # 根目录中的图片不是 GUI 相机输入，防止示意图或历史文件混入。
    shutil.copy2(next(fixture_dir.glob("*.jpg")), root / "preview.jpg")

    dataset = load_gui_dataset_folder(root)

    assert dataset.root == str(root.resolve())
    assert dataset.floor_plan_path == str((root / "floorplan.png").resolve())
    assert dataset.scale_path == str((root / "scale.txt").resolve())
    image_paths = [camera.image_path for camera in dataset.camera_list()]
    assert all(path is not None for path in image_paths)
    assert all(Path(path).parent == screenshots.resolve() for path in image_paths if path)


def test_gui_loader_accepts_json_inputs_and_jpg_floorplan(fixture_dir, tmp_path):
    """新版固定目录可直接使用 cali/scale JSON 和 JPG 平面图。"""

    import json
    import shutil

    import cv2

    root = tmp_path / "json-gui-dataset"
    screenshots = root / "screenshots"
    screenshots.mkdir(parents=True)
    shutil.copy2(fixture_dir / "cali.txt", root / "cali.json")
    (root / "scale.json").write_text(
        json.dumps({"data": {"calibrationPoints": "[]"}}),
        encoding="utf-8",
    )
    floor = cv2.imread(str(fixture_dir / "footfallplan.png"))
    assert cv2.imwrite(str(root / "floorplan.jpg"), floor)
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, screenshots / image.name)

    dataset = load_gui_dataset_folder(root)

    assert dataset.floor_plan_path == str((root / "floorplan.jpg").resolve())
    assert dataset.scale_path == str((root / "scale.json").resolve())
    assert len(dataset.cameras) >= 9


def test_gui_loader_prefers_existing_txt_when_json_is_also_present(
    fixture_dir, tmp_path
):
    """TXT/JSON 并存时保持旧 TXT 的稳定优先级。"""

    import shutil

    root = tmp_path / "dual-format-dataset"
    screenshots = root / "screenshots"
    screenshots.mkdir(parents=True)
    shutil.copy2(fixture_dir / "cali.txt", root / "cali.txt")
    (root / "cali.json").write_text("{}", encoding="utf-8")
    (root / "scale.txt").write_text("{}", encoding="utf-8")
    (root / "scale.json").write_text("not-json", encoding="utf-8")
    shutil.copy2(fixture_dir / "footfallplan.png", root / "floorplan.png")
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, screenshots / image.name)

    dataset = load_gui_dataset_folder(root)

    assert dataset.scale_path == str((root / "scale.txt").resolve())
    assert len(dataset.cameras) >= 9


def test_gui_loader_reports_missing_fixed_inputs(tmp_path):
    """固定目录不完整时一次指出所有缺失入口。"""

    import pytest

    root = tmp_path / "incomplete"
    root.mkdir()

    with pytest.raises(FileNotFoundError) as exc_info:
        load_gui_dataset_folder(root)

    message = str(exc_info.value)
    assert "cali.txt 或 cali.json" in message
    assert "scale.txt 或 scale.json" in message
    assert "floorplan.png/jpg/jpeg" in message
    assert "screenshots/" in message


def _write_single_camera_cali(
    path: Path,
    *,
    points: list[tuple[float, float]],
    calibration_size: tuple[int, int] | None,
) -> None:
    """构造异分辨率输入测试使用的最小 API 标定文件。"""
    import json

    record = {
        "id": 1,
        "serialnum": "ABCD-1234-5678-90EF-101",
        "deviceSerialnum": "ABCD-1234-5678-90EF",
        "name": "mixed-resolution-camera",
        "coordinates": json.dumps(
            {
                "cameraPoints": [{"x": x, "y": y} for x, y in points],
                "mapPoints": [
                    {"x": 10, "y": 10},
                    {"x": 20, "y": 10},
                    {"x": 20, "y": 20},
                    {"x": 10, "y": 20},
                ],
            }
        ),
    }
    if calibration_size:
        record["deviceSnapWidth"], record["deviceSnapHeight"] = calibration_size
    path.write_text(
        json.dumps({"data": {"list": [record]}}), encoding="utf-8"
    )


def test_loader_scales_pixel_calibration_to_same_aspect_image(tmp_path):
    """4K 像素标定可无损适配到同视场的 1080p 截图。"""
    cali = tmp_path / "cali.json"
    image = tmp_path / "ABCD-1234-5678-90EF-101.jpg"
    _write_single_camera_cali(
        cali,
        points=[(400, 200), (1200, 200), (1200, 800), (400, 800)],
        calibration_size=(3840, 2160),
    )
    assert cv2.imwrite(str(image), np.zeros((1080, 1920, 3), np.uint8))

    dataset = load_store_inputs(cali, None, [image])
    camera = dataset.camera_list()[0]

    assert camera.coordinate_mode == "pixel_scaled"
    assert camera.image_size == (1920, 1080)
    assert np.allclose(
        camera.camera_points_array(),
        [[200, 100], [600, 100], [600, 400], [200, 400]],
    )
    assert not dataset.resolution_errors()


def test_loader_maps_normalized_coordinates_to_native_image(tmp_path):
    """0～1 标定点按当前图片宽高转换，不要求其他相机尺寸相同。"""
    cali = tmp_path / "cali.json"
    image = tmp_path / "ABCD-1234-5678-90EF-101.jpg"
    _write_single_camera_cali(
        cali,
        points=[(0.1, 0.2), (0.8, 0.2), (0.8, 0.7), (0.1, 0.7)],
        calibration_size=(2560, 2560),
    )
    assert cv2.imwrite(str(image), np.zeros((2560, 2560, 3), np.uint8))

    camera = load_store_inputs(cali, None, [image]).camera_list()[0]

    assert camera.coordinate_mode == "normalized_to_image"
    assert np.allclose(camera.camera_points_array()[0], [256, 512])


def test_loader_blocks_unknown_aspect_or_crop_transform(tmp_path):
    """宽高比变化且没有裁剪元数据时必须报告 error，不能当普通缩放。"""
    cali = tmp_path / "cali.json"
    image = tmp_path / "ABCD-1234-5678-90EF-101.jpg"
    _write_single_camera_cali(
        cali,
        points=[(400, 200), (1200, 200), (1200, 800), (400, 800)],
        calibration_size=(3840, 2160),
    )
    assert cv2.imwrite(str(image), np.zeros((1200, 1600, 3), np.uint8))

    dataset = load_store_inputs(cali, None, [image])

    assert {
        row["code"] for row in dataset.resolution_errors()
    } >= {"calibration_aspect_mismatch"}
