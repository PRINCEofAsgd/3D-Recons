from __future__ import annotations

import json
import shutil

from store_vision.calibration import workflow
from store_vision.calibration.workflow import run_calibration_workflow


def _write_fitted_fisheye_report(root, dataset):
    """为 Sfm dry-run 构造身份匹配的最小拟合 K/D 报告。"""

    root.mkdir(parents=True)
    (root / "fisheye_calibration_report.json").write_text(
        json.dumps(
            {
                "input": {
                    "dataset_path": str(dataset.resolve()),
                    "image_resolution": [3840, 2160],
                },
                "selection": {
                    "selected_model": "FISHEYE",
                    "selected_K": [
                        [1200.0, 0.0, 1920.0],
                        [0.0, 1180.0, 1080.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "selected_D": [0.1, -0.01, 0.001, -0.0001],
                },
                "intrinsics_groups": [{"canonical_resolution": [3840, 2160]}],
                "nonlinear_optimization": {
                    "initialization": {
                        "fit": {
                            "accepted_by_reprojection_threshold": True,
                            "reprojection_rmse_px": 2.0,
                        }
                    },
                    "bundle_adjustment": {
                        "accepted_by_reprojection_threshold": True,
                        "reprojection_rmse_px": 1.5,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_calibration_workflow_skip_colmap_preserves_manual_path(fixture_dir, tmp_path):
    result = run_calibration_workflow(fixture_dir, tmp_path / "demo", skip_colmap=True)
    assert result.manual_camera_count >= 9
    assert result.colmap_status == "skipped"
    assert result.report_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["metrics"]["position_mean"] is None
    assert report["total_images"] == 9
    assert report["registered_images"] == 0
    assert report["feature_count_by_image"] == {}
    assert report["sparse_models"] == []
    required_fields = {
        "colmap_status",
        "colmap_version",
        "total_images",
        "registered_images",
        "unregistered_images",
        "feature_count_by_image",
        "verified_matches_by_pair",
        "connected_components",
        "sparse_models",
        "sparse_points",
        "mean_reprojection_error_px",
        "warnings",
    }
    assert required_fields <= report.keys()
    assert report["geometry_diagnostics"]["status"] == "not_run"
    assert report["intrinsics_configuration"]["status"] == "missing_required_intrinsics"
    assert (tmp_path / "demo" / "reports" / "reconstruction_summary.json").is_file()
    assert (tmp_path / "demo" / "reports" / "known_intrinsics_experiment.json").is_file()


def test_calibration_workflow_dry_run_outputs_commands(fixture_dir, tmp_path):
    fitted = _write_fitted_fisheye_report(tmp_path / "fitted", fixture_dir)
    result = run_calibration_workflow(
        fixture_dir,
        tmp_path / "dry",
        dry_run=True,
        fisheye_calibration=fitted,
    )
    assert result.colmap_status == "dry-run"
    assert [command[1] for command in result.commands] == [
        "feature_extractor",
        "exhaustive_matcher",
        "global_mapper",
        "mapper",
    ]
    assert result.report_path is None
    feature = result.commands[0]
    assert feature[feature.index("--ImageReader.camera_model") + 1] == "OPENCV_FISHEYE"
    assert "--ImageReader.camera_params" in feature
    global_mapper = result.commands[2]
    assert (
        global_mapper[
            global_mapper.index("--GlobalMapper.track_min_num_views_per_track") + 1
        ]
        == "2"
    )
    mapper = result.commands[3]
    assert mapper[mapper.index("--Mapper.ba_refine_focal_length") + 1] == "0"
    assert mapper[mapper.index("--Mapper.ba_refine_extra_params") + 1] == "0"
    assert not (tmp_path / "dry" / "colmap" / "database.db").exists()
    assert not (tmp_path / "dry" / "database.db").exists()


def test_calibration_workflow_missing_colmap_writes_degraded_report(
    fixture_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(workflow, "detect_colmap", lambda: None)
    result = run_calibration_workflow(fixture_dir, tmp_path / "missing")
    assert result.colmap_status == "unavailable"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["colmap_status"] == "unavailable"
    assert report["colmap_version"] is None
    assert report["registered_images"] == 0
    assert not (tmp_path / "missing" / "database.db").exists()


def test_calibration_workflow_reads_gui_screenshots_layout(fixture_dir, tmp_path):
    """COLMAP 工作流从 GUI 固定 screenshots 目录选择九台相机图。"""

    dataset = tmp_path / "dataset"
    screenshots = dataset / "screenshots"
    screenshots.mkdir(parents=True)
    shutil.copy2(fixture_dir / "cali.txt", dataset / "cali.txt")
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, screenshots / image.name)

    result = run_calibration_workflow(
        dataset,
        tmp_path / "screenshots-demo",
        skip_colmap=True,
    )

    assert result.total_images == 9
    assert result.manual_camera_count >= 9


def test_calibration_workflow_disables_single_camera_for_mixed_sizes(
    fixture_dir, tmp_path, monkeypatch
):
    """COLMAP 混合尺寸输入必须生成多个 camera 记录。"""

    def fake_size(path):
        return (1920, 1080) if "CAMERA-01" in str(path) else (3840, 2160)

    monkeypatch.setattr(workflow, "read_image_size", fake_size)
    fitted = _write_fitted_fisheye_report(tmp_path / "mixed-fitted", fixture_dir)
    result = run_calibration_workflow(
        fixture_dir,
        tmp_path / "mixed-dry",
        dry_run=True,
        fisheye_calibration=fitted,
    )
    feature = result.commands[0]

    option = feature.index("--ImageReader.single_camera") + 1
    assert feature[option] == "0"
    assert "--ImageReader.camera_params" not in feature
    assert any("混合尺寸" in message for message in result.diagnostics)
