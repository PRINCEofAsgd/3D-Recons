"""云端快照、run 与 artifact 合同，以及本地物化和业务适配。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from store_vision import __version__
from store_vision.data.loader import find_calibration_path, find_floor_plan_path, find_scale_path, load_gui_dataset_folder
from store_vision.data.workspace import load_manifest, load_intermediate_runtime, publish_intermediate_package
from store_vision.mapping.map25d_pipeline import run_map25d_from_intermediate
from store_vision.mapping.parameter_stitcher import ParameterStitchConfig, run_parameter_stitching

SCHEMA = "store-vision-cloud/1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _now() -> str:
    """统一使用 UTC 时间，避免容器时区改变合同含义。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} 非法：{value!r}")
    return value


def _relative(value: str) -> PurePosixPath:
    """拒绝绝对路径、反斜杠、编码绕行和目录穿越。"""
    if (not isinstance(value, str) or not value or "\\" in value or "%" in value
            or ":" in value or any(ord(char) < 32 for char in value)):
        raise ValueError(f"资源相对路径非法：{value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"资源路径越界：{value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
    if any(part.rstrip(". ") != part or part.split(".")[0].upper() in reserved for part in path.parts):
        raise ValueError(f"资源路径在 Windows 上非法：{value!r}")
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


class LocalObjectStore:
    """只暴露字节对象接口；以后可替换为对象存储实现。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _checked(self, path: Path) -> Path:
        """阻止本地存储目录中的符号链接把对象导向根目录外。"""
        if not path.resolve().is_relative_to(self.root) or path.is_symlink():
            raise ValueError("对象存储路径越界")
        return path

    def put(self, data: bytes, media_type: str) -> dict[str, Any]:
        """按内容寻址写入，不覆盖不同内容，返回完整资源描述。"""
        digest = _sha(data)
        path = self.root / "objects" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        self._checked(path)
        if not path.exists():
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".stage-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temp, path)
            except FileExistsError:
                pass
            finally:
                Path(temp).unlink(missing_ok=True)
        if path.read_bytes() != data:
            raise ValueError("对象存储中已有同名但内容不符的对象")
        return {"uri": f"svlocal://objects/{digest}", "bytes": len(data), "sha256": digest, "media_type": media_type}

    def read(self, resource: dict[str, Any]) -> bytes:
        """读取时再次核对 URI、字节数和 SHA-256。"""
        if not isinstance(resource, dict):
            raise ValueError("资源描述必须是对象")
        uri = resource.get("uri")
        parsed = urlsplit(uri) if isinstance(uri, str) else None
        digest = str(resource.get("sha256", ""))
        if (parsed is None or parsed.scheme != "svlocal" or parsed.netloc != "objects"
                or parsed.path != f"/{digest}" or parsed.query or parsed.fragment or not _SHA.fullmatch(digest)):
            raise ValueError(f"对象 URI 非法：{uri!r}")
        path = self.root / "objects" / digest[:2] / digest
        self._checked(path)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"对象缺失：{uri}") from exc
        if type(resource.get("bytes")) is not int or len(data) != resource["bytes"] or _sha(data) != digest:
            raise ValueError(f"对象大小或 SHA-256 不符：{uri}")
        if not isinstance(resource.get("media_type"), str) or not resource["media_type"]:
            raise ValueError("资源媒体类型缺失")
        return data

    def publish(self, kind: str, identity: tuple[str, ...], manifest: dict[str, Any]) -> Path:
        """所有对象验证后才原子地使 manifest 可见。"""
        if kind not in {"snapshots", "runs", "artifacts"}:
            raise ValueError("未知清单类型")
        parts = [_identity(part, "清单身份") for part in identity]
        path = self.root / "manifests" / kind / Path(*parts) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._checked(path)
        data = _json_bytes(manifest)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".stage-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, path)
        except FileExistsError as exc:
            raise ValueError(f"清单已发布，不允许覆盖：{path}") from exc
        finally:
            Path(temp).unlink(missing_ok=True)
        return path


def _resource(store: LocalObjectStore, path: Path, name: str, **extra: Any) -> dict[str, Any]:
    """只读取已经由调用者明确选定的文件。"""
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"资源缺失或不是普通文件：{name}")
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {"path": str(_relative(name)), **store.put(path.read_bytes(), media), **extra}


def _validate_header(manifest: dict[str, Any], kind: str, dataset_id: str, run_id: str | None = None, attempt_id: str | None = None) -> None:
    """清单身份由调用方的期望值约束，不能只相信清单自述。"""
    if manifest.get("schema_version") != SCHEMA or manifest.get("kind") != kind:
        raise ValueError("云端合同 schema 或 kind 错误")
    if manifest.get("dataset_id") != _identity(dataset_id, "dataset_id"):
        raise ValueError("dataset_id 不匹配")
    if run_id is not None and manifest.get("run_id") != _identity(run_id, "run_id"):
        raise ValueError("run_id 不匹配")
    if attempt_id is not None and manifest.get("attempt_id") != _identity(attempt_id, "attempt_id"):
        raise ValueError("attempt_id 不匹配")
    if (not isinstance(manifest.get("producer"), str) or not manifest["producer"]
            or not isinstance(manifest.get("algorithm_version"), str) or not manifest["algorithm_version"]
            or not _SHA.fullmatch(str(manifest.get("parameters_digest", "")))
            or not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]):
        raise ValueError("清单缺少生产者、算法版本、参数摘要或时间")


def _validated_files(store: LocalObjectStore, rows: Any) -> dict[str, bytes]:
    """在物化前完成整批校验，避免产生可被误用的半成品。"""
    if not isinstance(rows, list) or not rows:
        raise ValueError("资源清单为空")
    files: dict[str, bytes] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("资源行必须是对象")
        name = str(_relative(row.get("path")))
        if name in files:
            raise ValueError(f"重复资源路径：{name}")
        files[name] = store.read(row)
    return files


def create_dataset_snapshot(store: LocalObjectStore, dataset_path: str | Path, dataset_id: str, snapshot_id: str) -> dict[str, Any]:
    """对固定输入格式预检后冻结逐相机输入，原目录始终只读。"""
    dataset_id = _identity(dataset_id, "dataset_id")
    snapshot_id = _identity(snapshot_id, "snapshot_id")
    root = Path(dataset_path).expanduser().resolve()
    dataset = load_gui_dataset_folder(root)
    cali = find_calibration_path(root, include_calibration_subdir=False)
    scale = find_scale_path(root, include_calibration_subdir=False)
    floor = find_floor_plan_path(root)
    assert cali and scale and floor
    files = [_resource(store, path, path.relative_to(root).as_posix(), role=role) for path, role in ((cali, "cali"), (scale, "scale"), (floor, "floorplan"))]
    camera_ids: set[str] = set()
    for camera in dataset.camera_list():
        camera_id = _identity(camera.device_serial, "camera_id")
        if camera_id in camera_ids:
            raise ValueError(f"重复相机 ID：{camera_id}")
        camera_ids.add(camera_id)
        if not camera.image_path:
            raise ValueError(f"相机图片缺失：{camera_id}")
        path = Path(camera.image_path).resolve()
        try:
            name = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("相机图片越过输入目录") from exc
        if not name.startswith("screenshots/"):
            raise ValueError("相机图片不在 screenshots 目录")
        files.append(_resource(store, path, name, role="image", camera_id=camera_id))
    manifest = {"schema_version": SCHEMA, "kind": "dataset_snapshot", "dataset_id": dataset_id, "snapshot_id": snapshot_id,
                "producer": "store_vision.cloud_workspace", "algorithm_version": __version__, "parameters_digest": _sha(b"{}"),
                "created_at": _now(), "parents": [], "camera_ids": sorted(camera_ids), "files": files}
    validate_snapshot(store, manifest, dataset_id)
    store.publish("snapshots", (dataset_id, snapshot_id), manifest)
    return manifest


def validate_snapshot(store: LocalObjectStore, manifest: dict[str, Any], dataset_id: str) -> dict[str, bytes]:
    """核对角色、图片身份和全部对象内容。"""
    _validate_header(manifest, "dataset_snapshot", dataset_id)
    _identity(manifest.get("snapshot_id"), "snapshot_id")
    files = _validated_files(store, manifest.get("files"))
    rows = manifest["files"]
    for role in ("cali", "scale", "floorplan"):
        if sum(row.get("role") == role for row in rows) != 1:
            raise ValueError(f"输入角色缺失或重复：{role}")
    ids = [row.get("camera_id") for row in rows if row.get("role") == "image"]
    if not ids or any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)) or sorted(ids) != manifest.get("camera_ids"):
        raise ValueError("重复相机 ID 或相机清单不一致")
    if any(row.get("role") not in {"cali", "scale", "floorplan", "image"} for row in rows):
        raise ValueError("未知输入角色")
    return files


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    """在独立空目录中写入已验证字节，并阻断符号链接跳转。"""
    for name, data in files.items():
        path = root.joinpath(*_relative(name).parts)
        if path.exists() or path.is_symlink():
            raise ValueError(f"物化目标已存在：{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"物化路径越界：{name}")
        path.write_bytes(data)


def materialize_snapshot(store: LocalObjectStore, manifest: dict[str, Any], dataset_id: str, scratch_root: str | Path) -> Path:
    """在调用者提供的新 scratch 根下恢复标准输入树。"""
    files = validate_snapshot(store, manifest, dataset_id)
    root = Path(scratch_root).resolve() / "input" / dataset_id
    root.mkdir(parents=True, exist_ok=False)
    _write_files(root, files)
    load_gui_dataset_folder(root)
    return root


def _portable(value: Any, input_root: Path, package_root: Path) -> Any:
    """把 v2 JSON 内的本机路径改为局部引用，原 v2 文件不变。"""
    if isinstance(value, dict):
        return {key: _portable(item, input_root, package_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, input_root, package_root) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        path = Path(value).resolve()
        for prefix, base in (("input", input_root), ("intermediate", package_root)):
            if path.is_relative_to(base):
                relative = path.relative_to(base).as_posix()
                return f"svref://{prefix}" if relative == "." else f"svref://{prefix}/{relative}"
        raise ValueError("v2 资源含快照之外的机器绝对路径")
    return value


def _local(value: Any, input_root: Path, package_root: Path) -> Any:
    """仅把受控 svref 引用恢复为 scratch 内路径。"""
    if isinstance(value, dict):
        return {key: _local(item, input_root, package_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_local(item, input_root, package_root) for item in value]
    if isinstance(value, str) and value.startswith("svref://"):
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.netloc not in {"input", "intermediate"}:
            raise ValueError("局部引用非法")
        base = input_root if parsed.netloc == "input" else package_root
        if not parsed.path:
            return str(base)
        return str(base.joinpath(*_relative(parsed.path[1:]).parts))
    return value


def _v2_resource_names(manifest: dict[str, Any]) -> list[str]:
    """只从 v2 manifest 的正式资源键枚举，不扫描目录猜测。"""
    names = ["manifest.json", manifest["input"]["camera_plan_controls"]]
    for key in ("calibration_candidates", "planar_projection_candidates"):
        names.extend(item["path"] for item in manifest.get(key, {}).values())
    for key in ("frames", "business"):
        names.extend(manifest.get(key, {}).values())
    if manifest.get("evidence", {}).get("sfm"):
        names.append(manifest["evidence"]["sfm"])
    return sorted(set(str(_relative(name)) for name in names))


def stage_intermediate(store: LocalObjectStore, package_path: str | Path, input_root: str | Path, snapshot: dict[str, Any], run_id: str, attempt_id: str, candidate: str = "estimated") -> dict[str, Any]:
    """把本地 v2 中间层转为独立、可恢复的云端 run 合同。"""
    dataset_id = snapshot["dataset_id"]
    validate_snapshot(store, snapshot, dataset_id)
    _identity(run_id, "run_id")
    _identity(attempt_id, "attempt_id")
    package = Path(package_path).resolve()
    input_root = Path(input_root).resolve()
    v2 = load_manifest(package)
    if v2["dataset_id"] != dataset_id:
        raise ValueError("v2 中间层 dataset_id 不匹配")
    if candidate not in v2.get("calibration_candidates", {}) or candidate not in v2.get("planar_projection_candidates", {}):
        raise ValueError("标定候选与平面候选不一致")
    runtime = load_intermediate_runtime(package, candidate_name=candidate)
    if set(runtime.camera_rig.get("cameras", {})) != set(snapshot["camera_ids"]):
        raise ValueError("中间层相机身份与输入快照不一致")
    resources = []
    for name in _v2_resource_names(v2):
        path = package.joinpath(*_relative(name).parts)
        if not path.resolve().is_relative_to(package) or not path.is_file() or path.is_symlink():
            raise ValueError(f"中间层资源越界或缺失：{name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        portable = _portable(payload, input_root, package)
        resources.append({"path": name, **store.put(_json_bytes(portable), "application/json")})
    resource_by_path = {row["path"]: row for row in resources}
    ground = json.loads((package / v2["frames"]["ground_plane"]).read_text(encoding="utf-8"))
    plan = json.loads((package / v2["frames"]["plan_registration"]).read_text(encoding="utf-8"))
    coordinates = {"ground_frame": ground["world_frame_id"], "ground_unit": ground["unit"],
                   "plan_frame": plan["plan_frame_id"], "plan_unit": "pixel"}
    parameter_summary = {
        "candidate": candidate,
        "rig_sha256": resource_by_path[v2["calibration_candidates"][candidate]["path"]]["sha256"],
        "projection_sha256": resource_by_path[v2["planar_projection_candidates"][candidate]["path"]]["sha256"],
    }
    run = {"schema_version": SCHEMA, "kind": "run", "dataset_id": dataset_id, "snapshot_id": snapshot["snapshot_id"],
           "run_id": run_id, "attempt_id": attempt_id, "producer": "store_vision.cloud_workspace", "algorithm_version": __version__,
           "parameters_digest": _sha(_json_bytes(parameter_summary)), "created_at": _now(),
           "parents": [{"kind": "dataset_snapshot", "dataset_id": dataset_id, "snapshot_id": snapshot["snapshot_id"]}],
           "candidate": candidate, "camera_ids": snapshot["camera_ids"], "capabilities": runtime.capabilities.as_dict(),
           "coordinate_contract": coordinates,
           "resources": resources}
    validate_run(store, run, snapshot, dataset_id, run_id, attempt_id)
    store.publish("runs", (dataset_id, run_id, attempt_id), run)
    return run


def validate_run(store: LocalObjectStore, run: dict[str, Any], snapshot: dict[str, Any], dataset_id: str, run_id: str, attempt_id: str) -> dict[str, bytes]:
    """校验父快照、候选、坐标合同与全部 v2 资源。"""
    _validate_header(run, "run", dataset_id, run_id, attempt_id)
    validate_snapshot(store, snapshot, dataset_id)
    if run.get("snapshot_id") != snapshot["snapshot_id"] or run.get("camera_ids") != snapshot["camera_ids"]:
        raise ValueError("run 与输入快照身份不一致")
    if run.get("parents") != [{"kind": "dataset_snapshot", "dataset_id": dataset_id, "snapshot_id": snapshot["snapshot_id"]}]:
        raise ValueError("run 父资源不一致")
    files = _validated_files(store, run.get("resources"))
    if "manifest.json" not in files:
        raise ValueError("run 缺少 v2 manifest")
    v2 = json.loads(files["manifest.json"])
    if v2.get("schema_version") != 2 or v2.get("dataset_id") != dataset_id:
        raise ValueError("v2 schema 或 dataset_id 错误")
    candidate = run.get("candidate")
    if candidate not in v2.get("calibration_candidates", {}) or candidate not in v2.get("planar_projection_candidates", {}):
        raise ValueError("候选不一致")
    if set(files) != set(_v2_resource_names(v2)):
        raise ValueError("v2 资源清单不完整")
    rig_name = v2["calibration_candidates"][candidate]["path"]
    projection_name = v2["planar_projection_candidates"][candidate]["path"]
    rows_by_path = {row["path"]: row for row in run["resources"]}
    expected_parameters = {"candidate": candidate, "rig_sha256": rows_by_path[rig_name]["sha256"], "projection_sha256": rows_by_path[projection_name]["sha256"]}
    if run.get("parameters_digest") != _sha(_json_bytes(expected_parameters)):
        raise ValueError("run 参数摘要不一致")
    rig = json.loads(files[rig_name])
    projection = json.loads(files[projection_name])
    if (set(rig.get("cameras", {})) != set(snapshot["camera_ids"])
            or set(projection.get("projections", {})) != set(snapshot["camera_ids"])
            or projection.get("candidate_name") != candidate):
        raise ValueError("v2 相机 ID 或候选不一致")
    images = {row["camera_id"]: f"svref://input/{row['path']}" for row in snapshot["files"] if row["role"] == "image"}
    for camera_id, expected_image in images.items():
        if rig["cameras"][camera_id].get("image_path") != expected_image:
            raise ValueError(f"相机图片引用与快照不一致：{camera_id}")
    manifest_images = {row["camera_id"]: row.get("path") for row in v2.get("input", {}).get("images", [])}
    if len(v2.get("input", {}).get("images", [])) != len(images) or manifest_images != images:
        raise ValueError("v2 输入图片清单与快照不一致")
    if v2.get("input", {}).get("dataset_path") != "svref://input":
        raise ValueError("v2 输入目录与快照不一致")
    for role, field in (("floorplan", "floorplan_path"), ("scale", "scale_path")):
        expected = next(f"svref://input/{row['path']}" for row in snapshot["files"] if row["role"] == role)
        if v2["input"].get(field) != expected:
            raise ValueError(f"v2 {role} 引用与快照不一致")
    ground = json.loads(files[v2["frames"]["ground_plane"]])
    plan = json.loads(files[v2["frames"]["plan_registration"]])
    expected_coordinates = {"ground_frame": ground.get("world_frame_id"), "ground_unit": ground.get("unit"),
                            "plan_frame": plan.get("plan_frame_id"), "plan_unit": "pixel"}
    if any(not isinstance(value, str) or not value for value in expected_coordinates.values()) or run.get("coordinate_contract") != expected_coordinates:
        raise ValueError("坐标或单位合同错误")
    if any(camera.get("world_frame_id") != ground["world_frame_id"] or camera.get("unit") != ground["unit"] for camera in rig["cameras"].values()):
        raise ValueError("候选相机坐标系与地面合同不一致")
    def check_paths(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                check_paths(item)
        elif isinstance(value, list):
            for item in value:
                check_paths(item)
        elif isinstance(value, str):
            if Path(value).is_absolute():
                raise ValueError("可移植资源仍含绝对路径")
            if value.startswith("svref://"):
                parsed = urlsplit(value)
                if parsed.netloc not in {"input", "intermediate"} or parsed.query or parsed.fragment:
                    raise ValueError("局部引用非法")
                if parsed.path:
                    _relative(parsed.path[1:])
    for data in files.values():
        check_paths(json.loads(data))
    return files


def materialize_run(store: LocalObjectStore, run: dict[str, Any], snapshot: dict[str, Any], dataset_id: str, run_id: str, attempt_id: str, scratch_root: str | Path) -> Path:
    """先校验所有对象，再在新的 scratch 中恢复 v2 供原消费者使用。"""
    files = validate_run(store, run, snapshot, dataset_id, run_id, attempt_id)
    scratch = Path(scratch_root).resolve()
    input_root = materialize_snapshot(store, snapshot, dataset_id, scratch)
    package = scratch / "intermediate" / dataset_id / run_id / attempt_id
    package.mkdir(parents=True, exist_ok=False)
    local_files = {name: _json_bytes(_local(json.loads(data), input_root, package)) for name, data in files.items()}
    _write_files(package, local_files)
    runtime = load_intermediate_runtime(package, candidate_name=run["candidate"])
    if runtime.capabilities.as_dict() != run["capabilities"]:
        raise ValueError("恢复后的能力门禁与 run 合同不一致")
    return package


def stage_artifact(store: LocalObjectStore, output_dir: str | Path, run: dict[str, Any], artifact_id: str, workflow: str) -> dict[str, Any]:
    """按原业务 manifest 列出的资源发布结果，失败时不发布清单。"""
    _identity(artifact_id, "artifact_id")
    if workflow not in {"map25d", "stitching"}:
        raise ValueError("未知业务类型")
    root = Path(output_dir).resolve()
    original = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if original.get("dataset_id") != run["dataset_id"] or original.get("calibration_candidate") != run["candidate"]:
        raise ValueError("业务结果的数据集或候选身份不一致")
    intermediate_source = Path(str(original.get("source_intermediate", ""))).resolve()
    if tuple(intermediate_source.parts[-3:]) != (run["dataset_id"], run["run_id"], run["attempt_id"]):
        raise ValueError("业务结果的 run/attempt 来源不一致")
    required_key = "geojson" if workflow == "map25d" else "fused"
    required_name = original.get("outputs", {}).get(required_key)
    if not isinstance(required_name, str) or not (root / required_name).is_file():
        raise ValueError(f"业务结果缺少必需资源：{required_key}")
    names = ["manifest.json"]
    def collect(value: Any) -> None:
        if isinstance(value, str):
            path = root.joinpath(*_relative(value).parts)
            if not path.resolve().is_relative_to(root):
                raise ValueError("业务资源路径越界")
            if path.is_file():
                names.append(value)
            elif path.is_dir():
                # v2 业务清单明确声明的目录可展开，未声明目录不参与发布。
                names.extend(item.relative_to(root).as_posix() for item in path.rglob("*") if item.is_file())
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(original.get("outputs", {}))
    files = []
    for name in sorted(set(names)):
        path = root.joinpath(*_relative(name).parts)
        if not path.resolve().is_relative_to(root):
            raise ValueError("产物路径越界")
        if name == "manifest.json":
            portable = _portable(original, intermediate_source, root)
            files.append({"path": name, **store.put(_json_bytes(portable), "application/json")})
        elif path.suffix.lower() in {".json", ".geojson"}:
            portable = _portable(json.loads(path.read_text(encoding="utf-8")), intermediate_source, root)
            files.append({"path": name, **store.put(_json_bytes(portable), "application/geo+json" if path.suffix.lower() == ".geojson" else "application/json")})
        else:
            files.append(_resource(store, path, name))
    artifact = {"schema_version": SCHEMA, "kind": "artifact", "dataset_id": run["dataset_id"], "snapshot_id": run["snapshot_id"],
                "run_id": run["run_id"], "attempt_id": run["attempt_id"], "artifact_id": artifact_id,
                "producer": f"store_vision.{workflow}", "algorithm_version": __version__,
                "parameters_digest": _sha(_json_bytes({"candidate": run["candidate"], "workflow": workflow, "config": original.get("config"), "mode": original.get("map25d_mode")})),
                "created_at": _now(), "parents": [{"kind": "run", "dataset_id": run["dataset_id"], "run_id": run["run_id"], "attempt_id": run["attempt_id"]}],
                "candidate": run["candidate"], "coordinate_contract": run["coordinate_contract"], "workflow": workflow, "files": files}
    validate_artifact(store, artifact, run)
    store.publish("artifacts", (run["dataset_id"], run["run_id"], run["attempt_id"], artifact_id), artifact)
    return artifact


def validate_artifact(store: LocalObjectStore, artifact: dict[str, Any], run: dict[str, Any]) -> dict[str, bytes]:
    """对外可见前确认来源和每个结果对象。"""
    _validate_header(artifact, "artifact", run["dataset_id"], run["run_id"], run["attempt_id"])
    _identity(artifact.get("artifact_id"), "artifact_id")
    if artifact.get("snapshot_id") != run["snapshot_id"] or artifact.get("candidate") != run["candidate"]:
        raise ValueError("artifact 来源或候选不一致")
    if artifact.get("coordinate_contract") != run["coordinate_contract"] or artifact.get("workflow") not in {"map25d", "stitching"}:
        raise ValueError("artifact 坐标合同或业务类型错误")
    if artifact.get("parents") != [{"kind": "run", "dataset_id": run["dataset_id"], "run_id": run["run_id"], "attempt_id": run["attempt_id"]}]:
        raise ValueError("artifact 父资源不一致")
    files = _validated_files(store, artifact.get("files"))
    if "manifest.json" not in files or len(files) < 2:
        raise ValueError("artifact 缺少业务 manifest 或结果")
    for row in artifact["files"]:
        if row["media_type"] in {"application/json", "application/geo+json"}:
            payload = json.loads(files[row["path"]])
            if isinstance(payload, dict) and "source_intermediate" in payload and not str(payload["source_intermediate"]).startswith("svref://input"):
                raise ValueError("artifact 含非可移植的中间层引用")
    return files


def run_local_adapted(store: LocalObjectStore, snapshot: dict[str, Any], run_id: str, attempt_id: str, shared_report: dict[str, Any], scratch_root: str | Path, *, stitch_config: ParameterStitchConfig | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """单次合成链路：物化输入、调用既有入口、分别 stage-out 两个业务。"""
    dataset_id = snapshot["dataset_id"]
    scratch = Path(scratch_root).resolve()
    input_root = materialize_snapshot(store, snapshot, dataset_id, scratch)
    package = scratch / "work" / run_id / attempt_id / "intermediate"
    publish_intermediate_package(input_root, package, shared_report)
    run = stage_intermediate(store, package, input_root, snapshot, run_id, attempt_id)
    restored = materialize_run(store, run, snapshot, dataset_id, run_id, attempt_id, scratch / "consumer")
    map_root = scratch / "work" / run_id / attempt_id / "map25d"
    run_map25d_from_intermediate(restored, map_root, candidate_name=run["candidate"])
    map_artifact = stage_artifact(store, map_root, run, "map25d", "map25d")
    stitch_root = scratch / "work" / run_id / attempt_id / "stitching"
    run_parameter_stitching(restored, stitch_root, candidate_name=run["candidate"], config=stitch_config)
    stitching_artifact = stage_artifact(store, stitch_root, run, "stitching", "stitching")
    return run, map_artifact, stitching_artifact
