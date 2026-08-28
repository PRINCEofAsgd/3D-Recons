"""多单应矩阵共享内参、尺度解析和正式 CLI 的专项测试。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from store_vision.calibration.scale_metadata import (
    build_identity_audit,
    fit_plan_scale,
    load_scale_metadata,
)
from store_vision.calibration.shared_calibration import (
    SharedCalibrationConfig,
    run_shared_intrinsics_calibration,
)
from store_vision.calibration.shared_intrinsics import (
    decompose_homography_pose,
    refine_intrinsics_constraints,
    solve_shared_intrinsics,
)
from store_vision.cli import build_parser


CAMERAS = (
    "0001-AAAA-BBBB-CCCC",
    "0002-AAAA-BBBB-CCCC",
    "0003-AAAA-BBBB-CCCC",
    "0004-AAAA-BBBB-CCCC",
)


def _scale_points() -> list[dict[str, float]]:
    return [
        {"rx": 0.0, "ry": 0.0, "rw": 0.0, "rh": 0.0},
        {"rx": 100.0, "ry": 0.0, "rw": 16.0, "rh": 0.0},
        {"rx": 0.0, "ry": 100.0, "rw": 0.0, "rh": 12.0},
        {"rx": 100.0, "ry": 100.0, "rw": 16.0, "rh": 12.0},
    ]


def _synthetic_homographies() -> tuple[np.ndarray, list[np.ndarray]]:
    """生成满足同一 K、不同俯视姿态的精确平面 H。"""
    K = np.array([[1500.0, 0.0, 1920.0], [0.0, 1500.0, 1080.0], [0.0, 0.0, 1.0]])
    homographies: list[np.ndarray] = []
    for index, angles in enumerate(((178, 3, 5), (175, -6, 92), (171, 8, -88), (179, -4, 178))):
        R = Rotation.from_euler("xyz", angles, degrees=True).as_matrix()
        center = np.array([3.0 + index * 2.0, -4.0 - index, 4.0])
        t = -R @ center
        H = K @ np.column_stack([R[:, 0], R[:, 1], t])
        homographies.append(H / H[2, 2])
    return K, homographies


def _write_dataset(
    root: Path,
    image_sizes: list[tuple[int, int]] | None = None,
) -> Path:
    """按 GUI screenshots 结构构造共享标定最小集成数据。"""
    dataset = root / "dataset"
    dataset.mkdir()
    screenshots = dataset / "screenshots"
    screenshots.mkdir()
    K, homographies = _synthetic_homographies()
    # 控制点必须真实位于 3840×2160 图片内，输入预检不再接受越界标定。
    percent = np.array([[35.0, 35.0], [42.0, 35.0], [42.0, 45.0], [35.0, 45.0]])
    scale = fit_plan_scale(_scale_points())
    world = scale.percent_to_world(percent)
    records = []
    sizes = image_sizes or [(3840, 2160)] * len(CAMERAS)
    for index, (camera_id, H, size) in enumerate(
        zip(CAMERAS, homographies, sizes), start=1
    ):
        width, height = size
        native_transform = np.diag([width / 3840.0, height / 2160.0, 1.0])
        native_h = native_transform @ H
        image_points = cv2.perspectiveTransform(
            world.reshape(-1, 1, 2), native_h
        ).reshape(-1, 2)
        records.append(
            {
                "id": index,
                "serialnum": f"{camera_id}-101",
                "deviceSerialnum": camera_id,
                "name": camera_id,
                "gateId": index,
                "deviceSnapWidth": width,
                "deviceSnapHeight": height,
                "coordinates": json.dumps(
                    {
                        "cameraPoints": [
                            {"x": str(point[0]), "y": str(point[1])}
                            for point in image_points
                        ],
                        "mapPoints": [
                            {"x": str(point[0]), "y": str(point[1])}
                            for point in percent
                        ],
                    }
                ),
            }
        )
        cv2.imwrite(
            str(screenshots / f"{camera_id}-01.jpg"),
            np.zeros((height, width, 3), np.uint8),
        )
    (dataset / "cali.txt").write_text(
        json.dumps({"data": {"total": len(records), "list": records}}),
        encoding="utf-8",
    )
    (dataset / "scale.txt").write_text(
        json.dumps(
            {
                "data": {
                    "id": 1,
                    "name": "synthetic",
                    "area": 6,
                    "mallPlan": "remote.png",
                    "calibrationPoints": json.dumps(_scale_points()),
                    "mallPlanCoordinates": json.dumps(
                        [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}]
                    ),
                    "deviceNum": len(CAMERAS),
                    "deviceList": [{"serialnum": camera_id} for camera_id in CAMERAS],
                }
            }
        ),
        encoding="utf-8",
    )
    return dataset


def test_scale_fit_and_world_axis_definition():
    scale = fit_plan_scale(_scale_points())
    world = scale.percent_to_world(np.array([[50.0, 25.0]]))[0]
    assert np.allclose(world, [8.0, -3.0])
    assert np.allclose(scale.world_to_percent(world.reshape(1, 2)), [[50.0, 25.0]])


def test_scale_metadata_does_not_interpret_area_as_scale(tmp_path):
    path = tmp_path / "scale.txt"
    path.write_text(
        json.dumps(
            {
                "data": {
                    "area": 6.0,
                    "calibrationPoints": json.dumps(_scale_points()),
                    "mallPlanCoordinates": "[]",
                    "deviceList": [],
                }
            }
        ),
        encoding="utf-8",
    )
    scale, metadata = load_scale_metadata(path)
    assert np.isclose(scale.x_metres_per_percent, 0.16)
    assert "不参与" in metadata["area_interpretation"]


def test_identity_audit_preserves_scale_and_cali_discrepancy():
    audit = build_identity_audit(["A", "B"], ["A", "C"], ["A", "C"])
    assert audit["intersection_all_three"] == ["A"]
    assert audit["cali_and_image_not_scale"] == ["C"]
    assert audit["scale_not_cali_or_image"] == ["B"]


def test_linear_and_refined_model_a_recover_shared_focal():
    expected, homographies = _synthetic_homographies()
    linear = solve_shared_intrinsics(homographies, (3840, 2160), "A")
    refined = refine_intrinsics_constraints(homographies, (3840, 2160), "A", linear.K)
    assert linear.success and refined["success"]
    assert np.isclose(refined["K"][0, 0], expected[0, 0], rtol=1e-5)


def test_model_c_recovers_free_principal_point():
    expected, homographies = _synthetic_homographies()
    linear = solve_shared_intrinsics(homographies, (3840, 2160), "C")
    refined = refine_intrinsics_constraints(homographies, (3840, 2160), "C", linear.K)
    assert refined["success"]
    assert np.allclose(refined["K"], expected, rtol=1e-4, atol=1e-3)


def test_pose_decomposition_selects_above_ground_downward_solution():
    K, homographies = _synthetic_homographies()
    world = np.array([[2.0, -2.0], [4.0, -2.0], [4.0, -5.0], [2.0, -5.0]])
    image = cv2.perspectiveTransform(world.reshape(-1, 1, 2), homographies[0]).reshape(-1, 2)
    pose = decompose_homography_pose(homographies[0], K, world, image)
    assert pose["camera_center"][2] > 0
    assert pose["optical_axis_world"][2] < 0
    assert pose["rmse_image_px"] < 1e-6


def test_cli_shared_intrinsics_arguments():
    args = build_parser().parse_args(
        [
            "shared-intrinsics-calibration",
            "--dataset-path",
            "dataset",
            "--output-path",
            "output",
            "--project-root",
            "root",
            "--seed",
            "7",
            "--dry-run",
        ]
    )
    assert args.command == "shared-intrinsics-calibration"
    assert args.seed == 7 and args.dry_run


def test_shared_calibration_dry_run_does_not_write(tmp_path):
    dataset = _write_dataset(tmp_path)
    output = tmp_path / "output"
    result = run_shared_intrinsics_calibration(dataset, output, dry_run=True)
    assert result["status"] == "dry_run"
    assert not output.exists()


def test_small_real_execution_writes_reports_and_recovers_pose(tmp_path):
    dataset = _write_dataset(tmp_path)
    output = tmp_path / "output"
    result = run_shared_intrinsics_calibration(
        dataset,
        output,
        project_root=tmp_path,
        config=SharedCalibrationConfig(random_subset_count=4),
    )
    assert result["selection"]["selected_model"] == "FISHEYE"
    assert len(result["selection"]["selected_D"]) == 4
    assert result["nonlinear_optimization"]["joint_K_D_R_T_ba_performed"]
    assert result["nonlinear_optimization"]["bundle_adjustment"][
        "uses_fitted_calibration_observations"
    ]
    assert len(result["poses"]) == len(CAMERAS)
    assert all(row["camera_above_ground"] for row in result["poses"])
    assert (output / "shared_calibration_report.json").is_file()
    assert (output / "camera_pose_map.png").is_file()


def test_shared_calibration_normalizes_same_aspect_mixed_resolutions(tmp_path):
    """同一 16:9 组的 4K/1080p H 应转到规范像素系后共同求 K/R/T。"""
    dataset = _write_dataset(
        tmp_path,
        [(3840, 2160), (1920, 1080), (3840, 2160), (1920, 1080)],
    )
    result = run_shared_intrinsics_calibration(
        dataset,
        tmp_path / "mixed-output",
        project_root=tmp_path,
        config=SharedCalibrationConfig(random_subset_count=4),
    )

    assert result["input"]["image_resolutions"] == [[1920, 1080], [3840, 2160]]
    assert result["summary"]["intrinsics_group_count"] == 1
    assert len(result["poses"]) == len(CAMERAS)
    assert {
        tuple(row["canonical_resolution"]) for row in result["homographies"]
    } == {(3840, 2160)}
    assert (tmp_path / "mixed-output" / "intrinsics_groups.json").is_file()


def test_shared_calibration_uses_one_fisheye_model_across_resolutions(tmp_path):
    """同型号路线将所有相机转换到参考像素系后共享一组鱼眼 K/D。"""
    dataset = _write_dataset(
        tmp_path,
        [(3840, 2160), (1920, 1080), (2560, 2560), (1280, 1280)],
    )
    result = run_shared_intrinsics_calibration(
        dataset,
        tmp_path / "aspect-output",
        project_root=tmp_path,
        config=SharedCalibrationConfig(random_subset_count=2),
    )

    assert result["summary"]["intrinsics_group_count"] == 1
    groups = result["intrinsics_groups"]
    assert groups[0]["camera_model"] == "OPENCV_FISHEYE"
    assert len(groups[0]["camera_ids"]) == 4
    assert len(groups[0]["native_resolutions"]) == 4
