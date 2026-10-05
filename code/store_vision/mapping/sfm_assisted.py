"""使用已验收 SfM 相机集合试算门店 2.5D 桌子地图。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from store_vision.calibration.homography import calibrate_homographies
from store_vision.calibration.scale_metadata import load_scale_metadata
from store_vision.config import StoreConfig
from store_vision.data import load_store_folder
from store_vision.data.models import MapObject25D, StoreDataset
from store_vision.detection import detect_tables_all
from store_vision.mapping.map25d import build_map25d
from store_vision.mapping.map25d_preview import render_map25d_preview


@dataclass(frozen=True)
class SfmAssistedMap25DResult:
    """一次 SfM 辅助 2.5D 试算的产物集合。"""

    dataset: StoreDataset
    map25d: dict[str, Any]
    objects: list[MapObject25D]
    output_dir: Path
    preview_path: Path
    summary: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    """读取必须为对象的 JSON 报告。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"报告不是 JSON 对象：{path}")
    return payload


def _report_paths(experiment_path: str | Path) -> tuple[Path, Path]:
    """定位 SfM 内参路由和视觉注册摘要。"""

    experiment = Path(experiment_path).expanduser().resolve()
    reports = experiment / "reports" if experiment.is_dir() else experiment.parent
    intrinsics = reports / "sfm_intrinsics.json"
    registration = reports / "sfm_registration_summary.json"
    if not intrinsics.is_file():
        raise FileNotFoundError(f"SfM 缺少内参路由报告：{intrinsics}")
    if not registration.is_file():
        raise FileNotFoundError(f"SfM 缺少视觉注册摘要：{registration}")
    return intrinsics, registration


def _runtime_config(
    cfg: StoreConfig,
    dataset: StoreDataset,
) -> tuple[StoreConfig, dict[str, Any]]:
    """优先使用 scale 文件推得的门店宽高，同时保留其误差告警。"""

    runtime = replace(cfg)
    if not dataset.scale_path:
        return runtime, {
            "status": "default_dimensions",
            "plan_width_cm": runtime.floor_plan_width_cm,
            "plan_height_cm": runtime.floor_plan_height_cm,
            "rmse_cm": None,
        }
    scale, metadata = load_scale_metadata(dataset.scale_path)
    runtime.floor_plan_width_cm = (
        abs(scale.x_metres_per_percent) * 100.0 * 100.0
    )
    runtime.floor_plan_height_cm = (
        abs(scale.y_metres_per_percent) * 100.0 * 100.0
    )
    return runtime, {
        "status": "scale_metadata_trial",
        "source": str(dataset.scale_path),
        "plan_width_cm": runtime.floor_plan_width_cm,
        "plan_height_cm": runtime.floor_plan_height_cm,
        "rmse_cm": scale.rmse_metres * 100.0,
        "max_error_cm": scale.max_error_metres * 100.0,
        "store_outline_percent": metadata.get(
            "mall_plan_coordinates_percent", []
        ),
    }


def _apply_active_intrinsics(
    dataset: StoreDataset,
    intrinsics: dict[str, Any],
    supported_images: set[str],
) -> dict[str, str]:
    """给已视觉注册相机写入 SfM 活动 K/D，不使用其任意尺度 R/T 定位。"""

    report_dataset = intrinsics.get("dataset_path")
    if report_dataset and Path(report_dataset).expanduser().resolve() != Path(
        dataset.root
    ).expanduser().resolve():
        raise ValueError("SfM 报告与当前 2.5D 数据集不是同一目录")
    canonical = intrinsics.get("canonical_resolution")
    matrix = intrinsics.get("shared_K")
    distortion = intrinsics.get("shared_D")
    if (
        not isinstance(canonical, list)
        or len(canonical) != 2
        or not isinstance(matrix, list)
        or np.asarray(matrix).shape != (3, 3)
        or not isinstance(distortion, list)
        or len(distortion) != 4
    ):
        raise ValueError("SfM 活动 K/D 或参考分辨率结构不完整")

    canonical_width, canonical_height = map(float, canonical)
    shared_k = np.asarray(matrix, dtype=np.float64)
    shared_d = np.asarray(distortion, dtype=np.float64)
    active_validation = intrinsics.get("active_intrinsics_validation", {})
    if active_validation and not active_validation.get("usable_for_sfm"):
        raise ValueError("SfM 活动 K/D 未通过可逆性门禁，不能用于 2.5D 试算")
    if not np.isfinite(shared_k).all() or not np.isfinite(shared_d).all():
        raise ValueError("SfM 活动 K/D 包含非有限数值")
    image_to_camera: dict[str, str] = {}
    for camera in dataset.camera_list():
        if not camera.image_path:
            continue
        image_name = Path(camera.image_path).name
        if image_name not in supported_images:
            continue
        if camera.image_size:
            width, height = camera.image_size
        else:
            image = cv2.imread(camera.image_path)
            if image is None:
                continue
            height, width = image.shape[:2]
        image_to_camera[image_name] = camera.device_serial
        native_k = shared_k.copy()
        native_k[0, 0] *= width / canonical_width
        native_k[0, 2] *= width / canonical_width
        native_k[1, 1] *= height / canonical_height
        native_k[1, 2] *= height / canonical_height
        camera.K = native_k
        camera.dist_coeffs = shared_d.copy()
        camera.camera_model = "OPENCV_FISHEYE"
    return image_to_camera


