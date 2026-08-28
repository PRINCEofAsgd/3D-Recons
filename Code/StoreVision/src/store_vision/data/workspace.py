"""统一数据工作区、中间层合同和业务能力解析。

本模块把原始输入、标定候选、平面映射与两个业务输出之间的关系收口为
版本化数据合同。业务页面只消费这里返回的能力与运行时对象，不直接猜测
目录名称或报告字段。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from store_vision.calibration.intrinsics_validation import (
    provisional_fisheye_intrinsics,
)
from store_vision.calibration.homography import calibrate_homographies
from store_vision.calibration.scale_metadata import load_scale_metadata
from store_vision.config import StoreConfig
from store_vision.data.loader import load_store_folder
from store_vision.data.models import (
    CameraCalibration,
    Point2D,
    StoreDataset,
)

INTERMEDIATE_SCHEMA_VERSION = 2
SUPPORTED_INTERMEDIATE_SCHEMA_VERSIONS = {1, 2}
PACKAGE_TYPE = "store_vision_intermediate"


def _write_json(path: Path, payload: Any) -> None:
    """以稳定 UTF-8 格式写入中间层 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象并对错误数据给出带路径的提示。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取中间层 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"中间层 JSON 顶层必须是对象：{path}")
    return payload


def repository_root() -> Path:
    """返回包含 ``Code/StoreVision`` 的仓库根目录。"""

    root = Path(__file__).resolve().parents[5]
    if not (root / "Code" / "StoreVision").is_dir():
        raise RuntimeError(f"无法定位 Store Vision 仓库根目录：{root}")
    return root


@dataclass(frozen=True)
class WorkspacePaths:
    """一个数据集在统一工作区中的四类稳定路径。"""

    data_root: Path
    dataset_id: str

    @property
    def input_root(self) -> Path:
        return self.data_root / "input" / self.dataset_id

    @property
    def intermediate_root(self) -> Path:
        return self.data_root / "intermediate" / self.dataset_id

    @property
    def map25d_output_root(self) -> Path:
        return self.data_root / "map25d_output" / self.dataset_id

    @property
    def stitching_output_root(self) -> Path:
        return self.data_root / "stitching_output" / self.dataset_id


def workspace_paths(
    dataset_path: str | Path,
    data_root: str | Path | None = None,
) -> WorkspacePaths:
    """按数据集目录名建立工作区路径，不要求立即创建任何目录。"""

    dataset = Path(dataset_path).expanduser().resolve()
    root = (
        Path(data_root).expanduser().resolve()
        if data_root is not None
        else repository_root() / "data"
    )
    return WorkspacePaths(root, dataset.name)


def create_unique_run_directory(root: str | Path) -> Path:
    """创建防覆盖 ``run_时间戳`` 目录。"""

    parent = Path(root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().astimezone().strftime("run_%Y%m%d_%H%M%S")
    for index in range(100):
        suffix = "" if index == 0 else f"_{index:02d}"
        candidate = parent / f"{stem}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"无法在 {parent} 分配新的运行目录")


def find_latest_intermediate_package(
    intermediate_root: str | Path,
    dataset_path: str | Path,
) -> Path | None:
    """查找与当前原始数据集身份一致的最新有效中间层。"""

    root = Path(intermediate_root).expanduser().resolve()
    dataset = Path(dataset_path).expanduser().resolve()
    if not root.is_dir():
        return None
    matches: list[tuple[str, Path]] = []
    for manifest_path in root.glob("run_*/manifest.json"):
        try:
            manifest = load_manifest(manifest_path.parent)
            source_value = manifest.get("input", {}).get("dataset_path")
            if not source_value:
                continue
            source = Path(str(source_value)).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if source != dataset:
            continue
        matches.append(
            (str(manifest.get("generated_at", "")), manifest_path.parent)
        )
    return max(matches, default=(None, None), key=lambda item: item[0])[1]


def discover_intermediate_packages(
    intermediate_root: str | Path,
) -> tuple[IntermediatePackageSummary, ...]:
    """发现工作区内所有有效中间层，供两个业务页独立选择。"""

    root = Path(intermediate_root).expanduser().resolve()
    if not root.is_dir():
        return ()
    summaries: list[IntermediatePackageSummary] = []
    for manifest_path in root.glob("*/run_*/manifest.json"):
        try:
            manifest = load_manifest(manifest_path.parent)
            dataset_id = _safe_dataset_id(manifest.get("dataset_id"))
        except (OSError, ValueError):
            continue
        summaries.append(
            IntermediatePackageSummary(
                dataset_id=dataset_id,
                package_path=manifest_path.parent.resolve(),
                generated_at=str(manifest.get("generated_at", "")),
                producer=str(
                    manifest.get("producer")
                    or manifest.get("lineage", {}).get("producer")
                    or "input_optimization"
                ),
                schema_version=int(manifest.get("schema_version", 1)),
            )
        )
    return tuple(
        sorted(
            summaries,
            key=lambda item: (item.dataset_id, item.generated_at),
            reverse=True,
        )
    )


@dataclass(frozen=True)
class CapabilityState:
    """一项业务能力的状态和缺失资源。"""

    ready: bool
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    trust_level: str = "blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "trust_level": self.trust_level,
        }


@dataclass(frozen=True)
class CapabilityReport:
    """中间层候选可供哪些业务运行的统一判断。"""

    calibration: CapabilityState
    planar_mapping: CapabilityState
    metric_plan: CapabilityState
    map25d: CapabilityState
    stitching: CapabilityState
    sfm_assisted: CapabilityState

    def as_dict(self) -> dict[str, Any]:
        return {
            "calibration_ready": self.calibration.as_dict(),
            "planar_mapping_ready": self.planar_mapping.as_dict(),
            "metric_plan_ready": self.metric_plan.as_dict(),
            "map25d_ready": self.map25d.as_dict(),
            "stitching_ready": self.stitching.as_dict(),
            "sfm_assisted_ready": self.sfm_assisted.as_dict(),
        }


@dataclass
class IntermediateRuntime:
    """从中间层恢复出的业务运行时数据。"""

    package_path: Path
    manifest: dict[str, Any]
    candidate_name: str
    dataset: StoreDataset
    config: StoreConfig
    capabilities: CapabilityReport
    camera_rig: dict[str, Any]
    planar_projection: dict[str, Any] | None


@dataclass(frozen=True)
class IntermediatePackageSummary:
    """供业务页选择器展示的一份中间层简要信息。"""

    dataset_id: str
    package_path: Path
    generated_at: str
    producer: str
    schema_version: int


def _resource_path(package: Path, value: str | Path) -> Path:
    """解析包内相对资源，同时兼容派生包保存的绝对只读引用。"""

    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (package / path).resolve()


def _safe_dataset_id(value: Any) -> str:
    """把 manifest 数据集标识限制为可安全用作单级目录的文本。"""

    dataset_id = str(value or "").strip()
    if not dataset_id or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
        raise ValueError(f"中间层 dataset_id 非法：{dataset_id!r}")
    return dataset_id


def _relative(package: Path, path: Path) -> str:
    """生成相对中间层根目录的稳定 POSIX 路径。"""

    return path.resolve().relative_to(package.resolve()).as_posix()


