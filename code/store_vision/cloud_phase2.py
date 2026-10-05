"""Phase 2 命令入口：快照上传与独立执行器的单次算法运行。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from store_vision.calibration.shared_calibration import run_shared_intrinsics_calibration
from store_vision.cloud_phase1 import (create_dataset_snapshot, materialize_run,
                                       materialize_snapshot, stage_artifact,
                                       stage_intermediate, validate_artifact,
                                       validate_run, validate_snapshot, _identity)
from store_vision.cloud_s3 import S3ObjectStore, StorageUnavailable
from store_vision.data.workspace import publish_intermediate_package
from store_vision.mapping.map25d_pipeline import run_map25d_from_intermediate
from store_vision.mapping.parameter_stitcher import ParameterStitchConfig, run_parameter_stitching


def upload_snapshot(store: S3ObjectStore, source: Path, dataset_id: str, snapshot_id: str) -> str:
    """预检并冻结输入；失败时仅留下不可见对象供清理。"""
    store.scope = f"snapshots/{_identity(dataset_id, 'dataset_id')}/{_identity(snapshot_id, 'snapshot_id')}"
    create_dataset_snapshot(store, source, dataset_id, snapshot_id)
    return store.manifest_uri("snapshots", dataset_id, snapshot_id)


def execute(store: S3ObjectStore, snapshot_uri: str, run_id: str, attempt_id: str,
            scratch: Path, report_json: Path | None = None) -> dict:
    """独立 scratch 中调用现有算法，全部校验后才创建发布指针。"""
    snapshot = store.load_manifest(snapshot_uri)
    dataset_id = snapshot["dataset_id"]
    _identity(dataset_id, "dataset_id")
    _identity(run_id, "run_id")
    _identity(attempt_id, "attempt_id")
    if snapshot_uri != store.manifest_uri("snapshots", dataset_id, snapshot["snapshot_id"]):
        raise ValueError("快照 URI 与清单身份不一致")
    validate_snapshot(store, snapshot, dataset_id)
    store.scope = f"attempts/{dataset_id}/{run_id}/{attempt_id}"
    input_root = materialize_snapshot(store, snapshot, dataset_id, scratch)
    if report_json is None:
        report = run_shared_intrinsics_calibration(input_root, scratch / "calibration")
    else:
        # 仅用于合成验收；生产任务不传此参数，始终运行原共享标定入口。
        report = json.loads(report_json.read_text(encoding="utf-8"))
    package = scratch / "work" / "intermediate"
    publish_intermediate_package(input_root, package, report)
    run = stage_intermediate(store, package, input_root, snapshot, run_id, attempt_id)
    validate_run(store, run, snapshot, dataset_id, run_id, attempt_id)
    restored = materialize_run(store, run, snapshot, dataset_id, run_id, attempt_id, scratch / "consumer")
    artifacts = {}
    for workflow in ("map25d", "stitching"):
        output = scratch / "work" / workflow
        if workflow == "map25d":
            run_map25d_from_intermediate(restored, output, candidate_name=run["candidate"])
        else:
            stitch_config = ParameterStitchConfig(pixels_per_metre=8.0,
                fallback_radius_metres=4.0, max_ground_distance_metres=4.0,
                max_canvas_long_edge=400, max_canvas_pixels=120_000,
                feather_radius_pixels=8, crop_margin_pixels=2)
            ray_address = os.getenv("SV_RAY_ADDRESS")
            if ray_address:
                # Phase 3 可选计算面：远端 worker 按快照资源 URI 读取各自原图。
                import ray
                from store_vision.cloud_ray import RayResources, run_ray_stitching
                ray.init(address=ray_address, _temp_dir=os.getenv("SV_RAY_TEMP_DIR"))
                try:
                    images = {row["camera_id"]: row for row in snapshot["files"] if row["role"] == "image"}
                    run_ray_stitching(restored, output, job_id=run_id, run_id=run_id,
                        attempt_id=attempt_id, candidate_name=run["candidate"],
                        image_resources=images, store_spec={"type": "s3"},
                        config=stitch_config,
                        resources=RayResources(
                            num_cpus=float(os.getenv("SV_RAY_TASK_CPUS", "1")),
                            num_gpus=float(os.getenv("SV_RAY_TASK_GPUS", "0")),
                            memory_bytes=int(os.getenv("SV_RAY_TASK_MEMORY_BYTES", str(256 * 1024 * 1024))),
                            max_in_flight=int(os.getenv("SV_RAY_MAX_IN_FLIGHT", "2")),
                            max_attempts=int(os.getenv("SV_RAY_MAX_ATTEMPTS", "2")),
                        ))
                finally:
                    ray.shutdown()
            else:
                run_parameter_stitching(restored, output, candidate_name=run["candidate"],
                                        config=stitch_config)
        artifact = stage_artifact(store, output, run, workflow, workflow)
        validate_artifact(store, artifact, run)
        artifacts[workflow] = store.manifest_uri("artifacts", dataset_id, run_id, attempt_id, workflow)
    publication = {"schema_version": "store-vision-cloud/1", "kind": "publication",
                   "dataset_id": dataset_id, "run_id": run_id, "attempt_id": attempt_id,
                   "snapshot_uri": snapshot_uri,
                   "run_manifest_uri": store.manifest_uri("runs", dataset_id, run_id, attempt_id),
                   "artifacts": artifacts}
    publication_uri = store.publish("published", (dataset_id, run_id, attempt_id), publication)
    return {"publication_uri": publication_uri, "run_manifest_uri": publication["run_manifest_uri"],
            "artifacts": artifacts}


def main() -> None:
    """供人工注册输入及 Go 过渡执行器调用。"""
    parser = argparse.ArgumentParser(description="Store Vision 云端 Phase 2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bucket")
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--dataset-id", required=True)
    snapshot.add_argument("--snapshot-id", required=True)
    run = sub.add_parser("execute")
    run.add_argument("--snapshot-uri", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--scratch", type=Path, required=True)
    run.add_argument("--report-json", type=Path)
    args = parser.parse_args()
    store = S3ObjectStore.from_env()
    if args.command == "bucket":
        store.create_bucket()
        result = {"bucket": store.bucket}
    elif args.command == "snapshot":
        result = {"snapshot_uri": upload_snapshot(store, args.source, args.dataset_id, args.snapshot_id)}
    else:
        args.scratch.mkdir(parents=True, exist_ok=False)
        result = execute(store, args.snapshot_uri, args.run_id, args.attempt_id,
                         args.scratch, args.report_json)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except StorageUnavailable as exc:
        # 75 告诉 Go 执行器这是可重试的对象存储暂态故障。
        print(f"temporary storage failure: {exc}", file=sys.stderr)
        raise SystemExit(75) from exc
