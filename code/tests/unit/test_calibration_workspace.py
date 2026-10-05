"""COLMAP 标定优化工作区的报告归一化测试。"""

from __future__ import annotations

import json

from store_vision.ui.calibration_workspace import (
    create_unique_analysis_directory,
    create_unique_run_directory,
    discover_report_roots,
    discover_shared_result,
    load_calibration_workspace,
)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_workspace_normalizes_v02_v03_v04_reports(tmp_path):
    dataset = tmp_path / "data" / "shop"
    dataset.mkdir(parents=True)
    experiment = tmp_path / "outputs" / "shop" / "calibration_demo" / "run_001"
    reports = experiment / "reports"
    _write_json(
        reports / "calibration_report.json",
        {
            "colmap_status": "partial",
            "total_images": 9,
            "registered_images": ["a.jpg", "b.jpg"],
            "sparse_points": 116,
            "geometry_diagnostics": {
                "status": "complete",
                "global_findings": ["真实诊断结论"],
            },
        },
    )
    _write_json(
        reports / "match_statistics.json",
        {"pairs": [{"verified_matches": 10} for _ in range(36)]},
    )
    _write_json(
        reports / "calibration_data_inventory.json",
        {
            "status": "blocked",
            "physical_camera_count": 9,
            "mapping_confirmed": True,
            "complete_intrinsics_count": 0,
            "complete_distortion_count": 0,
            "complete_extrinsics_count": 0,
            "cameras": [{"physical_camera_id": str(index)} for index in range(9)],
            "blocking_reasons": ["缺少真实 K/D/R/t"],
        },
    )
    _write_json(
        reports / "feature_track_summary.json",
        {"cleaned_track_count": 2063, "length_at_least_3": 638},
    )
    _write_json(
        reports / "manual_calibration_baseline.json",
        {"status": "blocked", "reason": "缺少真实 K/D/R/t"},
    )
    pair = reports / "pair_diagnostics" / "a__b"
    _write_json(pair / "pair_summary.json", {"verified_matches": 10})
    (pair / "verified_matches.png").write_bytes(b"not-a-real-png")

    snapshot = load_calibration_workspace(dataset, experiment)

    values = {metric.label: metric.value for metric in snapshot.metrics}
    assert values["验证匹配图像对"] == "36/36"
    assert values["视觉 SfM 注册"] == "2/9"
    assert values["清洗后轨迹"] == "2063"
    assert len(snapshot.stages) == 6
    assert all(stage.status == "未生成" for stage in snapshot.stages)
    assert len(snapshot.pairs) == 1
    assert len(snapshot.cameras) == 9
    assert "真实诊断结论" in snapshot.findings


def test_unique_run_paths_never_reuse_existing_directory(tmp_path):
    first = create_unique_run_directory(tmp_path)
    first.mkdir(parents=True)
    second = create_unique_run_directory(tmp_path)
    assert first != second
    assert not second.exists()

    experiment = tmp_path / "run_001"
    analysis = create_unique_analysis_directory(experiment)
    assert analysis.parent == experiment / "analysis"
    assert not analysis.exists()


def test_legacy_repository_reports_are_not_mixed_across_datasets(tmp_path):
    """自动兼容仓库级 V0.4 报告时必须确认其审计目标属于当前门店。"""

    (tmp_path / "code").mkdir(parents=True)
    (tmp_path / "code" / "pyproject.toml").write_text("", encoding="utf-8")
    current_dataset = tmp_path / "data" / "current"
    other_dataset = tmp_path / "data" / "other"
    current_dataset.mkdir(parents=True)
    other_dataset.mkdir(parents=True)
    _write_json(
        tmp_path / "reports" / "calibration_data_inventory.json",
        {"audited_dataset_files": [str(other_dataset / "cali.txt")]},
    )

    roots = discover_report_roots(current_dataset, None)
    assert tmp_path / "reports" not in roots


def test_fisheye_result_is_discovered_and_normalized_for_same_dataset(tmp_path):
    """鱼眼结果自动发现后应形成六阶段、共享 K/D 和逐机位姿视图。"""

    (tmp_path / "code").mkdir(parents=True)
    (tmp_path / "code" / "pyproject.toml").write_text("", encoding="utf-8")
    dataset = tmp_path / "data" / "shop"
    dataset.mkdir(parents=True)
    result = tmp_path / "outputs" / "shop" / "fisheye_calibration" / "run_001"
    _write_json(
        result / "shared_calibration_report.json",
        {
            "input": {"dataset_path": str(dataset.resolve())},
            "scale": {"metric_unit": "metre", "rmse_metres": 0.02},
            "homographies": [{}, {}],
            "intrinsics_models": {
                "FISHEYE": {
                    "definition": "opencv_fisheye_shared_K_D_joint_poses",
                    "K": [[1500, 0, 1920], [0, 1500, 1080], [0, 0, 1]],
                    "D": [0.1, -0.01, 0.001, 0.0],
                    "initialization": {
                        "fit": {"success": True, "reprojection_rmse_px": 2.0}
                    },
                    "bundle_adjustment": {
                        "performed": True,
                        "success": True,
                        "reprojection_rmse_px": 1.0,
                        "uses_fitted_calibration_observations": True,
                    },
                }
            },
            "selection": {
                "selected_model": "FISHEYE",
                "selected_K": [[1500, 0, 1920], [0, 1500, 1080], [0, 0, 1]],
                "selected_D": [0.1, -0.01, 0.001, 0.0],
            },
            "poses": [
                {
                    "physical_camera_id": "camera-a",
                    "height_metres": 4.2,
                    "physical_plausibility_passed": True,
                }
            ],
            "feature_track_plane_validation": {
                "candidate_counts_by_spread_threshold_metres": {"0.25": 3}
            },
            "summary": {
                "physical_plausibility_pass_rate": 1.0,
                "selected_model_stable": True,
                "fitted_initialization_available": True,
                "bundle_adjustment_performed": True,
                "bundle_adjustment_success": True,
                "safe_as_ba_initialization": True,
            },
        },
    )

    assert discover_shared_result(dataset) == result.resolve()
    snapshot = load_calibration_workspace(dataset, None)
    assert snapshot.shared is not None
    assert snapshot.shared.selected_model == "FISHEYE"
    assert snapshot.shared.selected_D == [0.1, -0.01, 0.001, 0.0]
    assert len(snapshot.shared.poses) == 1
    assert [stage.status for stage in snapshot.stages] == [
        "complete",
        "complete",
        "blocked",
        "blocked",
        "blocked",
        "blocked",
    ]


def test_explicit_shared_result_from_other_dataset_is_rejected(tmp_path):
    """手工选择结果目录也不能绕过跨门店身份校验。"""

    current = tmp_path / "current"
    other = tmp_path / "other"
    current.mkdir()
    other.mkdir()
    result = tmp_path / "run_other"
    _write_json(
        result / "shared_calibration_report.json",
        {"input": {"dataset_path": str(other.resolve())}},
    )

    snapshot = load_calibration_workspace(current, None, shared_result_path=result)
    assert snapshot.shared is None
    assert any("其他门店" in warning for warning in snapshot.warnings)