def _plan_runtime_config(
    dataset: StoreDataset,
    cfg: StoreConfig,
) -> tuple[StoreConfig, dict[str, Any]]:
    """用 scale 元数据解析本数据集的米制平面，不跨门店复用默认尺寸。"""

    runtime = replace(cfg)
    summary: dict[str, Any] = {
        "status": "default_dimensions",
        "plan_width_cm": runtime.floor_plan_width_cm,
        "plan_height_cm": runtime.floor_plan_height_cm,
        "rmse_cm": None,
        "max_error_cm": None,
        "world_definition": "X right, Y plan-up, Z up; ground Z=0",
    }
    if not dataset.scale_path:
        return runtime, summary
    scale, metadata = load_scale_metadata(dataset.scale_path)
    runtime.floor_plan_width_cm = (
        abs(scale.x_metres_per_percent) * 100.0 * 100.0
    )
    runtime.floor_plan_height_cm = (
        abs(scale.y_metres_per_percent) * 100.0 * 100.0
    )
    summary = {
        "status": "scale_metadata_trial",
        "source": str(Path(dataset.scale_path).resolve()),
        "plan_width_cm": runtime.floor_plan_width_cm,
        "plan_height_cm": runtime.floor_plan_height_cm,
        "rmse_cm": scale.rmse_metres * 100.0,
        "max_error_cm": scale.max_error_metres * 100.0,
        "world_definition": "X right, Y plan-up, Z up; ground Z=0",
        "scale_model": scale.as_dict(),
        "store_outline_percent": metadata.get(
            "mall_plan_coordinates_percent", []
        ),
    }
    return runtime, summary


def _active_intrinsics(
    report: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any], str]:
    """从共享标定报告选择可信 K/D，门禁拒绝时构造明确的试算参数。"""

    selection = report.get("selection", {})
    optimization = report.get("nonlinear_optimization", {})
    validation = selection.get("intrinsics_validation", {})
    resolution = tuple(
        int(value)
        for value in (
            validation.get("resolution")
            or report.get("intrinsics_groups", [{}])[0].get(
                "canonical_resolution", []
            )
        )
    )
    fitted_k = np.asarray(selection.get("selected_K"), dtype=np.float64)
    fitted_d = np.asarray(
        selection.get("selected_D", selection.get("distortion_D")),
        dtype=np.float64,
    ).reshape(-1)
    if fitted_k.shape != (3, 3) or fitted_d.size != 4:
        raise ValueError("共享标定报告缺少有效的鱼眼 K/D")
    if validation.get("usable_for_sfm"):
        return fitted_k, fitted_d, dict(validation), "credible_fitted_fisheye"
    initial_k = np.asarray(
        optimization.get("initialization", {}).get("initial_K"),
        dtype=np.float64,
    )
    if initial_k.shape != (3, 3) or len(resolution) != 2:
        raise ValueError("拟合 K/D 被拒绝，但报告缺少可构造试算参数的初始 K/分辨率")
    active_k, active_d, distortion_scale, active_validation = (
        provisional_fisheye_intrinsics(initial_k, fitted_d, resolution)
    )
    active_validation = {
        **active_validation,
        "source_fitted_validation": validation,
        "distortion_scale": distortion_scale,
    }
    return (
        np.asarray(active_k, dtype=np.float64),
        np.asarray(active_d, dtype=np.float64).reshape(4),
        active_validation,
        "provisional_initial_k_scaled_nonzero_d",
    )