def _enrich_objects_with_sfm(
    objects: list[MapObject25D],
    per_image_observations: dict[str, Any],
    image_to_camera: dict[str, str],
) -> dict[str, int]:
    """给桌子记录来源相机的 SfM 支撑；不把整图点数伪装成桌子点数。"""

    camera_counts = {
        image_to_camera[image]: int(count)
        for image, count in per_image_observations.items()
        if image in image_to_camera
    }
    for item in objects:
        # 当前没有桌子级三维点，必须把检测结果称为候选而不是确认桌子。
        item.label = "table_candidate"
        item.meta["trial_candidate"] = True
        cameras = list(item.meta.get("observed_by", []))
        supported = [camera for camera in cameras if camera in camera_counts]
        item.meta["sfm_camera_support"] = {
            "status": (
                "all_observation_cameras_registered"
                if len(supported) == len(cameras)
                else "partial_observation_camera_registration"
            ),
            "supported_cameras": supported,
            "camera_level_point3d_observations": {
                camera: camera_counts[camera] for camera in supported
            },
            "interpretation": (
                "这些数量证明来源相机参与了共同 SfM；它们不是桌子表面专属三维点，"
                "不参与桌子 XY 或高度计算。"
            ),
        }
    return camera_counts


def run_sfm_assisted_map25d(
    source: str | Path | StoreDataset,
    experiment_path: str | Path,
    output_dir: str | Path,
    *,
    cfg: StoreConfig | None = None,
) -> SfmAssistedMap25DResult:
    """使用 SfM 9/9 结果筛选相机，再由人工平面对应试算桌子 2.5D。"""

    dataset = source if isinstance(source, StoreDataset) else load_store_folder(source)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    intrinsics_path, registration_path = _report_paths(experiment_path)
    intrinsics = _read_json(intrinsics_path)
    registration = _read_json(registration_path)
    supported_images = {
        str(name) for name in registration.get("visually_registered_images", [])
    }
    if not supported_images:
        raise ValueError("SfM 没有任何通过真实三维观测门禁的相机")

    runtime_cfg, scale_summary = _runtime_config(cfg or StoreConfig(), dataset)
    image_to_camera = _apply_active_intrinsics(
        dataset,
        intrinsics,
        supported_images,
    )
    supported_camera_ids = set(image_to_camera.values())
    if not supported_camera_ids:
        raise ValueError("SfM 视觉注册图片无法映射到当前数据集相机")

    calibrate_homographies(dataset, output / "calibration")
    objects = detect_tables_all(
        dataset,
        runtime_cfg,
        output / "detection",
        allowed_camera_ids=supported_camera_ids,
    )
    camera_counts = _enrich_objects_with_sfm(
        objects,
        registration.get("per_image_point3d_observations", {}),
        image_to_camera,
    )
    summary = {
        "status": "trial_ready" if objects else "trial_empty",
        "trust_level": (
            "observation_supported_not_independently_validated"
            if intrinsics.get("credible_calibration")
            else "provisional_observation_only"
        ),
        "position_source": (
            "K/D/R/t tabletop-plane projection anchored by cali floor homography"
        ),
        "height_source": (
            f"fixed {runtime_cfg.table_height_cm:.1f} cm business value used "
            "for XY projection and Z extrusion"
        ),
        "sfm_status": registration.get("status"),
        "sfm_selected_candidate": registration.get("selected_candidate"),
        "sfm_full_visual_registration": bool(
            registration.get("full_visual_registration")
        ),
        "sfm_registered_camera_count": len(supported_camera_ids),
        "sfm_camera_point3d_observations": camera_counts,
        "intrinsics_route": intrinsics.get("routing_source"),
        "credible_calibration": bool(intrinsics.get("credible_calibration")),
        "table_count": len(objects),
        "table_candidate_count": len(objects),
        "plan_width_cm": scale_summary["plan_width_cm"],
        "plan_height_cm": scale_summary["plan_height_cm"],
        "scale": scale_summary,
        "limitations": [
            "SfM 只用于确认来源相机具有共同三维观测，不用于桌子 XY 定位。",
            "桌子 XY 由 K/D/R/t 投到指定桌高平面，再由 cali H 锚定平面图。",
            "桌子高度是固定业务值，不是从图像或点云测量得到。",
            "当前 K/D 为 provisional 时，本结果只能观察布局趋势，不能用于测量。",
        ],
    }
    collection_metadata = {
        **summary,
        "store_outline_percent": scale_summary.get(
            "store_outline_percent", []
        ),
        "sfm_experiment": str(Path(experiment_path).expanduser().resolve()),
        "sfm_intrinsics_report": str(intrinsics_path),
        "sfm_registration_report": str(registration_path),
    }
    geojson = build_map25d(
        objects,
        output / "map25d_sfm_trial.geojson",
        metadata=collection_metadata,
    )
    if not dataset.floor_plan_path:
        raise ValueError("当前数据集缺少 2.5D 预览所需平面图")
    preview = render_map25d_preview(
        geojson,
        dataset.floor_plan_path,
        output / "map25d_sfm_trial_preview.png",
    )
    reports = output / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "map25d_sfm_trial_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return SfmAssistedMap25DResult(
        dataset=dataset,
        map25d=geojson,
        objects=objects,
        output_dir=output,
        preview_path=preview,
        summary=summary,
    )
