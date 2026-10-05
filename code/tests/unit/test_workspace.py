"""统一中间层、能力解析和两条业务边界测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from store_vision.config import StoreConfig
from store_vision.data import (
    candidate_names,
    discover_intermediate_packages,
    find_latest_intermediate_package,
    load_intermediate_runtime,
    load_store_folder,
    publish_intermediate_package,
    publish_sfm_derived_package,
    resolve_capabilities,
)
from store_vision.mapping.parameter_stitcher import (
    ParameterStitchConfig,
    run_parameter_stitching,
)
from store_vision.mapping.map25d_pipeline import (
    run_map25d_from_intermediate,
)


def _shared_report(dataset_path: Path) -> dict:
    """为夹具构造已通过门禁的共享鱼眼 K/D/R/t 报告。"""

    dataset = load_store_folder(dataset_path)
    first = dataset.camera_list()[0]
    width, height = first.image_size or (640, 360)
    K = [
        [500.0, 0.0, width / 2.0],
        [0.0, 500.0, height / 2.0],
        [0.0, 0.0, 1.0],
    ]
    homographies = []
    poses = []
    rotation = np.diag([1.0, -1.0, -1.0])
    for index, camera in enumerate(dataset.camera_list()):
        center = np.asarray([index * 0.5, 0.0, 2.0])
        translation = -rotation @ center
        homographies.append(
            {
                "physical_camera_id": camera.device_serial,
                "image_to_canonical_transform": np.eye(3).tolist(),
            }
        )
        poses.append(
            {
                "physical_camera_id": camera.device_serial,
                "R": rotation.tolist(),
                "T_metres": translation.tolist(),
                "pose_reprojection_rmse_px": 1.0,
            }
        )
    return {
        "selection": {
            "selected_model": "FISHEYE",
            "selected_K": K,
            "selected_D": [0.0, 0.0, 0.0, 0.0],
            "intrinsics_validation": {
                "status": "credible",
                "usable_for_sfm": True,
                "resolution": [width, height],
            },
        },
        "homographies": homographies,
        "poses": poses,
        "coordinate_conventions": {
            "extrinsics": "world_to_camera",
            "world": "X right, Y plan-up, Z up; metre",
        },
    }


def test_publish_intermediate_and_resolve_business_capabilities(
    fixture_dir,
    tmp_path,
):
    """发布后两个业务共享候选，但各自保留独立能力结论。"""

    package = tmp_path / "intermediate" / "run_001"
    manifest = publish_intermediate_package(
        fixture_dir,
        package,
        _shared_report(fixture_dir),
        cfg=StoreConfig(workspace_data_root=tmp_path / "data"),
    )

    assert manifest["package_type"] == "store_vision_intermediate"
    assert candidate_names(package) == ("estimated",)
    assert (
        find_latest_intermediate_package(package.parent, fixture_dir)
        == package.resolve()
    )
    capabilities = resolve_capabilities(package)
    assert capabilities.map25d.ready
    assert capabilities.stitching.ready
    assert not capabilities.sfm_assisted.ready
    runtime = load_intermediate_runtime(package)
    assert runtime.candidate_name == "estimated"
    assert set(runtime.dataset.cameras) == set(runtime.camera_rig["cameras"])
    assert (package / "frames" / "ground_plane.json").is_file()
    assert (package / "business" / "height_policy.json").is_file()


def test_capability_resolver_reports_exact_missing_resource(
    fixture_dir,
    tmp_path,
):
    """损坏候选矩阵时返回字段级缺失项，不依赖目录扫描猜测。"""

    package = tmp_path / "intermediate" / "run_001"
    publish_intermediate_package(
        fixture_dir,
        package,
        _shared_report(fixture_dir),
    )
    rig_path = package / "calibrations" / "estimated" / "camera_rig.json"
    rig = json.loads(rig_path.read_text(encoding="utf-8"))
    camera_id = next(iter(rig["cameras"]))
    rig["cameras"][camera_id]["K"] = [[1.0, 0.0], [0.0, 1.0]]
    rig_path.write_text(
        json.dumps(rig, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    capabilities = resolve_capabilities(package)
    assert not capabilities.calibration.ready
    assert f"camera:{camera_id}:K_shape" in capabilities.calibration.missing
    assert not capabilities.map25d.ready
    assert not capabilities.stitching.ready


def test_parameter_stitching_consumes_only_intermediate(
    fixture_dir,
    tmp_path,
    monkeypatch,
):
    """参数拼接从 K/D/R/t 和地面合同生成完整独立输出包。"""

    package = tmp_path / "intermediate" / "run_001"
    publish_intermediate_package(
        fixture_dir,
        package,
        _shared_report(fixture_dir),
    )
    from store_vision.data import workspace as workspace_module

    monkeypatch.setattr(
        workspace_module,
        "load_store_folder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("业务运行不应重新解析原始数据集")
        ),
    )
    output = tmp_path / "stitching_output" / "run_001"
    result = run_parameter_stitching(
        package,
        output,
        config=ParameterStitchConfig(
            pixels_per_metre=8.0,
            fallback_radius_metres=4.0,
            max_ground_distance_metres=4.0,
            max_canvas_long_edge=400,
            max_canvas_pixels=120_000,
            feather_radius_pixels=8,
            crop_margin_pixels=2,
        ),
    )

    assert result.fused_path.is_file()
    assert result.alpha_path.is_file()
    assert result.coverage_path.is_file()
    assert result.overlap_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["package_type"] == "store_vision_stitching_output"
    assert report["source_intermediate"] == str(package.resolve())
    assert report["summary"]["camera_count"] > 0
    assert not report["raw_inputs_modified"]


def test_map25d_consumes_normalized_intermediate_skeleton(
    fixture_dir,
    tmp_path,
    monkeypatch,
):
    """2.5D 由中间层控制骨架运行，不重新读取 cali/scale。"""

    from store_vision.data import workspace as workspace_module
    from store_vision.detection.table_locator import TableDetectionResult
    from store_vision.mapping import map25d_pipeline

    package = tmp_path / "intermediate" / "run_001"
    publish_intermediate_package(
        fixture_dir,
        package,
        _shared_report(fixture_dir),
    )
    monkeypatch.setattr(
        workspace_module,
        "load_store_folder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("2.5D 不应重新解析原始数据集")
        ),
    )
    monkeypatch.setattr(
        map25d_pipeline,
        "compute_overlaps",
        lambda *_args, **_kwargs: {"pairs": []},
    )
    monkeypatch.setattr(
        map25d_pipeline,
        "detect_tables_with_review",
        lambda *_args, **_kwargs: TableDetectionResult([], []),
    )
    monkeypatch.setattr(
        map25d_pipeline,
        "compute_alignment_report",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        map25d_pipeline,
        "render_map25d_preview",
        lambda *_args, **_kwargs: None,
    )

    output = tmp_path / "map25d_output" / "run_001"
    result = run_map25d_from_intermediate(package, output)
    assert result.map25d["type"] == "FeatureCollection"
    assert not result.objects
    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["source_intermediate"] == str(package.resolve())
    assert manifest["outputs"]["geojson"] == "map25d.geojson"


def test_registered_external_candidate_can_drive_both_businesses(
    fixture_dir,
    tmp_path,
    monkeypatch,
):
    """已验收的精确 K/D/R/t 可推导 H，并与估计候选并列运行。"""

    from store_vision.data import workspace as workspace_module

    dataset_path = tmp_path / "input" / "store"
    shutil.copytree(fixture_dir, dataset_path)
    scale_points = [
        {"rx": 0, "ry": 0, "rw": 0, "rh": 0},
        {"rx": 100, "ry": 0, "rw": 20, "rh": 0},
        {"rx": 0, "ry": 100, "rw": 0, "rh": 12},
        {"rx": 100, "ry": 100, "rw": 20, "rh": 12},
    ]
    (dataset_path / "scale.json").write_text(
        json.dumps(
            {
                "data": {
                    "calibrationPoints": json.dumps(scale_points),
                    "deviceList": [],
                }
            }
        ),
        encoding="utf-8",
    )
    dataset = load_store_folder(dataset_path)
    report = _shared_report(dataset_path)
    rotation = np.diag([1.0, -1.0, -1.0])
    cameras = {}
    for index, camera in enumerate(dataset.camera_list()):
        width, height = camera.image_size
        center = np.asarray([index * 0.5, 0.0, 2.0])
        cameras[camera.device_serial] = {
            "camera_id": camera.device_serial,
            "image_path": str(Path(camera.image_path).resolve()),
            "image_size": [width, height],
            "camera_model": "opencv_pinhole_5",
            "K": [
                [500.0, 0.0, width / 2.0],
                [0.0, 500.0, height / 2.0],
                [0.0, 0.0, 1.0],
            ],
            "D": [0.0] * 5,
            "R": rotation.tolist(),
            "t": (-rotation @ center).tolist(),
            "extrinsic_direction": "world_to_camera",
            "world_frame_id": "store_ground_world",
            "unit": "metre",
            "source": "external_calibration",
            "quality_status": "reference_candidate",
        }
    external = {
        "schema_version": 1,
        "candidate_name": "external",
        "source": "external_calibration",
        "registration": {"verified": True},
        "cameras": cameras,
    }
    monkeypatch.setattr(
        workspace_module,
        "_parse_external_candidate",
        lambda *_args, **_kwargs: external,
    )

    package = tmp_path / "intermediate" / "run_001"
    publish_intermediate_package(dataset_path, package, report)
    assert candidate_names(package) == ("estimated", "external")
    capabilities = resolve_capabilities(
        package,
        candidate_name="external",
    )
    assert capabilities.map25d.ready
    assert capabilities.stitching.ready
    manifest = json.loads(
        (package / "manifest.json").read_text(encoding="utf-8")
    )
    assert "external" in manifest["planar_projection_candidates"]


def test_discover_packages_and_sfm_derivation_keep_parent_immutable(
    fixture_dir,
    tmp_path,
):
    """SfM 完成后发布派生包，父包和基础 2.5D 能力保持不变。"""

    dataset_root = tmp_path / "input" / "store"
    shutil.copytree(fixture_dir, dataset_root)
    intermediate_root = tmp_path / "intermediate" / "store"
    base = intermediate_root / "run_001"
    publish_intermediate_package(
        dataset_root,
        base,
        _shared_report(dataset_root),
    )
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    image_rows = manifest["input"]["images"]
    image_names = [Path(row["path"]).name for row in image_rows]
    experiment = tmp_path / "sfm" / "run_001"
    reports = experiment / "reports"
    reports.mkdir(parents=True)
    (reports / "sfm_registration_summary.json").write_text(
        json.dumps(
            {
                "status": "partial_visual",
                "selected_candidate": "global",
                "visually_registered_images": image_names[:1],
                "weak_or_prior_only_images": image_names[1:],
                "unregistered_images": [],
                "per_image_point3d_observations": {
                    image_names[0]: 12,
                },
                "sparse_points": 8,
                "mean_track_length": 2.5,
                "mean_reprojection_error_px": 0.4,
            }
        ),
        encoding="utf-8",
    )

    derived = publish_sfm_derived_package(base, experiment, intermediate_root)
    assert "sfm_evidence" not in json.loads(
        (base / "manifest.json").read_text(encoding="utf-8")
    )
    assert resolve_capabilities(base).map25d.ready
    assert not resolve_capabilities(base).sfm_assisted.ready
    assert resolve_capabilities(derived).map25d.ready
    assert resolve_capabilities(derived).sfm_assisted.ready
    summaries = discover_intermediate_packages(tmp_path / "intermediate")
    assert {item.producer for item in summaries} == {
        "input_optimization",
        "sfm_derivation",
    }


def test_schema_one_manifest_remains_readable(fixture_dir, tmp_path):
    """V0.18 中间层读取器继续兼容 V0.17 发布的 schema 1 包。"""

    package = tmp_path / "intermediate" / "store" / "run_001"
    publish_intermediate_package(
        fixture_dir,
        package,
        _shared_report(fixture_dir),
    )
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert resolve_capabilities(package).map25d.ready