def _apply_shared_report(
    dataset: StoreDataset,
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把共享标定报告解析成逐相机原生 K/D/R/t 和平面 H。"""

    canonical_k, shared_d, validation, route = _active_intrinsics(report)
    homography_rows = {
        str(row.get("physical_camera_id")): row
        for row in report.get("homographies", [])
        if isinstance(row, dict) and row.get("physical_camera_id")
    }
    pose_rows = {
        str(row.get("physical_camera_id")): row
        for row in report.get("poses", [])
        if isinstance(row, dict) and row.get("physical_camera_id")
    }
    cameras: dict[str, Any] = {}
    for camera in dataset.camera_list():
        camera_id = camera.device_serial
        homography_row = homography_rows.get(camera_id)
        pose = pose_rows.get(camera_id)
        if homography_row is None or pose is None:
            continue
        image_to_canonical = np.asarray(
            homography_row.get("image_to_canonical_transform"),
            dtype=np.float64,
        )
        if image_to_canonical.shape != (3, 3):
            raise ValueError(f"相机 {camera_id} 缺少 image_to_canonical_transform")
        native_k = np.linalg.inv(image_to_canonical) @ canonical_k
        native_k /= native_k[2, 2]
        camera.K = native_k
        camera.dist_coeffs = shared_d.copy()
        camera.R = np.asarray(pose.get("R"), dtype=np.float64)
        camera.t = np.asarray(pose.get("T_metres"), dtype=np.float64).reshape(3)
        camera.camera_model = "OPENCV_FISHEYE"
        camera.reprojection_rmse = float(
            pose.get("pose_reprojection_rmse_px", 0.0)
        )
        pose_plausible = bool(
            pose.get("physical_plausibility_passed", False)
        )
        quality_accepted = (
            route == "credible_fitted_fisheye" and pose_plausible
        )
        cameras[camera_id] = {
            "camera_id": camera_id,
            "image_path": str(Path(camera.image_path).resolve())
            if camera.image_path
            else None,
            "image_size": list(camera.image_size or (0, 0)),
            "camera_model": "opencv_fisheye_4",
            "K": native_k.tolist(),
            "D": shared_d.tolist(),
            "R": camera.R.tolist(),
            "t": camera.t.tolist(),
            "extrinsic_direction": "world_to_camera",
            "world_frame_id": "store_ground_world",
            "unit": "metre",
            "source": "storevision_estimated",
            "quality_status": (
                "accepted" if quality_accepted else "provisional"
            ),
            "quality_reasons": (
                []
                if quality_accepted
                else list(
                    pose.get(
                        "physical_plausibility_reasons",
                        ["内参或逐机位姿物理门禁未完整通过"],
                    )
                )
            ),
            "reprojection_rmse_px": camera.reprojection_rmse,
        }
    if not cameras:
        raise ValueError("共享标定报告没有与当前数据集对应的相机")

    # H 是当前 2.5D 的直接运行输入，必须使用本轮活动 K/D 重新计算，
    # 不能只把原始 cali 当作已经解析完成的中间层。
    calibrate_homographies(dataset)
    projections = {
        camera.device_serial: {
            "camera_id": camera.device_serial,
            "H_image_to_plan": camera.homography.tolist(),
            "H_direction": "undistorted_image_pixel_to_floorplan_pixel",
            "H_source": "cali_control_points_with_active_intrinsics",
            "plan_frame_id": "floorplan_pixel",
            "quality": {
                "reprojection_rmse_floor_px": camera.reprojection_rmse
            },
        }
        for camera in dataset.camera_list()
        if camera.homography is not None
    }
    return (
        {
            "schema_version": INTERMEDIATE_SCHEMA_VERSION,
            "candidate_name": "estimated",
            "source": "storevision_estimated",
            "camera_model_policy": "per_camera_discriminated_union",
            "intrinsics_route": route,
            "intrinsics_validation": validation,
            "coordinate_conventions": report.get("coordinate_conventions", {}),
            "cameras": cameras,
        },
        {
            "schema_version": INTERMEDIATE_SCHEMA_VERSION,
            "candidate_name": "estimated",
            "source": "cali_control_points",
            "projections": projections,
        },
    )


def _fs_real(storage: cv2.FileStorage, key: str) -> float | None:
    """读取 OpenCV FileStorage 标量。"""

    node = storage.getNode(key)
    return None if node.empty() else float(node.real())


def _fs_matrix(storage: cv2.FileStorage, key: str) -> np.ndarray | None:
    """读取 OpenCV FileStorage 矩阵。"""

    node = storage.getNode(key)
    if node.empty():
        return None
    matrix = node.mat()
    return None if matrix is None else np.asarray(matrix, dtype=np.float64)


def _parse_external_candidate(
    dataset_path: Path,
    dataset: StoreDataset,
) -> dict[str, Any] | None:
    """兼容精确参数项目的 intrinsic/extrinsic YML 输入。

    外部参数只在三类 ID 能配对时进入候选；没有明确世界到平面图配准时，
    它不会自动获得 2.5D 平面映射能力。
    """

    roots = (
        dataset_path / "external_calibration",
        dataset_path,
    )
    root = next(
        (
            candidate
            for candidate in roots
            if (candidate / "intrinsic").is_dir()
            and (candidate / "extrinsic").is_dir()
        ),
        None,
    )
    if root is None:
        return None
    registration_path = root / "registration.json"
    registration = (
        _read_json(registration_path)
        if registration_path.is_file()
        else {}
    )
    expected_axes = "X right, Y plan-up, Z up"
    try:
        target_plane_z = float(registration.get("target_plane_z"))
    except (TypeError, ValueError):
        target_plane_z = float("nan")
    registration_verified = bool(
        registration.get("verified")
        and registration.get("world_frame_id") == "store_ground_world"
        and registration.get("unit") == "metre"
        and registration.get("axis_definition") == expected_axes
        and target_plane_z == 0.0
    )
    world_frame_id = (
        str(registration.get("world_frame_id"))
        if registration_verified and registration.get("world_frame_id")
        else "external_calibration_world"
    )
    world_unit = (
        str(registration.get("unit"))
        if registration_verified and registration.get("unit")
        else "unverified"
    )
    cameras: dict[str, Any] = {}
    for intrinsic_path in sorted((root / "intrinsic").glob("*.yml")):
        camera_id = intrinsic_path.stem
        extrinsic_path = root / "extrinsic" / f"{camera_id}.yml"
        camera = dataset.cameras.get(camera_id)
        if not extrinsic_path.is_file() or camera is None or not camera.image_path:
            continue
        intrinsic_text = intrinsic_path.read_text(encoding="utf-8")
        extrinsic_text = extrinsic_path.read_text(encoding="utf-8")
        intrinsic = cv2.FileStorage(
            intrinsic_text,
            cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY,
        )
        extrinsic = cv2.FileStorage(
            extrinsic_text,
            cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY,
        )
        try:
            K = _fs_matrix(intrinsic, "camera_matrix")
            D = _fs_matrix(intrinsic, "distortion_coefficients")
            rvec = _fs_matrix(extrinsic, "rvec")
            tvec = _fs_matrix(extrinsic, "tvec")
            avg_error = _fs_real(intrinsic, "avg_reprojection_error")
            is_fisheye = float(_fs_real(intrinsic, "is_fisheye") or 0.0)
            width = int(_fs_real(intrinsic, "image_width") or 0)
            height = int(_fs_real(intrinsic, "image_height") or 0)
        finally:
            intrinsic.release()
            extrinsic.release()
        if any(value is None for value in (K, D, rvec, tvec)):
            continue
        coefficients = np.asarray(D, dtype=np.float64).reshape(-1)
        expected = 4 if is_fisheye else 5
        if (
            np.asarray(K).shape != (3, 3)
            or coefficients.size != expected
            or np.asarray(rvec).size != 3
            or np.asarray(tvec).size != 3
        ):
            continue
        R, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3, 1))
        cameras[camera_id] = {
            "camera_id": camera_id,
            "image_path": str(Path(camera.image_path).resolve()),
            "image_size": [width, height],
            "camera_model": (
                "opencv_fisheye_4" if is_fisheye else "opencv_pinhole_5"
            ),
            "K": np.asarray(K, dtype=np.float64).tolist(),
            "D": coefficients.tolist(),
            "R": R.tolist(),
            "t": np.asarray(tvec, dtype=np.float64).reshape(3).tolist(),
            "extrinsic_direction": "world_to_camera",
            "world_frame_id": world_frame_id,
            "unit": world_unit,
            "source": "external_calibration",
            "quality_status": "reference_candidate",
            "avg_reprojection_error_px": avg_error,
        }
    if not cameras:
        return None
    return {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "candidate_name": "external",
        "source": "external_calibration",
        "camera_model_policy": "per_camera_discriminated_union",
        "coordinate_conventions": {
            "extrinsics": "world_to_camera: X_camera=R*X_world+t",
            "world_frame": "必须由 world_frame/plan registration 独立确认",
        },
        "registration": {
            "verified": registration_verified,
            "source": str(registration_path) if registration_path.is_file() else None,
            "world_frame_id": world_frame_id,
            "unit": world_unit,
            "policy": (
                "仅接受 store_ground_world、metre、X right/Y plan-up/Z up "
                "和 Z=0 的已验证声明；不会猜测轴向或缩放。"
            ),
        },
        "quality_status": "reference_candidate",
        "cameras": cameras,
    }


def _external_planar_projection(
    external: dict[str, Any],
    scale_summary: dict[str, Any],
    floor_size: tuple[int, int] | None,
) -> dict[str, Any] | None:
    """从已注册精确 K/R/t 候选推导去畸变图到平面图的 H。

    未通过显式注册声明的外部世界系不会生成 H，避免把参数数值精度误当成
    世界坐标、单位、轴向和目标平面也已经得到业务验收。
    """

    if not external.get("registration", {}).get("verified"):
        return None
    model = scale_summary.get("scale_model", {})
    if not model or not floor_size:
        return None
    x_scale = float(model["x_metres_per_percent"])
    x_intercept = float(model["x_intercept_metres"])
    y_scale = float(model["y_metres_per_percent"])
    y_intercept = float(model["y_intercept_metres"])
    floor_width, floor_height = floor_size
    world_to_plan = np.asarray(
        [
            [
                floor_width / (100.0 * x_scale),
                0.0,
                -floor_width * x_intercept / (100.0 * x_scale),
            ],
            [
                0.0,
                -floor_height / (100.0 * y_scale),
                -floor_height * y_intercept / (100.0 * y_scale),
            ],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    projections: dict[str, Any] = {}
    for camera_id, camera in external.get("cameras", {}).items():
        K = np.asarray(camera["K"], dtype=np.float64)
        R = np.asarray(camera["R"], dtype=np.float64)
        t = np.asarray(camera["t"], dtype=np.float64).reshape(3)
        world_to_undistorted = K @ np.column_stack(
            [R[:, 0], R[:, 1], t]
        )
        if abs(float(np.linalg.det(world_to_undistorted))) < 1e-12:
            continue
        image_to_plan = world_to_plan @ np.linalg.inv(
            world_to_undistorted
        )
        image_to_plan /= image_to_plan[2, 2]
        projections[camera_id] = {
            "camera_id": camera_id,
            "H_image_to_plan": image_to_plan.tolist(),
            "H_direction": "undistorted_image_pixel_to_floorplan_pixel",
            "H_source": "registered_external_K_R_t_and_scale",
            "plan_frame_id": "floorplan_pixel",
            "quality": {
                "status": "inherits_external_registration_and_calibration"
            },
        }
    if not projections:
        return None
    return {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "candidate_name": "external",
        "source": "registered_external_K_R_t_and_scale",
        "projections": projections,
    }


def publish_intermediate_package(
    dataset_path: str | Path,
    package_path: str | Path,
    shared_report: dict[str, Any],
    *,
    cfg: StoreConfig | None = None,
    shared_result_path: str | Path | None = None,
) -> dict[str, Any]:
    """把输入优化结果发布为业务页面可直接消费的不可变中间层。"""

    dataset_root = Path(dataset_path).expanduser().resolve()
    package = Path(package_path).expanduser().resolve()
    package.mkdir(parents=True, exist_ok=True)
    dataset = load_store_folder(dataset_root)
    runtime_cfg, scale_summary = _plan_runtime_config(
        dataset, cfg or StoreConfig()
    )
    rig, projections = _apply_shared_report(dataset, shared_report)

    estimated_rig_path = package / "calibrations" / "estimated" / "camera_rig.json"
    estimated_projection_path = (
        package / "planar_projections" / "estimated.json"
    )
    _write_json(estimated_rig_path, rig)
    _write_json(estimated_projection_path, projections)

    # 2.5D 检测仍需要相机在平面图上的控制区域和可选叠加线。将其作为
    # 中间层资源固化，避免业务运行时再次解析 cali/scale 原始格式。
    controls_path = (
        package / "observations" / "camera_plan_controls.json"
    )
    controls_payload = {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "source": "normalized_input_snapshot",
        "cameras": {
            camera.device_serial: {
                "camera_id": camera.device_serial,
                "name": camera.name,
                "serialnum": camera.serialnum,
                "camera_points": [
                    list(point.as_tuple()) for point in camera.camera_points
                ],
                "map_points_percent": [
                    list(point.as_tuple()) for point in camera.map_points
                ],
                "overlay_polygons_image": [
                    [list(point.as_tuple()) for point in polygon]
                    for polygon in camera.overlay_polygons_img
                ],
                "calibration_size": list(camera.calibration_size)
                if camera.calibration_size
                else None,
                "coordinate_mode": camera.coordinate_mode,
                "calibration_to_image": (
                    np.asarray(
                        camera.calibration_to_image,
                        dtype=np.float64,
                    ).tolist()
                ),
                "input_issues": camera.input_issues,
            }
            for camera in dataset.camera_list()
        },
    }
    _write_json(controls_path, controls_payload)

    candidates: dict[str, Any] = {
        "estimated": {
            "path": _relative(package, estimated_rig_path),
            "source": "storevision_estimated",
            "quality_status": (
                "accepted"
                if all(
                    camera.get("quality_status") == "accepted"
                    for camera in rig["cameras"].values()
                )
                else "provisional"
            ),
        }
    }
    projection_candidates: dict[str, Any] = {
        "estimated": {
            "path": _relative(package, estimated_projection_path),
            "source": projections["source"],
        }
    }
    external = _parse_external_candidate(dataset_root, dataset)
    if external is not None:
        external_path = (
            package / "calibrations" / "external" / "camera_rig.json"
        )
        _write_json(external_path, external)
        candidates["external"] = {
            "path": _relative(package, external_path),
            "source": "external_calibration",
            "quality_status": "reference_candidate",
        }
        external_projection = _external_planar_projection(
            external,
            scale_summary,
            dataset.floor_plan_size,
        )
        if external_projection is not None:
            external_projection_path = (
                package / "planar_projections" / "external.json"
            )
            _write_json(external_projection_path, external_projection)
            projection_candidates["external"] = {
                "path": _relative(package, external_projection_path),
                "source": external_projection["source"],
            }

    plan_path = package / "frames" / "plan_registration.json"
    ground_path = package / "frames" / "ground_plane.json"
    height_path = package / "business" / "height_policy.json"
    plan_payload = {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "plan_frame_id": "floorplan_pixel",
        "floorplan_path": str(Path(dataset.floor_plan_path).resolve())
        if dataset.floor_plan_path
        else None,
        "floorplan_size": list(dataset.floor_plan_size or (0, 0)),
        "plan_width_cm": runtime_cfg.floor_plan_width_cm,
        "plan_height_cm": runtime_cfg.floor_plan_height_cm,
        "plan_to_metric": {
            "model": "axis_aligned_floor_extent",
            "unit": "centimetre",
            "x_cm_per_pixel": (
                runtime_cfg.floor_plan_width_cm
                / max((dataset.floor_plan_size or (1, 1))[0], 1)
            ),
            "y_cm_per_pixel": (
                runtime_cfg.floor_plan_height_cm
                / max((dataset.floor_plan_size or (1, 1))[1], 1)
            ),
        },
        "scale": scale_summary,
    }
    _write_json(plan_path, plan_payload)
    _write_json(
        ground_path,
        {
            "schema_version": INTERMEDIATE_SCHEMA_VERSION,
            "world_frame_id": "store_ground_world",
            "unit": "metre",
            "axis_definition": "X right, Y plan-up, Z up",
            "extrinsic_direction": "world_to_camera",
            "verification_status": "verified",
            "plane": {"normal": [0.0, 0.0, 1.0], "offset": 0.0},
            "source": "scale_and_cali_contract",
        },
    )
    _write_json(
        height_path,
        {
            "schema_version": INTERMEDIATE_SCHEMA_VERSION,
            "policy": "fixed_business_height",
            "table_height_cm": runtime_cfg.table_height_cm,
            "shelf_height_cm": runtime_cfg.shelf_height_cm,
            "measured_from_images": False,
        },
    )

    images = [
        {
            "camera_id": camera.device_serial,
            "path": str(Path(camera.image_path).resolve())
            if camera.image_path
            else None,
            "image_size": list(camera.image_size or (0, 0)),
        }
        for camera in dataset.camera_list()
    ]
    manifest = {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "dataset_id": dataset_root.name,
        "producer": "input_optimization",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "input": {
            "dataset_path": str(dataset_root),
            "images": images,
            "floorplan_path": str(Path(dataset.floor_plan_path).resolve())
            if dataset.floor_plan_path
            else None,
            "scale_path": str(Path(dataset.scale_path).resolve())
            if dataset.scale_path
            else None,
            "camera_plan_controls": _relative(package, controls_path),
        },
        "calibration_candidates": candidates,
        "active_calibration": "estimated",
        "planar_projection_candidates": projection_candidates,
        "active_planar_projection": "estimated",
        "frames": {
            "ground_plane": _relative(package, ground_path),
            "plan_registration": _relative(package, plan_path),
        },
        "business": {
            "height_policy": _relative(package, height_path),
        },
        "lineage": {
            "producer": "input_optimization",
            "shared_result_path": str(
                Path(shared_result_path).expanduser().resolve()
            )
            if shared_result_path
            else None,
            "raw_inputs_modified": False,
        },
    }
    manifest_path = package / "manifest.json"
    _write_json(manifest_path, manifest)
    capabilities = resolve_capabilities(package, candidate_name="estimated")
    manifest["capabilities"] = capabilities.as_dict()
    _write_json(manifest_path, manifest)
    return manifest


def load_manifest(package_path: str | Path) -> dict[str, Any]:
    """读取并校验中间层清单的基本身份。"""

    package = Path(package_path).expanduser().resolve()
    manifest = _read_json(package / "manifest.json")
    if manifest.get("package_type") != PACKAGE_TYPE:
        raise ValueError(f"目录不是 Store Vision 中间层数据包：{package}")
    if manifest.get("schema_version") not in SUPPORTED_INTERMEDIATE_SCHEMA_VERSIONS:
        raise ValueError(
            "不支持的中间层 schema_version："
            f"{manifest.get('schema_version')}"
        )
    _safe_dataset_id(manifest.get("dataset_id"))
    return manifest


def candidate_names(package_path: str | Path) -> tuple[str, ...]:
    """列出中间层中的标定候选名称。"""

    manifest = load_manifest(package_path)
    candidates = manifest.get("calibration_candidates", {})
    active = str(manifest.get("active_calibration", ""))
    names = sorted(str(name) for name in candidates)
    return tuple(
        ([active] if active in names else [])
        + [name for name in names if name != active]
    )


def _valid_numeric_shape(value: Any, shape: tuple[int, ...]) -> bool:
    """判断 JSON 数组是否为指定形状且仅包含有限数值。"""

    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return False
    return array.shape == shape and bool(np.isfinite(array).all())


def _candidate_payload(
    package: Path,
    manifest: dict[str, Any],
    candidate_name: str,
) -> dict[str, Any] | None:
    item = manifest.get("calibration_candidates", {}).get(candidate_name)
    if not isinstance(item, dict) or not item.get("path"):
        return None
    return _read_json(_resource_path(package, str(item["path"])))


def _projection_payload(
    package: Path,
    manifest: dict[str, Any],
    candidate_name: str,
) -> dict[str, Any] | None:
    item = manifest.get("planar_projection_candidates", {}).get(
        candidate_name
    )
    if not isinstance(item, dict) or not item.get("path"):
        return None
    return _read_json(_resource_path(package, str(item["path"])))


def resolve_capabilities(
    package_path: str | Path,
    *,
    candidate_name: str | None = None,
) -> CapabilityReport:
    """根据版本化资源键判断业务能力，不根据任意文件名做启发式猜测。"""

    package = Path(package_path).expanduser().resolve()
    manifest = load_manifest(package)
    candidate = candidate_name or str(manifest.get("active_calibration", ""))
    rig = _candidate_payload(package, manifest, candidate)
    projection = _projection_payload(package, manifest, candidate)
    inputs = manifest.get("input", {})
    images = [
        row
        for row in inputs.get("images", [])
        if isinstance(row, dict) and row.get("path")
    ]
    images_by_id = {
        str(row.get("camera_id")): row
        for row in images
        if row.get("camera_id")
    }
    cameras = rig.get("cameras", {}) if rig else {}
    calibration_missing: list[str] = []
    if not images:
        calibration_missing.append("camera_images")
    if not cameras:
        calibration_missing.append(f"calibration_candidate:{candidate}")
    else:
        required = (
            "K",
            "D",
            "R",
            "t",
            "camera_model",
            "image_path",
            "image_size",
        )
        for camera_id, camera in cameras.items():
            for key in required:
                if camera.get(key) in (None, [], ""):
                    calibration_missing.append(
                        f"camera:{camera_id}:{key}"
                    )
            image_path = Path(str(camera.get("image_path", "")))
            if not image_path.is_file():
                calibration_missing.append(
                    f"camera:{camera_id}:image_file"
                )
            image_size = camera.get("image_size")
            if (
                not isinstance(image_size, list)
                or len(image_size) != 2
                or any(
                    not isinstance(value, (int, float)) or value <= 0
                    for value in image_size
                )
            ):
                calibration_missing.append(
                    f"camera:{camera_id}:image_size"
                )
            source_image = images_by_id.get(str(camera_id))
            if source_image is None:
                calibration_missing.append(
                    f"camera:{camera_id}:input_image_registration"
                )
            elif list(source_image.get("image_size", [])) != image_size:
                calibration_missing.append(
                    f"camera:{camera_id}:image_size_registration"
                )
            model = camera.get("camera_model")
            distortion_size = 4 if model == "opencv_fisheye_4" else 5
            if model not in {"opencv_fisheye_4", "opencv_pinhole_5"}:
                calibration_missing.append(
                    f"camera:{camera_id}:supported_camera_model"
                )
            if not _valid_numeric_shape(camera.get("K"), (3, 3)):
                calibration_missing.append(f"camera:{camera_id}:K_shape")
            if not _valid_numeric_shape(
                camera.get("D"), (distortion_size,)
            ):
                calibration_missing.append(f"camera:{camera_id}:D_shape")
            if not _valid_numeric_shape(camera.get("R"), (3, 3)):
                calibration_missing.append(f"camera:{camera_id}:R_shape")
            if not _valid_numeric_shape(camera.get("t"), (3,)):
                calibration_missing.append(f"camera:{camera_id}:t_shape")
    calibration_warnings = tuple(
        sorted(
            {
                f"标定候选 {candidate} 的可信状态为 "
                f"{camera.get('quality_status')}"
                for camera in cameras.values()
                if camera.get("quality_status")
                not in {"accepted", "reference"}
            }
        )
    )
    calibration_state = CapabilityState(
        not calibration_missing,
        tuple(calibration_missing),
        calibration_warnings,
        "blocked" if calibration_missing else (
            "ready_provisional" if calibration_warnings else "ready_verified"
        ),
    )

    projections = projection.get("projections", {}) if projection else {}
    planar_missing: list[str] = []
    if not projections:
        planar_missing.append(
            f"planar_projection_candidate:{candidate}"
        )
    for camera_id in cameras:
        if camera_id not in projections:
            planar_missing.append(f"camera:{camera_id}:H_image_to_plan")
        elif not _valid_numeric_shape(
            projections[camera_id].get("H_image_to_plan"),
            (3, 3),
        ):
            planar_missing.append(
                f"camera:{camera_id}:H_image_to_plan_shape"
            )
    planar_state = CapabilityState(
        not planar_missing,
        tuple(planar_missing),
        (),
        "ready_verified" if not planar_missing else "blocked",
    )

    frames = manifest.get("frames", {})
    plan_item = frames.get("plan_registration")
    plan = (
        _read_json(_resource_path(package, str(plan_item)))
        if plan_item and _resource_path(package, str(plan_item)).is_file()
        else {}
    )
    metric_missing: list[str] = []
    if not plan.get("plan_to_metric"):
        metric_missing.append("plan_to_metric")
    if not plan.get("plan_width_cm") or not plan.get("plan_height_cm"):
        metric_missing.append("metric_plan_extent")
    metric_warnings = (
        ("未提供可拟合的 scale，当前使用配置中的默认平面尺寸。",)
        if plan.get("scale", {}).get("status") == "default_dimensions"
        else ()
    )
    metric_state = CapabilityState(
        not metric_missing,
        tuple(metric_missing),
        metric_warnings,
        "blocked" if metric_missing else (
            "ready_provisional" if metric_warnings else "ready_verified"
        ),
    )

    floorplan = inputs.get("floorplan_path")
    controls_item = inputs.get("camera_plan_controls")
    controls = (
        _read_json(_resource_path(package, str(controls_item)))
        if controls_item and _resource_path(package, str(controls_item)).is_file()
        else {}
    )
    control_cameras = controls.get("cameras", {})
    height_item = manifest.get("business", {}).get("height_policy")
    map_missing = list(calibration_state.missing)
    map_missing.extend(planar_state.missing)
    map_missing.extend(metric_state.missing)
    if not floorplan or not Path(str(floorplan)).is_file():
        map_missing.append("floorplan_image")
    if not height_item or not _resource_path(package, str(height_item)).is_file():
        map_missing.append("height_policy")
    if not control_cameras:
        map_missing.append("camera_plan_controls")
    else:
        for camera_id in cameras:
            row = control_cameras.get(camera_id, {})
            map_points = row.get("map_points_percent", [])
            if not isinstance(map_points, list) or len(map_points) < 3:
                map_missing.append(
                    f"camera:{camera_id}:map_points_percent"
                )
    map_state = CapabilityState(
        not map_missing,
        tuple(dict.fromkeys(map_missing)),
        tuple(
            dict.fromkeys(
                calibration_state.warnings + metric_state.warnings
            )
        ),
        (
            "blocked" if map_missing else (
                "ready_provisional"
                if calibration_state.warnings or metric_state.warnings
                else "ready_verified"
            )
        ),
    )

    ground_item = frames.get("ground_plane")
    stitch_missing = list(calibration_state.missing)
    coordinate_warnings: list[str] = []
    if not ground_item or not _resource_path(package, str(ground_item)).is_file():
        stitch_missing.append("ground_plane")
    else:
        ground = _read_json(_resource_path(package, str(ground_item)))
        expected_frame = ground.get("world_frame_id")
        expected_unit = ground.get("unit")
        if ground.get("extrinsic_direction", "world_to_camera") != "world_to_camera":
            stitch_missing.append("world_to_camera_extrinsic_direction")
        plane = ground.get("plane", {})
        if not _valid_numeric_shape(plane.get("normal"), (3,)):
            stitch_missing.append("ground_plane_normal")
        if not isinstance(plane.get("offset"), (int, float)):
            stitch_missing.append("ground_plane_offset")
        contract_status = str(ground.get("verification_status", "verified"))
        if contract_status not in {"verified", "declared", "assumed"}:
            stitch_missing.append("world_coordinate_contract")
        elif contract_status != "verified":
            coordinate_warnings.append(
                f"世界坐标合同为 {contract_status}，输出仅可作为 geometric_trial。"
            )
        for camera_id, camera in cameras.items():
            if camera.get("extrinsic_direction") != "world_to_camera":
                stitch_missing.append(
                    f"camera:{camera_id}:extrinsic_direction"
                )
            if camera.get("world_frame_id") != expected_frame:
                stitch_missing.append(
                    f"camera:{camera_id}:world_frame_registration"
                )
            if camera.get("unit") != expected_unit:
                stitch_missing.append(
                    f"camera:{camera_id}:world_unit_registration"
                )
            if _valid_numeric_shape(camera.get("R"), (3, 3)):
                rotation = np.asarray(camera["R"], dtype=np.float64)
                if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5):
                    stitch_missing.append(
                        f"camera:{camera_id}:rotation_orthogonality"
                    )
                if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-5):
                    stitch_missing.append(
                        f"camera:{camera_id}:rotation_determinant"
                    )
    stitch_state = CapabilityState(
        not stitch_missing,
        tuple(dict.fromkeys(stitch_missing)),
        tuple(dict.fromkeys(calibration_state.warnings + tuple(coordinate_warnings))),
        (
            "blocked" if stitch_missing else (
                "ready_provisional"
                if calibration_state.warnings or coordinate_warnings
                else "ready_verified"
            )
        ),
    )

    sfm_item = manifest.get("sfm_evidence")
    sfm_payload: dict[str, Any] = {}
    if isinstance(sfm_item, dict) and sfm_item.get("path"):
        sfm_path = _resource_path(package, str(sfm_item["path"]))
        if sfm_path.is_file():
            sfm_payload = _read_json(sfm_path)
    accepted_camera_ids = sfm_payload.get("accepted_camera_ids", [])
    sfm_ready = bool(
        sfm_payload.get("calibration_candidate") == candidate
        and isinstance(accepted_camera_ids, list)
        and accepted_camera_ids
    )
    sfm_state = CapabilityState(
        sfm_ready,
        () if sfm_ready else ("sfm_evidence_for_candidate",),
        ("SfM 是可选相机观测证据，不阻塞基础 2.5D 或拼接。",),
        "ready_provisional" if sfm_ready else "blocked",
    )
    return CapabilityReport(
        calibration=calibration_state,
        planar_mapping=planar_state,
        metric_plan=metric_state,
        map25d=map_state,
        stitching=stitch_state,
        sfm_assisted=sfm_state,
    )


def load_sfm_evidence(package_path: str | Path) -> dict[str, Any] | None:
    """读取与中间层绑定的规范化 SfM 证据；缺失时返回 ``None``。"""

    package = Path(package_path).expanduser().resolve()
    manifest = load_manifest(package)
    item = manifest.get("sfm_evidence")
    if not isinstance(item, dict) or not item.get("path"):
        return None
    path = _resource_path(package, str(item["path"]))
    return _read_json(path) if path.is_file() else None


def _copy_manifest_resource(
    base: Path,
    derived: Path,
    relative_value: str,
) -> None:
    """把父中间层的小型合同资源复制到派生包的相同相对位置。"""

    source = _resource_path(base, relative_value)
    target = derived / relative_value
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def publish_sfm_derived_package(
    base_package_path: str | Path,
    experiment_path: str | Path,
    output_root: str | Path,
    *,
    candidate_name: str | None = None,
) -> Path:
    """把一次 SfM 结果发布为不修改父包的轻量派生中间层。"""

    base = Path(base_package_path).expanduser().resolve()
    experiment = Path(experiment_path).expanduser().resolve()
    manifest = json.loads(json.dumps(load_manifest(base)))
    candidate = candidate_name or str(manifest.get("active_calibration", ""))
    summary_path = experiment / "reports" / "sfm_registration_summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"SfM 缺少注册摘要：{summary_path}")
    summary = _read_json(summary_path)
    images = manifest.get("input", {}).get("images", [])
    camera_by_image = {
        Path(str(row.get("path"))).name: str(row.get("camera_id"))
        for row in images
        if isinstance(row, dict) and row.get("path") and row.get("camera_id")
    }
    accepted_names = [
        str(name) for name in summary.get("visually_registered_images", [])
    ]
    accepted_ids = [
        camera_by_image[name] for name in accepted_names if name in camera_by_image
    ]
    per_image = summary.get("per_image_point3d_observations", {})
    per_camera = {
        camera_by_image[name]: int(count)
        for name, count in per_image.items()
        if name in camera_by_image and isinstance(count, (int, float))
    }

    derived = create_unique_run_directory(output_root)
    for section_name in ("calibration_candidates", "planar_projection_candidates"):
        for item in manifest.get(section_name, {}).values():
            if isinstance(item, dict) and item.get("path"):
                _copy_manifest_resource(base, derived, str(item["path"]))
    for section_name in ("frames", "business"):
        for value in manifest.get(section_name, {}).values():
            if isinstance(value, str):
                _copy_manifest_resource(base, derived, value)
    controls = manifest.get("input", {}).get("camera_plan_controls")
    if isinstance(controls, str):
        _copy_manifest_resource(base, derived, controls)

    evidence_path = derived / "evidence" / "sfm.json"
    evidence = {
        "schema_version": 1,
        "status": str(summary.get("status", "unknown")),
        "calibration_candidate": candidate,
        "source_intermediate": str(base),
        "experiment_path": str(experiment),
        "selected_candidate": summary.get("selected_candidate"),
        "accepted_camera_ids": accepted_ids,
        "visually_registered_images": accepted_names,
        "weak_or_prior_only_images": summary.get(
            "weak_or_prior_only_images", []
        ),
        "unregistered_images": summary.get("unregistered_images", []),
        "per_camera_point3d_observations": per_camera,
        "sparse_points": summary.get("sparse_points"),
        "mean_track_length": summary.get("mean_track_length"),
        "mean_reprojection_error_px": summary.get(
            "mean_reprojection_error_px"
        ),
        "interpretation": (
            "仅表示来源相机具有共同三维观测，不提供门店 XY、尺度或桌面高度。"
        ),
    }
    _write_json(evidence_path, evidence)
    manifest["schema_version"] = INTERMEDIATE_SCHEMA_VERSION
    manifest["generated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    manifest["producer"] = "sfm_derivation"
    manifest["sfm_evidence"] = {
        "path": _relative(derived, evidence_path),
        "status": evidence["status"],
        "calibration_candidate": candidate,
    }
    lineage = manifest.setdefault("lineage", {})
    lineage["producer"] = "sfm_derivation"
    lineage["parent_intermediate"] = str(base)
    lineage["sfm_experiment"] = str(experiment)
    manifest_path = derived / "manifest.json"
    _write_json(manifest_path, manifest)
    manifest["capabilities"] = resolve_capabilities(
        derived, candidate_name=candidate
    ).as_dict()
    _write_json(manifest_path, manifest)
    return derived


def _external_image_id(path: Path) -> str:
    """按精确参数项目规则从图片名恢复相机 ID。"""

    return path.name.split(".mp4_", 1)[0] if ".mp4_" in path.name else path.stem


def import_external_calibration_package(
    source_path: str | Path,
    package_path: str | Path,
    *,
    copy_source: bool = True,
) -> dict[str, Any]:
    """把图片和精确 K/D/R/t 规范化为可独立选择的中间层。"""

    original_source = Path(source_path).expanduser().resolve()
    package = Path(package_path).expanduser().resolve()
    if package == original_source or package.is_relative_to(original_source):
        raise ValueError("外部参数中间层输出不能位于源数据目录内部")
    package.mkdir(parents=True, exist_ok=True)
    source = original_source
    embedded_source = package / "source"
    if copy_source and original_source != embedded_source:
        if embedded_source.exists():
            raise FileExistsError(f"中间层 source 已存在：{embedded_source}")
        shutil.copytree(original_source, embedded_source)
        source = embedded_source
    image_root = source / "screenshots"
    if not image_root.is_dir():
        image_root = source / "image"
    image_paths = sorted(
        path
        for path in image_root.rglob("*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )
    cameras: dict[str, CameraCalibration] = {}
    for image_path in image_paths:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        camera_id = _external_image_id(image_path)
        cameras[camera_id] = CameraCalibration(
            device_serial=camera_id,
            name=camera_id,
            serialnum=camera_id,
            camera_points=[],
            map_points=[],
            image_path=str(image_path),
            image_size=(image.shape[1], image.shape[0]),
            image_match_strategy="external_parameter_import",
        )
    dataset = StoreDataset(
        root=str(source),
        floor_plan_path=None,
        cameras=cameras,
    )
    rig = _parse_external_candidate(source, dataset)
    if rig is None:
        raise ValueError("外部参数包没有形成图片、内参、外参完全配对的相机")
    rig["registration"] = {
        "verified": False,
        "verification_status": "assumed",
        "world_frame_id": "external_calibration_world",
        "unit": "metre",
        "axis_definition": "X right, Y down, Z up",
        "target_plane_z": 0.0,
        "policy": "沿用精确参数项目的几何试算假设，不等同于独立控制点验收。",
    }
    rig["quality_status"] = "geometric_trial"
    for camera in rig.get("cameras", {}).values():
        camera["world_frame_id"] = "external_calibration_world"
        camera["unit"] = "metre"
        camera["quality_status"] = "geometric_trial"
    rig_path = package / "calibrations" / "external" / "camera_rig.json"
    ground_path = package / "frames" / "ground_plane.json"
    _write_json(rig_path, rig)
    _write_json(
        ground_path,
        {
            "schema_version": INTERMEDIATE_SCHEMA_VERSION,
            "world_frame_id": "external_calibration_world",
            "unit": "metre",
            "axis_definition": "X right, Y down, Z up",
            "extrinsic_direction": "world_to_camera",
            "verification_status": "assumed",
            "plane": {"normal": [0.0, 0.0, 1.0], "offset": 0.0},
            "source": "external_parameter_project_geometric_trial",
        },
    )
    images = [
        {
            "camera_id": camera_id,
            "path": camera["image_path"],
            "image_size": camera["image_size"],
        }
        for camera_id, camera in rig["cameras"].items()
    ]
    manifest = {
        "schema_version": INTERMEDIATE_SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "dataset_id": _safe_dataset_id(original_source.name),
        "producer": "external_parameter_import",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "input": {
            "dataset_path": str(source),
            "images": images,
            "floorplan_path": None,
        },
        "calibration_candidates": {
            "external": {
                "path": _relative(package, rig_path),
                "source": "external_calibration",
                "quality_status": "geometric_trial",
            }
        },
        "active_calibration": "external",
        "planar_projection_candidates": {},
        "active_planar_projection": None,
        "frames": {"ground_plane": _relative(package, ground_path)},
        "business": {},
        "lineage": {
            "producer": "external_parameter_import",
            "source_path": str(original_source),
            "embedded_source": str(source),
            "raw_inputs_modified": False,
        },
    }
    manifest_path = package / "manifest.json"
    _write_json(manifest_path, manifest)
    manifest["capabilities"] = resolve_capabilities(
        package, candidate_name="external"
    ).as_dict()
    _write_json(manifest_path, manifest)
    return manifest


def load_intermediate_runtime(
    package_path: str | Path,
    *,
    candidate_name: str | None = None,
    cfg: StoreConfig | None = None,
) -> IntermediateRuntime:
    """恢复候选 K/D/R/t/H、米制平面与原始图片引用。"""

    package = Path(package_path).expanduser().resolve()
    manifest = load_manifest(package)
    candidate = candidate_name or str(manifest.get("active_calibration", ""))
    rig = _candidate_payload(package, manifest, candidate)
    if rig is None:
        raise ValueError(f"中间层缺少标定候选：{candidate}")
    projection = _projection_payload(package, manifest, candidate)
    inputs = manifest.get("input", {})
    dataset_path = inputs.get("dataset_path") or str(package)
    plan_item = manifest.get("frames", {}).get("plan_registration")
    plan = (
        _read_json(_resource_path(package, str(plan_item)))
        if plan_item
        else {}
    )
    controls_item = inputs.get("camera_plan_controls")
    controls = (
        _read_json(_resource_path(package, str(controls_item)))
        if controls_item
        else {}
    )
    control_cameras = controls.get("cameras", {})
    cameras = rig.get("cameras", {})
    projections = projection.get("projections", {}) if projection else {}
    runtime_cameras: dict[str, CameraCalibration] = {}
    for camera_id, payload in cameras.items():
        if not isinstance(payload, dict):
            continue
        control = control_cameras.get(camera_id, {})
        camera = CameraCalibration(
            device_serial=camera_id,
            name=str(control.get("name") or camera_id),
            serialnum=str(control.get("serialnum") or camera_id),
            camera_points=[
                Point2D(float(point[0]), float(point[1]))
                for point in control.get("camera_points", [])
                if isinstance(point, list) and len(point) == 2
            ],
            map_points=[
                Point2D(float(point[0]), float(point[1]))
                for point in control.get("map_points_percent", [])
                if isinstance(point, list) and len(point) == 2
            ],
            image_path=str(payload.get("image_path"))
            if payload.get("image_path")
            else None,
            overlay_polygons_img=[
                [
                    Point2D(float(point[0]), float(point[1]))
                    for point in polygon
                    if isinstance(point, list) and len(point) == 2
                ]
                for polygon in control.get(
                    "overlay_polygons_image", []
                )
                if isinstance(polygon, list)
            ],
            calibration_size=tuple(control["calibration_size"])
            if control.get("calibration_size")
            else None,
            image_size=tuple(payload.get("image_size", (0, 0))),
            coordinate_mode=str(
                control.get("coordinate_mode", "intermediate_normalized")
            ),
            calibration_to_image=np.asarray(
                control.get("calibration_to_image", np.eye(3)),
                dtype=np.float64,
            ),
            image_match_strategy="intermediate_manifest",
            input_issues=list(control.get("input_issues", [])),
            source_metadata={
                "intermediate_candidate": candidate,
                "intermediate_package": str(package),
            },
        )
        camera.camera_model = str(payload.get("camera_model", ""))
        camera.K = np.asarray(payload.get("K"), dtype=np.float64)
        camera.dist_coeffs = np.asarray(payload.get("D"), dtype=np.float64)
        camera.R = np.asarray(payload.get("R"), dtype=np.float64)
        camera.t = np.asarray(payload.get("t"), dtype=np.float64).reshape(3)
        planar = projections.get(camera_id)
        if isinstance(planar, dict) and planar.get("H_image_to_plan"):
            camera.homography = np.asarray(
                planar["H_image_to_plan"], dtype=np.float64
            )
            camera.homography_inv = np.linalg.inv(camera.homography)
        runtime_cameras[camera_id] = camera
    # 业务运行时只从中间层重建对象模型，不重新解析 cali/scale 输入格式。
    dataset = StoreDataset(
        root=str(dataset_path),
        floor_plan_path=str(inputs.get("floorplan_path"))
        if inputs.get("floorplan_path")
        else None,
        floor_plan_size=tuple(plan.get("floorplan_size", (0, 0))),
        cameras=runtime_cameras,
        scale_path=None,
        input_diagnostics=[],
    )

    runtime = replace(cfg or StoreConfig())
    if plan_item:
        runtime.floor_plan_width_cm = float(
            plan.get("plan_width_cm", runtime.floor_plan_width_cm)
        )
        runtime.floor_plan_height_cm = float(
            plan.get("plan_height_cm", runtime.floor_plan_height_cm)
        )
    height_item = manifest.get("business", {}).get("height_policy")
    if height_item:
        height = _read_json(_resource_path(package, str(height_item)))
        runtime.table_height_cm = float(
            height.get("table_height_cm", runtime.table_height_cm)
        )
        runtime.shelf_height_cm = float(
            height.get("shelf_height_cm", runtime.shelf_height_cm)
        )
    capabilities = resolve_capabilities(
        package, candidate_name=candidate
    )
    return IntermediateRuntime(
        package_path=package,
        manifest=manifest,
        candidate_name=candidate,
        dataset=dataset,
        config=runtime,
        capabilities=capabilities,
        camera_rig=rig,
        planar_projection=projection,
    )


def model_project_points(
    object_points: np.ndarray,
    camera: dict[str, Any],
) -> np.ndarray:
    """按中间层相机模型把三维点投影到原始畸变图片。"""

    points = np.asarray(object_points, dtype=np.float64).reshape(-1, 1, 3)
    K = np.asarray(camera["K"], dtype=np.float64)
    D = np.asarray(camera["D"], dtype=np.float64).reshape(-1)
    R = np.asarray(camera["R"], dtype=np.float64)
    rvec, _ = cv2.Rodrigues(R)
    tvec = np.asarray(camera["t"], dtype=np.float64).reshape(3, 1)
    if camera.get("camera_model") == "opencv_fisheye_4":
        projected, _ = cv2.fisheye.projectPoints(
            points, rvec, tvec, K, D.reshape(4, 1)
        )
    else:
        projected, _ = cv2.projectPoints(
            points, rvec, tvec, K, D
        )
    return projected.reshape(-1, 2)


def model_undistort_image(
    image: np.ndarray,
    camera: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    """按普通针孔/鱼眼模型去畸变，并返回实际使用的新 K。"""

    K = np.asarray(camera["K"], dtype=np.float64)
    D = np.asarray(camera["D"], dtype=np.float64).reshape(-1)
    height, width = image.shape[:2]
    if camera.get("camera_model") == "opencv_fisheye_4":
        new_k = K.copy()
        map_x, map_y = cv2.fisheye.initUndistortRectifyMap(
            K,
            D.reshape(4, 1),
            np.eye(3, dtype=np.float64),
            new_k,
            (width, height),
            cv2.CV_32FC1,
        )
    else:
        new_k, _ = cv2.getOptimalNewCameraMatrix(
            K,
            D,
            (width, height),
            1.0,
            (width, height),
            centerPrincipalPoint=True,
        )
        map_x, map_y = cv2.initUndistortRectifyMap(
            K,
            D,
            None,
            new_k,
            (width, height),
            cv2.CV_32FC1,
        )
    return (
        cv2.remap(
            image,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        ),
        new_k,
    )


__all__ = [
    "CapabilityReport",
    "CapabilityState",
    "IntermediatePackageSummary",
    "IntermediateRuntime",
    "WorkspacePaths",
    "candidate_names",
    "create_unique_run_directory",
    "discover_intermediate_packages",
    "find_latest_intermediate_package",
    "import_external_calibration_package",
    "load_intermediate_runtime",
    "load_manifest",
    "load_sfm_evidence",
    "model_project_points",
    "model_undistort_image",
    "publish_intermediate_package",
    "publish_sfm_derived_package",
    "repository_root",
    "resolve_capabilities",
    "workspace_paths",
]
