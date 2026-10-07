"""Phase 3 合成样例：真实 Ray 多进程、串并行对照与分片重试。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import pytest

from store_vision.cloud_workspace import LocalObjectStore
from store_vision.ray_stitching import (
    RayResources, TASK_VERSION, _digest, _one_camera_task,
    _validated_part, run_ray_stitching,
)
from store_vision.data.workspace import load_intermediate_runtime, publish_intermediate_package
from store_vision.mapping.parameter_stitcher import (
    ParameterStitchConfig, prepare_stitching_runtime, run_parameter_stitching,
)
from tests.unit.test_workspace import _shared_report


@pytest.fixture
def ray_temp_dir():
    """每个测试独占 Ray 会话目录，避免连接此前验收留下的集群地址。"""

    # macOS 使用短路径避开 Unix socket 长度限制；其他平台使用系统临时目录。
    path = Path(tempfile.mkdtemp(prefix="svray-", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.timeout(180)
def test_ray_parts_match_serial_and_retry_only_failed_camera(fixture_dir, tmp_path, ray_temp_dir):
    """固定相机次序使整型像素、覆盖和统计都可要求完全相等。"""

    ray = pytest.importorskip("ray")
    package = tmp_path / "intermediate"
    publish_intermediate_package(fixture_dir, package, _shared_report(fixture_dir))
    config = ParameterStitchConfig(
        pixels_per_metre=8.0, fallback_radius_metres=4.0,
        max_ground_distance_metres=4.0, max_canvas_long_edge=400,
        max_canvas_pixels=120_000, feather_radius_pixels=8,
        crop_margin_pixels=2,
    )
    started = time.perf_counter()
    serial = run_parameter_stitching(package, tmp_path / "serial", config=config)
    serial_seconds = time.perf_counter() - started
    runtime = load_intermediate_runtime(package)
    store = LocalObjectStore(tmp_path / "objects")
    images = {cid: store.put(Path(camera["image_path"]).read_bytes(), "image/jpeg")
              for cid, camera in runtime.camera_rig["cameras"].items()}
    failed_camera = next(iter(images))
    started = time.perf_counter()
    ray.init(num_cpus=2, include_dashboard=False, _temp_dir=str(ray_temp_dir))
    ray_start_seconds = time.perf_counter() - started
    try:
        with pytest.raises(RuntimeError, match="重试耗尽"):
            run_ray_stitching(
                package, tmp_path / "failed", job_id="job1", run_id="run1",
                attempt_id="failed1", image_resources=images,
                store_spec={"type": "local", "root": str(store.root)},
                config=config, resources=RayResources(max_in_flight=2, max_attempts=1),
                fail_once_camera=failed_camera,
            )
        assert not (tmp_path / "failed" / "manifest.json").exists()
        started = time.perf_counter()
        parallel, manifests = run_ray_stitching(
            package, tmp_path / "parallel", job_id="job1", run_id="run1",
            attempt_id="attempt1", image_resources=images,
            store_spec={"type": "local", "root": str(store.root)},
            config=config, resources=RayResources(max_in_flight=2),
            fail_once_camera=failed_camera,
        )
        ray_seconds = time.perf_counter() - started
        with pytest.raises(ValueError, match="已有产物"):
            run_ray_stitching(
                package, tmp_path / "parallel", job_id="job1", run_id="run1",
                attempt_id="attempt2", image_resources=images,
                store_spec={"type": "local", "root": str(store.root)},
                config=config, resources=RayResources(max_in_flight=2),
            )
    finally:
        ray.shutdown()
    assert serial.summary == parallel.summary
    serial_manifest = json.loads(serial.report_path.read_text())
    parallel_manifest = json.loads(parallel.report_path.read_text())
    assert serial_manifest["working_canvas"] == parallel_manifest["working_canvas"]
    assert serial_manifest["crop"] == parallel_manifest["crop"]
    assert serial_manifest["cameras"] == parallel_manifest["cameras"]
    for name in ("mosaic_fused.jpg", "mosaic_alpha.jpg", "overlap_count.png"):
        first = cv2.imread(str(serial.output_dir / name), cv2.IMREAD_UNCHANGED)
        second = cv2.imread(str(parallel.output_dir / name), cv2.IMREAD_UNCHANGED)
        assert np.array_equal(first, second), name
    assert len({row["worker_pid"] for row in manifests}) >= 2
    assert all(row["worker_pid"] != os.getpid() for row in manifests)
    retried = next(row for row in manifests if row["camera_id"] == failed_camera)
    assert retried["part_attempt"] == "attempt1-1"
    assert all(row["part_attempt"] == "attempt1-0" for row in manifests if row["camera_id"] != failed_camera)
    assert len(manifests) == len(images)
    print(f"phase3 synthetic timing: serial={serial_seconds:.3f}s ray_start={ray_start_seconds:.3f}s ray_run_with_retry={ray_seconds:.3f}s")


def test_ray_contract_error_precedes_output(fixture_dir, tmp_path, ray_temp_dir):
    """相机集合错误属于不可重试合同错误，输出目录不可见。"""

    package = tmp_path / "intermediate"
    publish_intermediate_package(fixture_dir, package, _shared_report(fixture_dir))
    with pytest.raises(ValueError, match="相机集合"):
        # 使用测试替身只检查驱动端合同，避免依赖真实对象读取。
        import ray
        ray.init(num_cpus=1, include_dashboard=False, _temp_dir=str(ray_temp_dir))
        try:
            run_ray_stitching(package, tmp_path / "bad", job_id="job1", run_id="run1",
                              attempt_id="attempt1", image_resources={},
                              store_spec={"type": "local", "root": str(tmp_path / "objects")})
        finally:
            ray.shutdown()
    assert not (tmp_path / "bad").exists()


def test_part_manifest_rejects_bad_identity_checksum_and_reuse(fixture_dir, tmp_path):
    """独立 attempt 只创建一次；坏合同和坏字节在 reduce 前被拒绝。"""

    package = tmp_path / "intermediate"
    publish_intermediate_package(fixture_dir, package, _shared_report(fixture_dir))
    config = ParameterStitchConfig(pixels_per_metre=8, max_canvas_long_edge=400,
                                   max_canvas_pixels=120_000)
    runtime, canvas = prepare_stitching_runtime(package, None, config)
    camera = next(iter(runtime.camera_rig["cameras"].values()))
    cid = camera["camera_id"]
    store = LocalObjectStore(tmp_path / "objects")
    image = store.put(Path(camera["image_path"]).read_bytes(), "image/jpeg")
    spec = {"type": "local", "root": str(store.root)}
    identity = {"job_id": "job1", "run_id": "run1", "candidate": runtime.candidate_name,
                "canvas_hash": _digest(asdict(canvas)), "camera_id": cid,
                "task_version": TASK_VERSION}
    manifest = _one_camera_task(camera, asdict(canvas), asdict(config), image,
                                spec, identity, "attempt1-0", False)
    assert _validated_part(manifest, identity, image, camera, canvas, spec).camera_id == cid
    with pytest.raises(FileExistsError):
        _one_camera_task(camera, asdict(canvas), asdict(config), image,
                         spec, identity, "attempt1-0", False)
    wrong = {**identity, "canvas_hash": "0" * 64}
    with pytest.raises(ValueError, match="合同"):
        _validated_part(manifest, wrong, image, camera, canvas, spec)
    part_path = Path(store.root) / "attempts" / "job1" / "run1" / runtime.candidate_name / identity["canvas_hash"] / cid / TASK_VERSION.replace("/", "_") / "attempt1-0" / "part.npz"
    part_path.write_bytes(part_path.read_bytes() + b"bad")
    with pytest.raises(ValueError, match="SHA-256"):
        _validated_part(manifest, identity, image, camera, canvas, spec)
