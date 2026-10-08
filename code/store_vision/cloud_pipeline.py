"""Phase 4 容器阶段入口：只以身份和已发布清单连接不同 Pod。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from store_vision import __version__
from store_vision.calibration.shared_calibration import run_shared_intrinsics_calibration
from store_vision.cloud_workspace import (
    _identity, _json_bytes, materialize_run, materialize_snapshot, stage_artifact,
    stage_intermediate, validate_artifact, validate_run, validate_snapshot,
)
from store_vision.data.workspace import publish_intermediate_package
from store_vision.mapping.map25d_pipeline import run_map25d_from_intermediate
from store_vision.mapping.parameter_stitcher import ParameterStitchConfig, run_parameter_stitching
from store_vision.s3_object_store import S3ObjectStore, StorageUnavailable


def manifest_hash(manifest: dict[str, Any]) -> str:
    """与对象存储发布字节一致的哈希，供跨 Pod 固定不可变版本。"""
    payload = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def snapshot_for(store: S3ObjectStore, dataset: str, snapshot_uri: str) -> dict[str, Any]:
    """验证快照所属命名空间及其全部输入对象。"""
    _identity(dataset, "dataset_id")
    snapshot = store.load_manifest(snapshot_uri)
    snapshot_id = _identity(snapshot.get("snapshot_id"), "snapshot_id")
    if snapshot_uri != store.manifest_uri("snapshots", dataset, snapshot_id):
        raise ValueError("快照 URI 与数据集身份不一致")
    validate_snapshot(store, snapshot, dataset)
    return snapshot


def run_for(store: S3ObjectStore, snapshot: dict[str, Any], run_id: str,
            attempt: str, candidate: str, uri: str, digest: str) -> dict[str, Any]:
    """拒绝错误数据集、候选、参数版本及被替换的 run 清单。"""
    dataset = snapshot["dataset_id"]
    if uri != store.manifest_uri("runs", dataset, run_id, attempt):
        raise ValueError("run URI 与固定身份不一致")
    run = store.load_manifest(uri)
    if manifest_hash(run) != digest or run.get("candidate") != candidate:
        raise ValueError("run 哈希或候选不一致")
    if run.get("algorithm_version") != __version__:
        raise ValueError("run 算法/参数版本不一致")
    validate_run(store, run, snapshot, dataset, run_id, attempt)
    return run


def verify_snapshot(store: S3ObjectStore, dataset: str, uri: str) -> dict[str, str]:
    """DAG 首节点不发布新资源，只校验冻结输入。"""
    snapshot_for(store, dataset, uri)
    return {"status": "succeeded", "snapshot_uri": uri}


def calibrate(store: S3ObjectStore, dataset: str, run_id: str, attempt: str,
              candidate: str, snapshot_uri: str, scratch: Path,
              synthetic_report: Path | None = None) -> dict[str, str]:
    """全局共享标定只运行一次；重放时校验并复用同身份的已发布 run。"""
    snapshot = snapshot_for(store, dataset, snapshot_uri)
    run_uri = store.manifest_uri("runs", dataset, run_id, attempt)
    try:
        previous = store.load_manifest(run_uri)
    except ValueError as exc:
        if "对象不存在" not in str(exc):
            raise
    else:
        digest = manifest_hash(previous)
        run_for(store, snapshot, run_id, attempt, candidate, run_uri, digest)
        return {"status": "succeeded", "run_manifest_uri": run_uri,
                "run_sha256": digest, "reused": "true"}
    store.scope = f"attempts/{dataset}/{run_id}/{attempt}/calibration"
    input_root = materialize_snapshot(store, snapshot, dataset, scratch)
    if synthetic_report is None:
        report = run_shared_intrinsics_calibration(input_root, scratch / "calibration")
    else:
        # 合成验收专用；报告只在标定 Pod 读取，不作为跨阶段本地路径传递。
        report = json.loads(synthetic_report.read_text(encoding="utf-8"))
    package = scratch / "work" / "intermediate"
    publish_intermediate_package(input_root, package, report)
    run = stage_intermediate(store, package, input_root, snapshot, run_id, attempt, candidate)
    validate_run(store, run, snapshot, dataset, run_id, attempt)
    return {"status": "succeeded", "run_manifest_uri": run_uri,
            "run_sha256": manifest_hash(run), "reused": "false"}


def stitch_config(profile: str) -> ParameterStitchConfig:
    """画布参数版本由显式 profile 固定，防止同一 attempt 换参复用。"""
    if profile == "default":
        return ParameterStitchConfig()
    if profile == "synthetic-small":
        return ParameterStitchConfig(pixels_per_metre=8.0,
            fallback_radius_metres=4.0, max_ground_distance_metres=4.0,
            max_canvas_long_edge=400, max_canvas_pixels=120_000,
            feather_radius_pixels=8, crop_margin_pixels=2)
    raise ValueError("未知拼接参数版本")


def artifact_parameters_match(artifact: dict[str, Any], candidate: str,
                              branch: str, profile: str) -> bool:
    """复用和汇总时复核业务参数，避免同一 attempt 换画布配置。"""
    config = asdict(stitch_config(profile)) if branch == "stitching" else None
    mode = "baseline" if branch == "map25d" else None
    expected = hashlib.sha256(_json_bytes({"candidate": candidate, "workflow": branch,
                                           "config": config, "mode": mode})).hexdigest()
    return artifact.get("parameters_digest") == expected


def run_branch(store: S3ObjectStore, branch: str, dataset: str, run_id: str,
               attempt: str, candidate: str, snapshot_uri: str, run_uri: str,
               run_sha256: str, scratch: Path, profile: str = "default") -> dict[str, str]:
    """两个消费者各自恢复同一 run，并写到彼此独立的对象前缀。"""
    if branch not in {"map25d", "stitching"}:
        raise ValueError("未知业务分支")
    snapshot = snapshot_for(store, dataset, snapshot_uri)
    run = run_for(store, snapshot, run_id, attempt, candidate, run_uri, run_sha256)
    capability = run["capabilities"][f"{branch}_ready"]
    if not capability["ready"]:
        return {"status": "blocked", "reason": ",".join(capability["missing"])}
    artifact_uri = store.manifest_uri("artifacts", dataset, run_id, attempt, branch)
    try:
        previous = store.load_manifest(artifact_uri)
    except ValueError as exc:
        if "对象不存在" not in str(exc):
            raise
    else:
        validate_artifact(store, previous, run)
        if not artifact_parameters_match(previous, candidate, branch, profile):
            raise ValueError("已发布业务结果的参数版本不一致")
        return {"status": "succeeded", "artifact_uri": artifact_uri, "reused": "true"}
    store.scope = f"attempts/{dataset}/{run_id}/{attempt}/{branch}"
    restored = materialize_run(store, run, snapshot, dataset, run_id, attempt, scratch)
    output = scratch / "output" / branch
    if branch == "map25d":
        run_map25d_from_intermediate(restored, output, candidate_name=candidate)
    else:
        config = stitch_config(profile)
        ray_address = os.getenv("SV_RAY_ADDRESS")
        if os.getenv("SV_REQUIRE_RAY") == "1" and not ray_address:
            raise ValueError("Argo 拼接阶段要求 SV_RAY_ADDRESS")
        if ray_address:
            import ray
            from store_vision.ray_stitching import RayResources, run_ray_stitching
            ray.init(address=ray_address, _temp_dir=os.getenv("SV_RAY_TEMP_DIR"))
            try:
                images = {row["camera_id"]: row for row in snapshot["files"] if row["role"] == "image"}
                run_ray_stitching(restored, output, job_id=run_id, run_id=run_id,
                    attempt_id=attempt, candidate_name=candidate, image_resources=images,
                    store_spec={"type": "s3"}, config=config,
                    resources=RayResources(
                        num_cpus=float(os.getenv("SV_RAY_TASK_CPUS", "1")),
                        num_gpus=float(os.getenv("SV_RAY_TASK_GPUS", "0")),
                        memory_bytes=int(os.getenv("SV_RAY_TASK_MEMORY_BYTES", str(256 * 1024 * 1024))),
                        max_in_flight=int(os.getenv("SV_RAY_MAX_IN_FLIGHT", "2")),
                        max_attempts=int(os.getenv("SV_RAY_MAX_ATTEMPTS", "2"))))
            finally:
                ray.shutdown()
        else:
            run_parameter_stitching(restored, output, candidate_name=candidate, config=config)
    stage_artifact(store, output, run, branch, branch)
    validate_artifact(store, store.load_manifest(artifact_uri), run)
    return {"status": "succeeded", "artifact_uri": artifact_uri, "reused": "false"}


def summarize(store: S3ObjectStore, dataset: str, run_id: str, attempt: str,
              candidate: str, snapshot_uri: str, run_uri: str, run_sha256: str,
              map_phase: str, stitch_phase: str, profile: str = "default") -> dict[str, Any]:
    """双分支独立结算；能力就绪的分支均为必需，blocked 留下原因。"""
    snapshot = snapshot_for(store, dataset, snapshot_uri)
    run = run_for(store, snapshot, run_id, attempt, candidate, run_uri, run_sha256)
    publication_uri = store.manifest_uri("published", dataset, run_id, attempt)
    try:
        previous = store.load_manifest(publication_uri)
    except ValueError as exc:
        if "对象不存在" not in str(exc):
            raise
    else:
        if (previous.get("dataset_id"), previous.get("run_id"), previous.get("attempt_id"),
                previous.get("run_sha256")) != (dataset, run_id, attempt, run_sha256) or previous.get("stitch_profile") != profile:
            raise ValueError("已有汇总清单身份或哈希不一致")
        for branch, uri in previous.get("artifacts", {}).items():
            if branch not in {"map25d", "stitching"} or uri != store.manifest_uri("artifacts", dataset, run_id, attempt, branch):
                raise ValueError("已有汇总业务 URI 不属于本次运行")
            artifact = store.load_manifest(uri)
            validate_artifact(store, artifact, run)
            if not artifact_parameters_match(artifact, candidate, branch, profile):
                raise ValueError("已有汇总业务参数版本不一致")
        return previous
    stages: dict[str, dict[str, str]] = {}
    artifacts: dict[str, str] = {}
    for branch, phase in (("map25d", map_phase), ("stitching", stitch_phase)):
        capability = run["capabilities"][f"{branch}_ready"]
        if not capability["ready"]:
            stages[branch] = {"status": "blocked", "reason": ",".join(capability["missing"])}
            continue
        if phase != "Succeeded":
            stages[branch] = {"status": "failed", "reason": f"Argo node: {phase}"}
            continue
        uri = store.manifest_uri("artifacts", dataset, run_id, attempt, branch)
        try:
            artifact = store.load_manifest(uri)
            validate_artifact(store, artifact, run)
            if not artifact_parameters_match(artifact, candidate, branch, profile):
                raise ValueError("业务参数版本不一致")
        except (ValueError, KeyError) as exc:
            stages[branch] = {"status": "failed", "reason": f"artifact validation: {exc}"}
        else:
            stages[branch] = {"status": "succeeded"}
            artifacts[branch] = uri
    # stitching 永远为必需；2.5D 在能力 ready 时也必须成功。
    success = stages["stitching"]["status"] == "succeeded" and stages["map25d"]["status"] in {"succeeded", "blocked"}
    summary = {"schema_version": "store-vision-cloud/1", "kind": "pipeline_summary",
        "dataset_id": dataset, "run_id": run_id, "attempt_id": attempt,
        "candidate": candidate, "algorithm_version": __version__, "stitch_profile": profile,
        "snapshot_uri": snapshot_uri, "run_manifest_uri": run_uri,
        "run_sha256": run_sha256, "status": "succeeded" if success else "failed",
        "stages": stages, "artifacts": artifacts,
        "publication_uri": publication_uri}
    store.publish("published", (dataset, run_id, attempt), summary)
    return summary


def main() -> None:
    """Argo 每个节点单独调用；结果文件供 outputs.parameters 读取。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("verify", "calibrate", "map25d", "stitching", "summarize"))
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--snapshot-uri", required=True)
    parser.add_argument("--run-uri")
    parser.add_argument("--run-sha256")
    parser.add_argument("--scratch", type=Path, default=Path("/tmp/store-vision"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/stage-result.json"))
    parser.add_argument("--run-uri-output", type=Path)
    parser.add_argument("--run-sha-output", type=Path)
    parser.add_argument("--synthetic-report", type=Path)
    parser.add_argument("--stitch-profile", default="default")
    parser.add_argument("--map-phase")
    parser.add_argument("--stitch-phase")
    args = parser.parse_args()
    for value, label in ((args.dataset_id, "dataset_id"), (args.run_id, "run_id"),
                         (args.attempt_id, "attempt_id"), (args.candidate, "candidate")):
        _identity(value, label)
    store = S3ObjectStore.from_env()
    if args.stage == "verify":
        result = verify_snapshot(store, args.dataset_id, args.snapshot_uri)
    elif args.stage == "calibrate":
        report = None
        if os.getenv("SV_USE_SYNTHETIC_REPORT") == "1":
            if not args.synthetic_report or not args.synthetic_report.is_file():
                raise ValueError("合成报告模式已启用，但报告文件不存在")
            report = args.synthetic_report
        result = calibrate(store, args.dataset_id, args.run_id, args.attempt_id,
                           args.candidate, args.snapshot_uri, args.scratch, report)
    else:
        if not args.run_uri or not args.run_sha256:
            parser.error("下游阶段需要 --run-uri 与 --run-sha256")
        if args.stage == "summarize":
            result = summarize(store, args.dataset_id, args.run_id, args.attempt_id,
                args.candidate, args.snapshot_uri, args.run_uri, args.run_sha256,
                args.map_phase or "Unknown", args.stitch_phase or "Unknown", args.stitch_profile)
        else:
            result = run_branch(store, args.stage, args.dataset_id, args.run_id,
                args.attempt_id, args.candidate, args.snapshot_uri, args.run_uri,
                args.run_sha256, args.scratch, args.stitch_profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if args.run_uri_output and "run_manifest_uri" in result:
        args.run_uri_output.write_text(result["run_manifest_uri"], encoding="utf-8")
    if args.run_sha_output and "run_sha256" in result:
        args.run_sha_output.write_text(result["run_sha256"], encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except StorageUnavailable as exc:
        print(f"temporary storage failure: {exc}", file=sys.stderr)
        raise SystemExit(75) from exc
