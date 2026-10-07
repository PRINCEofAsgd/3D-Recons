"""云端 Phase 1 合同的正反例；全部输入来自合成夹具。"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from store_vision.cloud_workspace import (
    LocalObjectStore,
    create_dataset_snapshot,
    materialize_run,
    materialize_snapshot,
    run_local_adapted,
    stage_artifact,
    stage_intermediate,
    validate_artifact,
    validate_run,
    validate_snapshot,
)
from store_vision.data.workspace import load_intermediate_runtime, publish_intermediate_package
from store_vision.mapping.map25d_pipeline import run_map25d_from_intermediate
from store_vision.mapping.parameter_stitcher import ParameterStitchConfig, run_parameter_stitching
from tests.unit.test_workspace import _shared_report


def _gui_synthetic(fixture_dir: Path, target: Path) -> Path:
    """把既有去标识化夹具布置为 GUI 固定输入合同。"""
    target.mkdir()
    shutil.copy2(fixture_dir / "cali.txt", target / "cali.txt")
    shutil.copy2(fixture_dir / "footfallplan.png", target / "floorplan.png")
    images = target / "screenshots"
    images.mkdir()
    for image in fixture_dir.glob("*.jpg"):
        shutil.copy2(image, images / image.name)
    points = [
        {"rx": 0, "ry": 0, "rw": 0, "rh": 0},
        {"rx": 100, "ry": 0, "rw": 20, "rh": 0},
        {"rx": 0, "ry": 100, "rw": 0, "rh": 12},
        {"rx": 100, "ry": 100, "rw": 20, "rh": 12},
    ]
    (target / "scale.json").write_text(
        json.dumps({"data": {"calibrationPoints": json.dumps(points), "deviceList": []}}),
        encoding="utf-8",
    )
    return target


def test_snapshot_and_run_restore_in_new_directory(fixture_dir, tmp_path):
    """快照与 v2 中间层跨目录恢复，原拼接消费者真实读取候选。"""
    source = _gui_synthetic(fixture_dir, tmp_path / "source")
    store = LocalObjectStore(tmp_path / "store")
    snapshot = create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    with pytest.raises(ValueError, match="已发布"):
        create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    first = materialize_snapshot(store, snapshot, "synthetic", tmp_path / "first")
    package = tmp_path / "first" / "local-v2"
    publish_intermediate_package(first, package, _shared_report(first))
    run = stage_intermediate(store, package, first, snapshot, "run1", "attempt1")
    restored = materialize_run(store, run, snapshot, "synthetic", "run1", "attempt1", tmp_path / "second")
    runtime = load_intermediate_runtime(restored)
    assert runtime.capabilities.stitching.ready
    assert runtime.capabilities.map25d.ready
    assert run["coordinate_contract"]["ground_unit"] == "metre"
    assert run["candidate"] == "estimated"
    # 对外清单与存储对象均不含本机输入根目录。
    for manifest in (snapshot, run):
        assert str(tmp_path) not in json.dumps(manifest)
    for row in run["resources"]:
        assert str(tmp_path).encode() not in store.read(row)
    output = tmp_path / "second" / "stitching"
    result = run_parameter_stitching(
        restored, output,
        config=ParameterStitchConfig(
            pixels_per_metre=8.0, fallback_radius_metres=4.0,
            max_ground_distance_metres=4.0, max_canvas_long_edge=400,
            max_canvas_pixels=120_000, feather_radius_pixels=8,
            crop_margin_pixels=2,
        ),
    )
    assert result.fused_path.is_file()
    artifact = stage_artifact(store, output, run, "stitching1", "stitching")
    assert "mosaic_fused.jpg" in validate_artifact(store, artifact, run)
    wrong_run = copy.deepcopy(run)
    wrong_run["run_id"] = "run2"
    with pytest.raises(ValueError, match="run/attempt"):
        stage_artifact(store, output, wrong_run, "wrongrun", "stitching")
    assert not (store.root / "manifests" / "artifacts" / "synthetic" / "run2").exists()
    map_output = tmp_path / "second" / "map25d"
    map_result = run_map25d_from_intermediate(restored, map_output)
    assert map_result.map25d["type"] == "FeatureCollection"
    map_artifact = stage_artifact(store, map_output, run, "map25d1", "map25d")
    assert "map25d.geojson" in validate_artifact(store, map_artifact, run)
    for artifact in (artifact, map_artifact):
        assert str(tmp_path) not in json.dumps(artifact)
        for row in artifact["files"]:
            if row["media_type"] in {"application/json", "application/geo+json"}:
                assert str(tmp_path).encode() not in store.read(row)


def test_snapshot_rejects_bad_schema_identity_path_hash_and_camera(fixture_dir, tmp_path):
    """合同错误在物化前失败，也不会写出输入目录。"""
    source = _gui_synthetic(fixture_dir, tmp_path / "source")
    store = LocalObjectStore(tmp_path / "store")
    snapshot = create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    variants = []
    bad = copy.deepcopy(snapshot)
    bad["schema_version"] = "store-vision-cloud/99"
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    bad["dataset_id"] = "other"
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    bad["files"][0]["path"] = "../escape.json"
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    bad["files"][0]["path"] = "C:/escape.json"
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    bad["files"][0]["sha256"] = "0" * 64
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    bad["files"][0]["uri"] = "svlocal://objects/" + "f" * 64
    bad["files"][0]["sha256"] = "f" * 64
    variants.append(bad)
    bad = copy.deepcopy(snapshot)
    images = [row for row in bad["files"] if row["role"] == "image"]
    images[1]["camera_id"] = images[0]["camera_id"]
    variants.append(bad)
    for index, variant in enumerate(variants):
        target = tmp_path / f"bad-{index}"
        with pytest.raises(ValueError):
            materialize_snapshot(store, variant, "synthetic", target)
        assert not (target / "input").exists()


def test_run_rejects_candidate_parent_and_missing_resource(fixture_dir, tmp_path):
    """不能把别的候选、父快照或缺资源的 run 当作已发布。"""
    source = _gui_synthetic(fixture_dir, tmp_path / "source")
    store = LocalObjectStore(tmp_path / "store")
    snapshot = create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    input_root = materialize_snapshot(store, snapshot, "synthetic", tmp_path / "scratch")
    package = tmp_path / "scratch" / "local-v2"
    publish_intermediate_package(input_root, package, _shared_report(input_root))
    run = stage_intermediate(store, package, input_root, snapshot, "run1", "attempt1")
    variants = []
    bad = copy.deepcopy(run)
    bad["candidate"] = "external"
    variants.append(bad)
    bad = copy.deepcopy(run)
    bad["snapshot_id"] = "other"
    variants.append(bad)
    bad = copy.deepcopy(run)
    bad["resources"].pop()
    variants.append(bad)
    bad = copy.deepcopy(run)
    bad["run_id"] = "other"
    variants.append(bad)
    bad = copy.deepcopy(run)
    bad["attempt_id"] = "other"
    variants.append(bad)
    for variant in variants:
        with pytest.raises(ValueError):
            validate_run(store, variant, snapshot, "synthetic", "run1", "attempt1")
    assert validate_snapshot(store, snapshot, "synthetic")


def test_local_adapter_publishes_both_artifacts(fixture_dir, tmp_path):
    """统一适配入口按 run/attempt 隔离两个消费者的正式结果。"""
    source = _gui_synthetic(fixture_dir, tmp_path / "source")
    store = LocalObjectStore(tmp_path / "store")
    snapshot = create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    run, map_artifact, stitching_artifact = run_local_adapted(
        store, snapshot, "run1", "attempt1", _shared_report(source), tmp_path / "scratch",
        stitch_config=ParameterStitchConfig(
            pixels_per_metre=8.0, fallback_radius_metres=4.0,
            max_ground_distance_metres=4.0, max_canvas_long_edge=400,
            max_canvas_pixels=120_000, feather_radius_pixels=8,
            crop_margin_pixels=2,
        ),
    )
    assert map_artifact["parents"][0]["run_id"] == "run1"
    assert "map25d.geojson" in validate_artifact(store, map_artifact, run)
    assert "mosaic_fused.jpg" in validate_artifact(store, stitching_artifact, run)
