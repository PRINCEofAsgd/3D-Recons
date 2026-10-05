"""Phase 3：逐相机 Ray 分片、校验屏障和串行全局汇总。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from store_vision import __version__
from store_vision.cloud_phase1 import LocalObjectStore, _identity
from store_vision.cloud_s3 import S3ObjectStore, StorageUnavailable
from store_vision.mapping.parameter_stitcher import (
    CameraStitchPart, ParameterStitchConfig, WorldCanvas,
    prepare_camera_stitch_part, prepare_stitching_runtime,
    run_parameter_stitching,
)

SCHEMA = "store-vision-stitch-part/1"
TASK_VERSION = f"parameter-stitch-part/{__version__}"


@dataclass(frozen=True)
class RayResources:
    """每 task 的资源和驱动端最大在途数；内存以字节计。"""

    num_cpus: float = 1.0
    num_gpus: float = 0.0
    memory_bytes: int = 256 * 1024 * 1024
    max_in_flight: int = 2
    max_attempts: int = 2

    def validate(self) -> None:
        if self.num_cpus <= 0 or self.num_gpus < 0 or self.memory_bytes <= 0:
            raise ValueError("Ray CPU/GPU/内存资源声明非法")
        if self.max_in_flight < 1 or self.max_attempts < 1:
            raise ValueError("Ray 最大在途数与重试数必须为正整数")


def _digest(value: Any) -> str:
    """使用规范 JSON 固定合同摘要，不依赖字典插入顺序。"""

    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _calibration_digest(camera: dict[str, Any]) -> str:
    """标定摘要只绑定几何参数，不受 worker 本地物化路径影响。"""

    keys = ("camera_id", "camera_model", "K", "D", "R", "t",
            "image_size", "world_frame_id", "unit")
    return _digest({key: camera.get(key) for key in keys})


def _store(spec: dict[str, str], scope: str | None = None) -> Any:
    """worker 自行构造存储客户端；Ray 参数不携带凭据或图像大对象。"""

    if spec["type"] == "s3":
        store = S3ObjectStore.from_env()
        if scope:
            store.scope = scope
        return store
    if spec["type"] == "local":
        return LocalObjectStore(spec["root"])
    raise ValueError("未知分片存储类型")


def _part_scope(identity: dict[str, str], part_attempt: str) -> str:
    """每次执行独占 attempt 前缀；失败重试不复写前次对象。"""

    return "/".join((
        "attempts", identity["job_id"], identity["run_id"],
        identity["candidate"], identity["canvas_hash"],
        identity["camera_id"], identity["task_version"].replace("/", "_"),
        part_attempt,
    ))


def _write_part(store: Any, spec: dict[str, str], scope: str, data: bytes) -> dict[str, Any]:
    """分片先落对象存储；本地替身同样按 attempt 不可覆盖。"""

    if spec["type"] == "s3":
        return store.put(data, "application/x-npz")
    path = Path(spec["root"]) / scope / "part.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path(spec["root"]).resolve()):
        raise ValueError("本地分片写入路径越界")
    with path.open("xb") as stream:
        stream.write(data)
    return {"uri": "svpart://" + scope + "/part.npz", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "media_type": "application/x-npz"}


def _read_part(store: Any, spec: dict[str, str], scope: str, resource: dict[str, Any]) -> bytes:
    """reduce 前重新下载并核对身份、大小和 SHA-256。"""

    if spec["type"] == "s3":
        if f"/{scope}/objects/" not in resource.get("uri", ""):
            raise ValueError("分片 URI 不属于目标 attempt")
        return store.read(resource)
    if resource.get("uri") != "svpart://" + scope + "/part.npz":
        raise ValueError("分片 URI 不属于目标 attempt")
    path = Path(spec["root"]) / scope / "part.npz"
    if path.is_symlink() or not path.resolve().is_relative_to(Path(spec["root"]).resolve()):
        raise ValueError("本地分片路径越界")
    data = path.read_bytes()
    if len(data) != resource.get("bytes") or hashlib.sha256(data).hexdigest() != resource.get("sha256"):
        raise ValueError("分片大小或 SHA-256 不符")
    return data


def _write_part_manifest(store: Any, spec: dict[str, str], scope: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """分片清单与数据存于同一独立 attempt，供驱动端二次核验。"""

    data = (json.dumps(manifest, sort_keys=True, ensure_ascii=False) + "\n").encode()
    if spec["type"] == "s3":
        return store.put(data, "application/json")
    path = Path(spec["root"]) / scope / "manifest.json"
    if path.is_symlink() or not path.parent.resolve().is_relative_to(Path(spec["root"]).resolve()):
        raise ValueError("本地分片清单写入路径越界")
    with path.open("xb") as stream:
        stream.write(data)
    return {"uri": "svpart://" + scope + "/manifest.json", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "media_type": "application/json"}


def _read_part_manifest(spec: dict[str, str], scope: str, resource: dict[str, Any]) -> dict[str, Any]:
    """只能读取任务返回的目标 attempt 清单，不能扫描或选择半成品。"""

    if spec["type"] == "s3":
        if f"/{scope}/objects/" not in resource.get("uri", ""):
            raise ValueError("分片清单 URI 不属于目标 attempt")
        data = _store(spec).read(resource)
    else:
        if resource.get("uri") != "svpart://" + scope + "/manifest.json":
            raise ValueError("分片清单 URI 不属于目标 attempt")
        path = Path(spec["root"]) / scope / "manifest.json"
        if path.is_symlink() or not path.resolve().is_relative_to(Path(spec["root"]).resolve()):
            raise ValueError("本地分片清单路径越界")
        data = path.read_bytes()
        if len(data) != resource.get("bytes") or hashlib.sha256(data).hexdigest() != resource.get("sha256"):
            raise ValueError("分片清单大小或 SHA-256 不符")
    return json.loads(data)


def _one_camera_task(
    camera: dict[str, Any], canvas_data: dict[str, Any], config_data: dict[str, Any],
    image_resource: dict[str, Any], store_spec: dict[str, str],
    identity: dict[str, str], part_attempt: str, fail_once: bool,
) -> dict[str, Any]:
    """只处理本相机；返回小清单，数组不进入 Ray object store。"""

    started = time.perf_counter()
    if fail_once:
        raise RuntimeError("injected transient camera failure")
    store = _store(store_spec)
    encoded = store.read(image_resource)
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("输入图像无法解码")
    canvas = WorldCanvas(**canvas_data)
    config = ParameterStitchConfig(**config_data)
    part = prepare_camera_stitch_part(camera, image, canvas, config)
    buffer = io.BytesIO()
    np.savez_compressed(buffer, warped=part.warped, valid=part.valid,
                        weight=part.weight, transform=part.transform)
    scope = _part_scope(identity, part_attempt)
    write_store = _store(store_spec, scope)
    result = _write_part(write_store, store_spec, scope, buffer.getvalue())
    # 诊断元数据只保存去标识化 worker 标识，避免业务输入路径泄漏。
    import ray
    task_id = str(ray.get_runtime_context().get_task_id()) if ray.is_initialized() else "local-direct"
    node_id = str(ray.get_runtime_context().get_node_id()) if ray.is_initialized() else "local-direct"
    manifest = {"schema_version": SCHEMA, "identity": identity, "part_attempt": part_attempt,
            "input_sha256": image_resource["sha256"],
            "calibration_sha256": _calibration_digest(camera), "canvas_sha256": identity["canvas_hash"],
            "result": result, "shape": [canvas.height, canvas.width],
            "camera_id": identity["camera_id"], "task_id": task_id, "worker_pid": os.getpid(),
            "worker_node_id": node_id, "duration_seconds": round(time.perf_counter() - started, 6)}
    manifest_resource = _write_part_manifest(write_store, store_spec, scope, manifest)
    return {**manifest, "manifest_resource": manifest_resource}


def _validated_part(
    manifest: dict[str, Any], expected: dict[str, str], image: dict[str, Any],
    camera: dict[str, Any], canvas: WorldCanvas, spec: dict[str, str],
) -> CameraStitchPart:
    """全量校验目标 attempt 后才把数组交给原算法的全局 reduce。"""

    if (manifest.get("schema_version") != SCHEMA or manifest.get("identity") != expected
            or manifest.get("camera_id") != expected["camera_id"]
            or manifest.get("input_sha256") != image["sha256"]
            or manifest.get("calibration_sha256") != _calibration_digest(camera)
            or manifest.get("canvas_sha256") != expected["canvas_hash"]
            or manifest.get("shape") != [canvas.height, canvas.width]):
        raise ValueError("分片 schema、候选、画布或相机合同不匹配")
    part_attempt = _identity(manifest.get("part_attempt"), "part_attempt")
    scope = _part_scope(expected, part_attempt)
    persisted = _read_part_manifest(spec, scope, manifest["manifest_resource"])
    if persisted != {key: value for key, value in manifest.items() if key != "manifest_resource"}:
        raise ValueError("分片清单与 worker 返回值不一致")
    data = _read_part(_store(spec), spec, scope, manifest["result"])
    with np.load(io.BytesIO(data), allow_pickle=False) as arrays:
        part = CameraStitchPart(expected["camera_id"], arrays["warped"],
                                arrays["valid"], arrays["weight"], arrays["transform"])
    if (part.warped.shape != (canvas.height, canvas.width, 3) or part.valid.shape != (canvas.height, canvas.width)
            or part.weight.shape != part.valid.shape or part.transform.shape != (3, 3)
            or part.warped.dtype != np.uint8 or part.valid.dtype != np.uint8
            or part.weight.dtype != np.float32 or not np.isfinite(part.weight).all()
            or not np.isfinite(part.transform).all()):
        raise ValueError("分片数组尺寸、类型或数值非法")
    return part


def run_ray_stitching(
    intermediate_path: str | Path, output_dir: str | Path, *,
    job_id: str, run_id: str, attempt_id: str,
    image_resources: dict[str, dict[str, Any]], store_spec: dict[str, str],
    candidate_name: str | None = None, config: ParameterStitchConfig | None = None,
    resources: RayResources | None = None, fail_once_camera: str | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """限流发出任务，失败仅重试受影响相机，全体校验通过才调用 reduce。"""

    import ray  # 串行桌面使用不依赖 Ray。

    if not ray.is_initialized():
        raise ValueError("Ray 尚未启动；请先 ray.init 或使用串行入口")
    config, resources = config or ParameterStitchConfig(), resources or RayResources()
    resources.validate()
    if store_spec.get("type") == "local":
        # worker 可能有不同的工作目录，必须传可共享的绝对存储根。
        store_spec = {**store_spec, "root": str(Path(store_spec["root"]).expanduser().resolve())}
    runtime, canvas = prepare_stitching_runtime(intermediate_path, candidate_name, config)
    cameras = list(runtime.camera_rig["cameras"].values())
    ids = [str(camera["camera_id"]) for camera in cameras]
    if len(ids) != len(set(ids)) or set(ids) != set(image_resources):
        raise ValueError("相机集合缺失、重复或含多余输入资源")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("目标输出目录已有产物，拒绝覆盖")
    for value in (job_id, run_id, attempt_id, runtime.candidate_name, *ids):
        _identity(value, "分片身份")
    ground_item = runtime.manifest.get("frames", {}).get("ground_plane")
    ground_contract = json.loads((runtime.package_path / ground_item).read_text(encoding="utf-8")) if ground_item else {}
    canvas_hash = _digest({"canvas": asdict(canvas), "config": asdict(config),
                           "candidate": runtime.candidate_name, "algorithm_version": TASK_VERSION,
                           "coordinate_contract": ground_contract})
    remote = ray.remote(_one_camera_task).options(
        num_cpus=resources.num_cpus, num_gpus=resources.num_gpus,
        memory=resources.memory_bytes, max_retries=0,
    )
    pending: dict[Any, tuple[int, int]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    expected_identities: dict[str, dict[str, str]] = {}
    attempt_counts: dict[str, int] = {}
    next_index = 0
    while next_index < len(cameras) or pending:
        while next_index < len(cameras) and len(pending) < resources.max_in_flight:
            index = next_index
            next_index += 1
            camera = cameras[index]
            cid = ids[index]
            identity = {"job_id": job_id, "run_id": run_id,
                        "candidate": runtime.candidate_name, "canvas_hash": canvas_hash,
                        "camera_id": cid, "task_version": TASK_VERSION}
            expected_identities[cid] = identity
            ref = remote.remote(camera, asdict(canvas), asdict(config), image_resources[cid],
                                store_spec, identity, f"{attempt_id}-{0}",
                                fail_once_camera == cid)
            pending[ref] = (index, 0)
        ready, _ = ray.wait(list(pending), num_returns=1)
        ref = ready[0]
        index, retry = pending.pop(ref)
        cid = ids[index]
        try:
            manifest = ray.get(ref)
        except Exception as exc:
            # 合同错误由驱动端预检；worker 的读取/进程失败允许有限重试。
            cause = exc.as_instanceof_cause() if hasattr(exc, "as_instanceof_cause") else exc
            if isinstance(cause, ValueError):
                raise ValueError(f"相机 {cid} 合同或图像错误") from exc
            if retry + 1 >= resources.max_attempts:
                raise RuntimeError(f"相机 {cid} 分片重试耗尽") from exc
            identity = {"job_id": job_id, "run_id": run_id,
                        "candidate": runtime.candidate_name, "canvas_hash": canvas_hash,
                        "camera_id": cid, "task_version": TASK_VERSION}
            next_retry = retry + 1
            new_ref = remote.remote(cameras[index], asdict(canvas), asdict(config),
                                    image_resources[cid], store_spec, identity,
                                    f"{attempt_id}-{next_retry}", False)
            pending[new_ref] = (index, next_retry)
            continue
        manifests[cid] = manifest
        attempt_counts[cid] = retry + 1
    if set(manifests) != set(ids) or len(manifests) != len(ids):
        raise ValueError("分片屏障前相机集合不完整")
    # 先校验全部分片，再创建输出目录；任何坏对象都不能混入 reduce。
    for camera in cameras:
        cid = str(camera["camera_id"])
        while True:
            try:
                _validated_part(manifests[cid], expected_identities[cid],
                                image_resources[cid], camera, canvas, store_spec)
                break
            except StorageUnavailable as exc:
                # 暂态对象读取只重做本相机；合同和校验和错误直接失败。
                retry = attempt_counts[cid]
                if retry >= resources.max_attempts:
                    raise RuntimeError(f"相机 {cid} 分片读取重试耗尽") from exc
                ref = remote.remote(camera, asdict(canvas), asdict(config),
                                    image_resources[cid], store_spec, expected_identities[cid],
                                    f"{attempt_id}-{retry}", False)
                manifests[cid] = ray.get(ref)
                attempt_counts[cid] = retry + 1
    result = run_parameter_stitching(
        intermediate_path, output, candidate_name=runtime.candidate_name,
        config=config, prepared=(runtime, canvas),
        part_loader=lambda camera, _canvas, _config: _validated_part(
            manifests[str(camera["camera_id"])],
            expected_identities[str(camera["camera_id"])],
            image_resources[str(camera["camera_id"])], camera, canvas, store_spec,
        ),
    )
    # 发布前把可追溯分片清单写入业务 manifest；不改变原有图片输出合同。
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    report["parallel_parts"] = [
        {"camera_id": cid, "identity": expected_identities[cid],
         "part_attempt": manifests[cid]["part_attempt"],
         "manifest_resource": manifests[cid]["manifest_resource"],
         "task_id": manifests[cid]["task_id"],
         "worker_pid": manifests[cid]["worker_pid"],
         "worker_node_id": manifests[cid]["worker_node_id"],
         "duration_seconds": manifests[cid]["duration_seconds"]}
        for cid in ids
    ]
    result.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result, [manifests[cid] for cid in ids]


def main() -> None:
    """本地合成验收命令；正式控制面由 Phase 2 execute 调用同一入口。"""

    parser = argparse.ArgumentParser(description="Ray 逐相机拼接")
    parser.add_argument("--intermediate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--address", default="auto")
    parser.add_argument("--ray-temp-dir", type=Path)
    parser.add_argument("--pixels-per-metre", type=float, default=8.0)
    parser.add_argument("--max-canvas-long-edge", type=int, default=400)
    parser.add_argument("--max-canvas-pixels", type=int, default=120_000)
    parser.add_argument("--task-cpus", type=float, default=1.0)
    parser.add_argument("--task-gpus", type=float, default=0.0)
    parser.add_argument("--task-memory-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-in-flight", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    import ray
    ray.init(address=args.address, _temp_dir=str(args.ray_temp_dir) if args.ray_temp_dir else None)
    config = ParameterStitchConfig(
        pixels_per_metre=args.pixels_per_metre,
        max_canvas_long_edge=args.max_canvas_long_edge,
        max_canvas_pixels=args.max_canvas_pixels,
    )
    runtime, _ = prepare_stitching_runtime(args.intermediate, None, config)
    store = LocalObjectStore(args.store_root)
    images = {str(camera["camera_id"]): store.put(Path(camera["image_path"]).read_bytes(), "image/jpeg")
              for camera in runtime.camera_rig["cameras"].values()}
    result, manifests = run_ray_stitching(
        args.intermediate, args.output, job_id=args.job_id, run_id=args.run_id,
        attempt_id=args.attempt_id, image_resources=images,
        store_spec={"type": "local", "root": str(args.store_root)},
        config=config,
        resources=RayResources(num_cpus=args.task_cpus, num_gpus=args.task_gpus,
            memory_bytes=args.task_memory_bytes, max_in_flight=args.max_in_flight,
            max_attempts=args.max_attempts),
    )
    print(json.dumps({"summary": result.summary, "tasks": manifests}, ensure_ascii=False))


if __name__ == "__main__":
    main()
