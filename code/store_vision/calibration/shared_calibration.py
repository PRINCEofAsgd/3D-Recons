"""真实数据的多单应矩阵共享内参实验编排、诊断与可视化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from store_vision.calibration.fisheye_bundle import (
    FisheyeBundleConfig,
    fit_shared_fisheye_bundle,
)
from store_vision.calibration.scale_metadata import (
    PlanScale,
    build_identity_audit,
    load_scale_metadata,
)
from store_vision.calibration.shared_intrinsics import (
    MODEL_NAMES,
    decompose_homography_pose,
    refine_intrinsics_constraints,
    solve_shared_intrinsics,
)
from store_vision.data.loader import (
    device_serial_from_filename,
    find_calibration_path,
    find_scale_path,
    load_store_inputs,
)
from store_vision.resolution import canonicalize_homographies


EXPERIMENT_VERSION = "V1.0.13_20261010"


@dataclass(frozen=True)
class SharedCalibrationConfig:
    """集中保存筛选、稳定性和物理验证阈值，避免散落硬编码。"""

    seed: int = 0
    min_image_quad_area_ratio: float = 0.015
    min_world_quad_area_ratio: float = 0.002
    max_homography_condition: float = 1.0e7
    min_loo_success_rate: float = 0.60
    max_focal_coefficient_of_variation: float = 0.35
    max_principal_point_std_fraction: float = 0.20
    max_pose_reprojection_rmse_px: float = 120.0
    min_downward_axis_component: float = 0.10
    max_height_relative_deviation: float = 0.50
    random_subset_count: int = 20
    ground_track_candidate_max_spread_metres: float = 0.25
    fisheye_initialization_max_nfev: int = 300
    fisheye_bundle_adjustment_max_nfev: int = 500
    fisheye_robust_loss_scale_px: float = 4.0
    fisheye_acceptable_reprojection_rmse_px: float = 30.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _json_value(value: Any) -> Any:
    """递归转换 NumPy 对象，并把非有限浮点显式写为 null。"""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Any) -> None:
    """统一写入 UTF-8、稳定缩进的实验 JSON。"""
    path.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_four_point_records(path: Path) -> list[dict[str, Any]]:
    """读取标定文件中的四点记录，不携带账户或业务侧元数据。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    items = payload.get("data", {}).get("list", []) if isinstance(payload, dict) else []
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("coordinates", {})
        try:
            coordinates = json.loads(raw) if isinstance(raw, str) else raw
            camera = [
                [float(point["x"]), float(point["y"])]
                for point in coordinates.get("cameraPoints", [])
            ]
            plane = [
                [float(point["x"]), float(point["y"])]
                for point in coordinates.get("mapPoints", [])
            ]
        except (AttributeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if len(camera) < 4 or len(plane) < 4:
            continue
        records.append(
            {
                "record_id": item.get("id"),
                "physical_camera_id": item.get("deviceSerialnum"),
                "serialnum": item.get("serialnum"),
                "business_name": item.get("name"),
                "calibration_width": item.get("deviceSnapWidth"),
                "calibration_height": item.get("deviceSnapHeight"),
                "camera_points": camera,
                "plane_points": plane,
            }
        )
    return records


def _image_files(dataset: Path) -> dict[str, Path]:
    """优先收集 screenshots 中带完整设备 ID 的相机图，并兼容旧布局。"""

    for root in (
        dataset / "screenshots",
        dataset / "cameras" / "images",
        dataset / "images",
        dataset,
    ):
        if not root.is_dir():
            continue
        result: dict[str, Path] = {}
        for path in sorted(root.rglob("*")):
            camera_id = device_serial_from_filename(path.name)
            if camera_id and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                result.setdefault(camera_id, path)
        if result:
            return result
    return {}


def _all_image_paths(dataset: Path) -> list[Path]:
    """收集全部候选图片，交给统一输入配对器联合选择快照和标定记录。"""
    for root in (
        dataset / "screenshots",
        dataset / "cameras" / "images",
        dataset / "images",
        dataset,
    ):
        if not root.is_dir():
            continue
        paths = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        if paths:
            return paths
    return []


def _record_signature(record: dict[str, Any]) -> str:
    """用对应点数值标识同一台相机的重复业务引用。"""
    camera = np.asarray(record["camera_points"][:4], dtype=np.float64).round(8)
    plane = np.asarray(record["plane_points"][:4], dtype=np.float64).round(8)
    return json.dumps([camera.tolist(), plane.tolist()], sort_keys=True)


def _representative_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按设备和点集去重，保留业务记录副本数量。"""
    records = _parse_four_point_records(path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        camera_id = str(record.get("physical_camera_id") or "")
        if camera_id:
            grouped.setdefault((camera_id, _record_signature(record)), []).append(record)
    representatives: list[dict[str, Any]] = []
    copy_counts: dict[str, int] = {}
    for (camera_id, _), copies in sorted(grouped.items()):
        item = dict(copies[0])
        item["business_record_ids"] = [copy.get("record_id") for copy in copies]
        item["business_record_count"] = len(copies)
        representatives.append(item)
        copy_counts[camera_id] = copy_counts.get(camera_id, 0) + len(copies)
    return representatives, copy_counts


def _polygon_area(points: np.ndarray) -> float:
    """返回四点凸包面积，点顺序错误时仍能诊断覆盖而不产生负面积。"""
    hull = cv2.convexHull(np.asarray(points, dtype=np.float32)).reshape(-1, 2)
    return float(abs(cv2.contourArea(hull)))


def _dlt_condition(source: np.ndarray, target: np.ndarray) -> tuple[int, list[float], float]:
    """计算四点 DLT 设计矩阵的秩与条件数。"""
    rows: list[list[float]] = []
    for (x, y), (u, v) in zip(source, target):
        rows.extend(
            [
                [-x, -y, -1.0, 0.0, 0.0, 0.0, u * x, u * y, u],
                [0.0, 0.0, 0.0, -x, -y, -1.0, v * x, v * y, v],
            ]
        )
    matrix = np.asarray(rows, dtype=np.float64)
    singular = np.linalg.svd(matrix, compute_uv=False)
    condition = float(singular[0] / singular[-1]) if singular[-1] > 0 else float("inf")
    return int(np.linalg.matrix_rank(matrix)), singular.tolist(), condition


def _build_homographies(
    records: list[dict[str, Any]],
    images: dict[str, Path],
    scale: PlanScale,
    config: SharedCalibrationConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构建统一 `world_plane_to_image` H 并执行可解释的基础几何筛选。"""
    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for record in records:
        camera_id = str(record["physical_camera_id"])
        image_path = images.get(camera_id)
        reasons: list[str] = []
        if image_path is None:
            excluded.append(
                {
                    "physical_camera_id": camera_id,
                    "reasons": ["没有与完整设备 ID 对应的相机图像"],
                }
            )
            continue
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            excluded.append(
                {"physical_camera_id": camera_id, "reasons": ["图像无法解码"]}
            )
            continue
        height, width = image.shape[:2]
        image_points = np.asarray(record["camera_points"][:4], dtype=np.float64)
        percent_points = np.asarray(record["plane_points"][:4], dtype=np.float64)
        world_points = scale.percent_to_world(percent_points)
        H, _ = cv2.findHomography(world_points, image_points, method=0)
        if H is None or not np.isfinite(H).all():
            excluded.append(
                {"physical_camera_id": camera_id, "reasons": ["Homography 求解失败"]}
            )
            continue
        H = H / H[2, 2]
        projected = cv2.perspectiveTransform(
            world_points.reshape(-1, 1, 2), H
        ).reshape(-1, 2)
        errors = np.linalg.norm(projected - image_points, axis=1)
        image_area_ratio = _polygon_area(image_points) / float(width * height)
        world_outline = scale.percent_to_world(np.array([[0, 0], [100, 0], [100, 100], [0, 100]]))
        world_area_ratio = _polygon_area(world_points) / max(_polygon_area(world_outline), 1e-12)
        dlt_rank, dlt_singular, dlt_condition = _dlt_condition(world_points, image_points)
        condition = float(np.linalg.cond(H))
        if image_area_ratio < config.min_image_quad_area_ratio:
            reasons.append("图像四点覆盖面积过小")
        if world_area_ratio < config.min_world_quad_area_ratio:
            reasons.append("平面四点覆盖面积过小")
        if dlt_rank < 8:
            reasons.append("DLT 设计矩阵秩不足")
        if condition > config.max_homography_condition:
            reasons.append("H 条件数超过阈值")
        row = {
            "physical_camera_id": camera_id,
            "image_name": image_path.name,
            "image_resolution": [width, height],
            "source_image_points_px": image_points,
            "source_plan_points_percent": percent_points,
            "source_world_points_metres": world_points,
            "H_world_plane_to_image": H,
            "H_normalization": "H[2,2]=1",
            "inverse_direction": "image_to_world_plane = inverse(H)",
            "four_point_reprojection_rmse_px": float(np.sqrt(np.mean(errors**2))),
            "four_point_reprojection_max_px": float(np.max(errors)),
            "homography_condition_number": condition,
            "dlt_rank": dlt_rank,
            "dlt_singular_values": dlt_singular,
            "dlt_condition_number": dlt_condition,
            "image_quad_area_ratio": image_area_ratio,
            "world_quad_area_ratio": world_area_ratio,
            "business_record_count": record.get("business_record_count", 1),
            "calibration_resolution": record.get("calibration_resolution"),
            "coordinate_mode": record.get("coordinate_mode", "pixel_unknown"),
            "calibration_to_image": record.get("calibration_to_image"),
            "filter_reasons": reasons,
            "camera_model_handling": (
                "按产品路线将全部镜头视为同一型号鱼眼镜头；"
                "在统一参考像素坐标系共享 K/D，并联合拟合逐机位姿。"
            ),
        }
        if reasons:
            excluded.append(
                {
                    "physical_camera_id": camera_id,
                    "reasons": reasons,
                    "diagnostics": row,
                }
            )
        else:
            valid.append(row)
    return valid, excluded


def _parameter_vector(K: np.ndarray) -> np.ndarray:
    return np.array([K[0, 0], K[1, 1], K[0, 2], K[1, 2]], dtype=np.float64)


def _distribution(vectors: list[np.ndarray], image_size: tuple[int, int]) -> dict[str, Any]:
    """汇总焦距/主点在留一法或子集求解中的波动。"""
    if not vectors:
        return {
            "successful_solution_count": 0,
            "mean": None,
            "std": None,
            "coefficient_of_variation_fx_fy": None,
            "principal_point_std_fraction": None,
        }
    values = np.vstack(vectors)
    mean, std = values.mean(axis=0), values.std(axis=0)
    focal_cv = float(max(std[0] / max(abs(mean[0]), 1e-12), std[1] / max(abs(mean[1]), 1e-12)))
    width, height = image_size
    principal_fraction = float(max(std[2] / max(width, 1), std[3] / max(height, 1)))
    return {
        "successful_solution_count": len(vectors),
        "parameter_order": ["fx", "fy", "cx", "cy"],
        "mean": mean,
        "std": std,
        "min": values.min(axis=0),
        "max": values.max(axis=0),
        "coefficient_of_variation_fx_fy": focal_cv,
        "principal_point_std_fraction": principal_fraction,
    }


def _stability_analysis(
    homographies: list[dict[str, Any]],
    image_size: tuple[int, int],
    model: str,
    config: SharedCalibrationConfig,
) -> dict[str, Any]:
    """执行逐相机留一法和固定随机种子的相机子集求解。"""
    loo_rows: list[dict[str, Any]] = []
    loo_vectors: list[np.ndarray] = []
    for omitted in range(len(homographies)):
        subset = [
            row["H_world_plane_to_image"]
            for index, row in enumerate(homographies)
            if index != omitted
        ]
        result = solve_shared_intrinsics(subset, image_size, model)
        refined = refine_intrinsics_constraints(
            subset, image_size, model, result.K
        )
        loo_rows.append(
            {
                "omitted_camera_id": homographies[omitted]["physical_camera_id"],
                "success": result.success,
                "plausible": result.plausible,
                "linear_K": result.K,
                "refined_success": refined["success"],
                "K": refined["K"],
                "failure_reason": result.failure_reason,
            }
        )
        if refined["success"] and refined["K"] is not None:
            loo_vectors.append(_parameter_vector(refined["K"]))

    rng = np.random.default_rng(config.seed)
    subset_size = max(3, len(homographies) - 2)
    candidates = list(combinations(range(len(homographies)), subset_size))
    if len(candidates) > config.random_subset_count:
        selected = rng.choice(len(candidates), config.random_subset_count, replace=False)
        candidates = [candidates[int(index)] for index in sorted(selected)]
    subset_rows: list[dict[str, Any]] = []
    subset_vectors: list[np.ndarray] = []
    for indices in candidates:
        result = solve_shared_intrinsics(
            [homographies[index]["H_world_plane_to_image"] for index in indices],
            image_size,
            model,
        )
        refined = refine_intrinsics_constraints(
            [homographies[index]["H_world_plane_to_image"] for index in indices],
            image_size,
            model,
            result.K,
        )
        subset_rows.append(
            {
                "camera_ids": [homographies[index]["physical_camera_id"] for index in indices],
                "success": result.success,
                "plausible": result.plausible,
                "linear_K": result.K,
                "refined_success": refined["success"],
                "K": refined["K"],
                "failure_reason": result.failure_reason,
            }
        )
        if refined["success"] and refined["K"] is not None:
            subset_vectors.append(_parameter_vector(refined["K"]))

    loo_distribution = _distribution(loo_vectors, image_size)
    subset_distribution = _distribution(subset_vectors, image_size)
    loo_rate = len(loo_vectors) / max(len(loo_rows), 1)
    stable = bool(
        loo_rate >= config.min_loo_success_rate
        and (loo_distribution["coefficient_of_variation_fx_fy"] or float("inf"))
        <= config.max_focal_coefficient_of_variation
        and (loo_distribution["principal_point_std_fraction"] or 0.0)
        <= config.max_principal_point_std_fraction
    )
    return {
        "leave_one_camera_out": loo_rows,
        "leave_one_camera_out_success_rate": loo_rate,
        "leave_one_camera_out_distribution": loo_distribution,
        "fixed_seed_subsets": subset_rows,
        "fixed_seed_subset_distribution": subset_distribution,
        "stable_by_configured_thresholds": stable,
    }


def _select_model(model_reports: dict[str, dict[str, Any]]) -> tuple[str | None, str]:
    """结合子集稳定性、无量纲约束残差和模型简约性选择 K。"""
    stable = [
        model
        for model in ("A", "B", "C")
        if model_reports[model]["linear"]["success"]
        and model_reports[model]["linear"]["plausible"]
        and model_reports[model]["refined"]["success"]
        and model_reports[model]["stability"]["stable_by_configured_thresholds"]
    ]
    if "C" in stable:
        return "C", "模型 C 通过线性可解性、无量纲细化和子集稳定性检查。"
    if "A" in stable and "B" in stable:
        cost_a = float(model_reports["A"]["refined"]["cost_half_sum_squared"])
        cost_b = float(model_reports["B"]["refined"]["cost_half_sum_squared"])
        K_b = np.asarray(model_reports["B"]["refined"]["K"], dtype=np.float64)
        aspect_difference = abs(K_b[0, 0] / K_b[1, 1] - 1.0)
        improvement = (cost_a - cost_b) / max(cost_a, 1e-12)
        if improvement >= 0.10 or aspect_difference >= 0.05:
            return "B", (
                "模型 B 稳定，且相对模型 A 的约束残差改善或 fx/fy 差异足以支持增加一个自由度。"
            )
        return "A", (
            "模型 A 与 B 均通过子集阈值；模型 B 的残差改善不足 10% 且 fx/fy 接近，"
            "按简约原则当前数据不足以证明需要分别估计两个焦距。"
        )
    if stable:
        selected = stable[0]
        return selected, f"模型 {selected} 是唯一通过线性、细化和子集稳定性检查的模型。"
    plausible = [
        model
        for model in ("A", "B", "C")
        if model_reports[model]["linear"]["success"]
        and model_reports[model]["linear"]["plausible"]
        and model_reports[model]["refined"]["success"]
    ]
    if plausible:
        selected = plausible[0]
        return selected, (
            f"没有模型通过全部子集稳定阈值；仅将约束最强的可计算模型 {selected} "
            "作为诊断初值，不标记为可信物理内参。"
        )
    return None, "A/B/C 均未得到有限且处于集中合理范围内的 K。"


def _solve_intrinsics_group(
    homographies: list[dict[str, Any]],
    image_size: tuple[int, int],
    config: SharedCalibrationConfig,
) -> tuple[dict[str, dict[str, Any]], str | None, str]:
    """在一个规范像素坐标系中比较 A/B/C 共享内参模型。"""
    model_reports: dict[str, dict[str, Any]] = {}
    matrices = [
        row["H_world_plane_to_image_canonical"] for row in homographies
    ]
    for model in ("A", "B", "C"):
        linear = solve_shared_intrinsics(matrices, image_size, model)
        refined = refine_intrinsics_constraints(
            matrices, image_size, model, linear.K
        )
        model_reports[model] = {
            "definition": MODEL_NAMES[model],
            "linear": linear.as_dict(),
            "refined": refined,
            "stability": _stability_analysis(
                [
                    {
                        **row,
                        "H_world_plane_to_image": row[
                            "H_world_plane_to_image_canonical"
                        ],
                    }
                    for row in homographies
                ],
                image_size,
                model,
                config,
            ),
        }
    selected, reason = _select_model(model_reports)
    return model_reports, selected, reason


def _pose_rows(
    homographies: list[dict[str, Any]],
    K: np.ndarray,
    config: SharedCalibrationConfig,
) -> list[dict[str, Any]]:
    """分解逐机位姿，并在得到全局高度中位数后执行软一致性检查。"""
    rows: list[dict[str, Any]] = []
    for homography in homographies:
        # 共享 K 在组内规范分辨率求解；位姿也必须使用同一个像素坐标系。
        pose_homography = homography.get(
            "H_world_plane_to_image_canonical",
            homography["H_world_plane_to_image"],
        )
        pose_image_points = homography.get(
            "source_image_points_canonical_px",
            homography["source_image_points_px"],
        )
        pose = decompose_homography_pose(
            pose_homography,
            K,
            homography["source_world_points_metres"],
            pose_image_points,
        )
        euler = Rotation.from_matrix(pose["R"]).as_euler("xyz", degrees=True)
        rows.append(
            {
                "physical_camera_id": homography["physical_camera_id"],
                "image_name": homography["image_name"],
                "intrinsics_group": homography.get("intrinsics_group"),
                "native_image_resolution": homography["image_resolution"],
                "intrinsics_resolution": homography.get(
                    "canonical_resolution", homography["image_resolution"]
                ),
                "convention": "world_to_camera: x_camera=R*x_world+T",
                "R": pose["R"],
                "euler_xyz_degrees_world_to_camera": euler,
                "rotation_vector": pose["rotation_vector"],
                "T_metres": pose["T"],
                "camera_center_world_metres": pose["camera_center"],
                "height_metres": float(pose["camera_center"][2]),
                "optical_axis_world": pose["optical_axis_world"],
                "camera_above_ground": pose["camera_above_ground"],
                "all_control_points_in_front": pose["all_control_points_in_front"],
                "looks_toward_ground": pose["looks_toward_ground"],
                "pose_reprojection_rmse_px": pose["rmse_image_px"],
                "pose_reprojection_max_px": pose["max_error_image_px"],
                "homography_decomposition_scale": pose["homography_scale"],
            }
        )

    positive_heights = [row["height_metres"] for row in rows if row["height_metres"] > 0]
    median_height = float(np.median(positive_heights)) if positive_heights else float("nan")
    for row in rows:
        height_deviation = (
            abs(row["height_metres"] - median_height) / median_height
            if np.isfinite(median_height) and median_height > 0
            else float("inf")
        )
        downward = float(-row["optical_axis_world"][2])
        reasons: list[str] = []
        if not row["camera_above_ground"]:
            reasons.append("相机中心不在地面上方")
        if not row["looks_toward_ground"] or downward < config.min_downward_axis_component:
            reasons.append("光轴没有稳定朝向地面")
        if not row["all_control_points_in_front"]:
            reasons.append("存在控制点位于相机后方")
        if row["pose_reprojection_rmse_px"] > config.max_pose_reprojection_rmse_px:
            reasons.append("共享 K 分解后的重投影误差过大")
        if height_deviation > config.max_height_relative_deviation:
            reasons.append("高度偏离正高度中位数超过软阈值")
        row["height_relative_deviation_from_median"] = height_deviation
        row["downward_optical_axis_component"] = downward
        row["physical_plausibility_passed"] = not reasons
        row["physical_plausibility_reasons"] = reasons
        row["classification"] = "normal_pinhole_candidate" if not reasons else "pinhole_fit_suspect"
    return rows


def _enrich_pose_plan_validation(
    poses: list[dict[str, Any]],
    homographies: list[dict[str, Any]],
    scale: PlanScale,
    mall_outline: list[dict[str, Any]],
) -> None:
    """补充相机中心、原标定区域和 `mallPlanCoordinates` 的平面关系。"""
    by_camera = {row["physical_camera_id"]: row for row in homographies}
    outline = np.asarray(
        [
            [float(point["x"]), float(point["y"])]
            for point in mall_outline
            if isinstance(point, dict) and "x" in point and "y" in point
        ],
        dtype=np.float32,
    )
    for pose in poses:
        source = by_camera[pose["physical_camera_id"]]
        center_world = np.asarray(pose["camera_center_world_metres"][:2]).reshape(1, 2)
        center_percent = scale.world_to_percent(center_world)[0]
        region_center = np.asarray(source["source_plan_points_percent"], dtype=np.float64).mean(axis=0)
        inside = (
            bool(cv2.pointPolygonTest(outline.reshape(-1, 1, 2), tuple(center_percent), False) >= 0)
            if len(outline) >= 3
            else None
        )
        pose["camera_center_plan_percent"] = center_percent
        pose["calibrated_region_centroid_percent"] = region_center
        pose["distance_to_calibrated_region_centroid_percent"] = float(
            np.linalg.norm(center_percent - region_center)
        )
        pose["within_mall_plan_boundary"] = inside
        pose["mall_boundary_interpretation"] = (
            "相机可安装在边界或边界外侧，是否位于折线内部只作诊断，不作为硬筛选。"
        )


def _feature_track_plane_validation(
    repository: Path,
    homographies: list[dict[str, Any]],
    config: SharedCalibrationConfig,
) -> dict[str, Any]:
    """把已有 verified feature tracks 映射到地面，统计平面一致候选。

    轨迹没有地面语义标签，因此小平面离散度只能表明“与当前 H 相容”，不能
    独立证明轨迹来自地面，也不能单独证明 K/R/T 正确。
    """
    from store_vision.calibration.pair_geometry import ColmapPairDatabase

    track_candidates = [repository / "reports/feature_tracks.json"]
    database_candidates = [repository / "reports/database.db"]
    track_path = next((path for path in track_candidates if path.is_file()), None)
    database_path = next((path for path in database_candidates if path.is_file()), None)
    if track_path is None or database_path is None:
        return {
            "available": False,
            "track_file": str(track_path) if track_path else None,
            "database_file": str(database_path) if database_path else None,
            "reason": "没有同时发现 feature_tracks.json 和对应 COLMAP database.db",
        }
    try:
        payload = json.loads(track_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"available": False, "reason": f"轨迹文件无法读取: {exc}"}
    tracks = payload.get("cleaned_tracks", [])
    inverse_by_image = {
        row["image_name"]: np.linalg.inv(np.asarray(row["H_world_plane_to_image"], dtype=np.float64))
        for row in homographies
    }
    spreads: list[float] = []
    lengths: list[int] = []
    rows: list[dict[str, Any]] = []
    try:
        with ColmapPairDatabase(database_path) as database:
            keypoints = {
                image_name: database.keypoints(image_name)[:, :2]
                for image_name in inverse_by_image
                if image_name in database.images
            }
            for track in tracks:
                world_observations: list[np.ndarray] = []
                used_images: list[str] = []
                for observation in track.get("observations", []):
                    image_name = observation.get("image_name")
                    index = observation.get("keypoint_index")
                    points = keypoints.get(image_name)
                    if points is None or not isinstance(index, int) or index < 0 or index >= len(points):
                        continue
                    mapped = cv2.perspectiveTransform(
                        np.asarray(points[index], dtype=np.float64).reshape(1, 1, 2),
                        inverse_by_image[image_name],
                    ).reshape(2)
                    if np.isfinite(mapped).all():
                        world_observations.append(mapped)
                        used_images.append(image_name)
                if len(world_observations) < 2:
                    continue
                values = np.vstack(world_observations)
                centroid = values.mean(axis=0)
                distances = np.linalg.norm(values - centroid, axis=1)
                spread = float(np.sqrt(np.mean(distances**2)))
                spreads.append(spread)
                lengths.append(len(values))
                if spread <= config.ground_track_candidate_max_spread_metres:
                    rows.append(
                        {
                            "track_id": track.get("track_id"),
                            "observation_count": len(values),
                            "images": used_images,
                            "ground_plane_spread_rms_metres": spread,
                            "mapped_world_centroid_metres": centroid,
                        }
                    )
    except (KeyError, ValueError, OSError) as exc:
        return {"available": False, "reason": f"轨迹与数据库关联失败: {exc}"}

    values = np.asarray(spreads, dtype=np.float64)
    thresholds = (0.25, 0.5, 1.0)
    return {
        "available": True,
        "track_file": str(track_path),
        "database_file": str(database_path),
        "evaluated_track_count": len(spreads),
        "evaluated_length_ge_3_count": int(sum(length >= 3 for length in lengths)),
        "spread_rms_metres_percentiles": (
            {
                "p25": float(np.percentile(values, 25)),
                "p50": float(np.percentile(values, 50)),
                "p75": float(np.percentile(values, 75)),
                "p90": float(np.percentile(values, 90)),
            }
            if len(values)
            else None
        ),
        "candidate_counts_by_spread_threshold_metres": {
            str(threshold): int(np.sum(values <= threshold)) for threshold in thresholds
        },
        "configured_candidate_threshold_metres": config.ground_track_candidate_max_spread_metres,
        "candidate_tracks": rows,
        "interpretation": (
            "轨迹来自 verified matches，但没有地面 ROI/语义标签。小离散度轨迹只是地面相容候选；"
            "墙面、桌面、重复纹理和 OSD 仍可能混入，不能当作独立地面真值。"
        ),
    }


def _legacy_position_comparison(
    repository: Path,
    poses: list[dict[str, Any]],
    scale: PlanScale,
) -> dict[str, Any]:
    """读取旧业务近似 bundle，仅按平面百分比比较，不冒充同一物理坐标系。"""
    candidates = [repository / "reports/legacy_bundle.json"]
    source = next((path for path in candidates if path.is_file()), None)
    new_by_id = {row["physical_camera_id"]: row for row in poses}
    rows: list[dict[str, Any]] = []
    if source is not None:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        for camera_id, item in payload.get("cameras", {}).items():
            if camera_id not in new_by_id:
                continue
            try:
                R = np.asarray(item["R"], dtype=np.float64).reshape(3, 3)
                t = np.asarray(item["t"], dtype=np.float64).reshape(3)
                center_cm = -R.T @ t
            except (KeyError, TypeError, ValueError):
                continue
            # 旧链固定使用 2000×1200 cm；只把 XY 归一化回百分比进行展示。
            legacy_percent = np.array([center_cm[0] / 20.0, center_cm[1] / 12.0])
            new_center = new_by_id[camera_id]["camera_center_world_metres"]
            new_percent = scale.world_to_percent(np.asarray(new_center[:2]).reshape(1, 2))[0]
            rows.append(
                {
                    "physical_camera_id": camera_id,
                    "legacy_center_raw_cm": center_cm,
                    "legacy_center_percent_under_old_20m_x_12m_assumption": legacy_percent,
                    "new_center_world_metres": new_center,
                    "new_center_percent": new_percent,
                    "plan_percent_displacement": float(np.linalg.norm(new_percent - legacy_percent)),
                }
            )
    return {
        "source": str(source) if source else None,
        "comparison_available": bool(rows),
        "warning": (
            "旧 bundle 使用默认 K、四点拟合畸变和固定 20m×12m 平面，坐标尺度与本轮 scale.txt "
            "米制世界系不同；这里只比较归一化平面位置，不能当作同尺度真值误差。"
        ),
        "rows": rows,
    }


def _save_visualizations(
    dataset: Path,
    output: Path,
    scale: PlanScale,
    homographies: list[dict[str, Any]],
    poses: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> list[str]:
    """输出平面映射、相机位置/朝向和高度分布三张诊断图。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    artifacts: list[str] = []
    floor_path = next(
        (
            path
            for path in (
                dataset / "footfallplan.png",
                dataset / "floorplan.png",
                dataset / "floorplan.jpg",
                dataset / "floorplan.jpeg",
            )
            if path.is_file()
        ),
        None,
    )
    floor = cv2.cvtColor(cv2.imread(str(floor_path)), cv2.COLOR_BGR2RGB) if floor_path else None

    fig, ax = plt.subplots(figsize=(12, 9))
    if floor is not None:
        ax.imshow(floor, extent=[0, 100, 100, 0])
    for row in homographies:
        points = row["source_plan_points_percent"]
        closed = np.vstack([points, points[0]])
        ax.plot(closed[:, 0], closed[:, 1], marker="o", linewidth=1.6)
        center = points.mean(axis=0)
        ax.text(center[0], center[1], row["physical_camera_id"][:4], fontsize=8)
    ax.set(xlabel="plan x (%)", ylabel="plan y (%)", title="Manual four-point ground regions")
    ax.set_xlim(0, 100)
    ax.set_ylim(100, 0)
    ax.grid(alpha=0.25)
    path = output / "homography_mapping.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    artifacts.append(path.name)

    fig, ax = plt.subplots(figsize=(12, 9))
    if floor is not None:
        ax.imshow(floor, extent=[0, 100, 100, 0])
    legacy = {
        row["physical_camera_id"]: row
        for row in comparison.get("rows", [])
    }
    for row in poses:
        center_world = np.asarray(row["camera_center_world_metres"][:2]).reshape(1, 2)
        center_percent = scale.world_to_percent(center_world)[0]
        direction = np.asarray(row["optical_axis_world"], dtype=np.float64)
        center3 = np.asarray(row["camera_center_world_metres"], dtype=np.float64)
        if direction[2] < -1e-9:
            ground = center3 + (-center3[2] / direction[2]) * direction
            target_percent = scale.world_to_percent(ground[:2].reshape(1, 2))[0]
        else:
            target_percent = center_percent
        color = "tab:green" if row["physical_plausibility_passed"] else "tab:red"
        ax.scatter(*center_percent, c=color, s=55, marker="^")
        ax.annotate(
            "",
            xy=target_percent,
            xytext=center_percent,
            arrowprops={"arrowstyle": "->", "color": color, "lw": 1.5},
        )
        ax.text(center_percent[0], center_percent[1], row["physical_camera_id"][:4], fontsize=8)
        old = legacy.get(row["physical_camera_id"])
        if old:
            old_percent = np.asarray(
                old["legacy_center_percent_under_old_20m_x_12m_assumption"]
            )
            ax.scatter(*old_percent, c="tab:blue", s=25, marker="x")
            ax.plot(
                [old_percent[0], center_percent[0]],
                [old_percent[1], center_percent[1]],
                color="tab:blue",
                alpha=0.35,
                linestyle="--",
            )
    ax.set(
        xlabel="plan x (%)",
        ylabel="plan y (%)",
        title="Camera centres and optical-axis ground intersections (blue x: legacy approximation)",
    )
    ax.set_xlim(-10, 110)
    ax.set_ylim(110, -10)
    ax.grid(alpha=0.25)
    path = output / "camera_pose_map.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    artifacts.append(path.name)

    fig, ax = plt.subplots(figsize=(11, 5))
    labels = [row["physical_camera_id"][:4] for row in poses]
    heights = [row["height_metres"] for row in poses]
    colors = ["tab:green" if row["physical_plausibility_passed"] else "tab:red" for row in poses]
    ax.bar(labels, heights, color=colors)
    if any(value > 0 for value in heights):
        ax.axhline(np.median([value for value in heights if value > 0]), color="black", linestyle="--", label="positive median")
        ax.legend()
    ax.set(xlabel="camera id prefix", ylabel="height (m)", title="Recovered camera height distribution")
    ax.grid(axis="y", alpha=0.25)
    path = output / "height_distribution.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    artifacts.append(path.name)
    return artifacts


def _markdown_report(report: dict[str, Any]) -> str:
    """生成人可读的实验结论，数值仍以 JSON 为完整来源。"""
    selected = report["selection"]["selected_model"]
    selected_report = report["intrinsics_models"].get(selected) if selected else None
    K = selected_report.get("K") if selected_report else None
    D = selected_report.get("D") if selected_report else None
    poses = report["poses"]
    passed = sum(row["physical_plausibility_passed"] for row in poses)
    lines = [
        "# 同型号鱼眼相机共享标定与联合 BA",
        "",
        f"- 版本：`{report['version']}`",
        f"- 状态：`{report['status']}`",
        f"- 输入：{report['input']['dataset_path']}",
        f"- 参与共享 K/D 与位姿联合拟合：{report['summary']['included_camera_count']} 台；基础筛除：{report['summary']['excluded_camera_count']} 台。",
        f"- 选定模型：`{selected or 'none'}`；{report['selection']['reason']}",
        f"- 通过逐机物理检查：{passed}/{len(poses)}。",
        "",
        "## scale.txt 结论",
        "",
        "- `calibrationPoints` 提供 `rx/ry` 平面图相对坐标到 `rw/rh` 米制地面坐标的控制点。",
        f"- 拟合尺度：X={report['scale']['x_metres_per_percent']:.8f} m/%，Y={report['scale']['y_metres_per_percent']:.8f} m/%；RMSE={report['scale']['rmse_metres']:.6f} m。",
        "- `mallPlanCoordinates` 是门店边界折线，`mallPlan` 是服务端资源引用；`area=6.0` 的业务语义未确认，未用于尺度。",
        "- 世界系：X 向平面图右侧、Y 向平面图上方、Z 向上；H 为 `world_plane_to_image`；R/T 为 `world_to_camera`。",
        "",
        "## 设备对应",
        "",
        f"- 三方共同设备：{', '.join(report['identity_audit']['intersection_all_three']) or '无'}。",
        f"- cali+图像但不在 scale 设备表：{', '.join(report['identity_audit']['cali_and_image_not_scale']) or '无'}。",
        f"- scale 设备表但缺 cali/图像：{', '.join(report['identity_audit']['scale_not_cali_or_image']) or '无'}。",
        "",
        "## 鱼眼共享模型",
        "",
        "| 模型 | 拟合初始化 RMSE px | BA RMSE px | BA 成功 | 复用拟合观测 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    fisheye = report["intrinsics_models"]["FISHEYE"]
    fitted = fisheye["initialization"]["fit"]
    bundle = fisheye["bundle_adjustment"]
    lines.append(
        f"| OPENCV_FISHEYE | {fitted['reprojection_rmse_px']:.4f} | "
        f"{bundle['reprojection_rmse_px']:.4f} | {bundle['success']} | "
        f"{bundle['uses_fitted_calibration_observations']} |"
    )
    lines.extend(
        [
            "",
            "## 选定共享 K/D",
            "",
            "```json",
            json.dumps(_json_value({"K": K, "D": D}), ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    lines.extend(
        [
            "## 外参与物理检查",
            "",
            "| 相机 | 高度 m | 光轴向下分量 | 位姿重投影 RMSE px | 分类 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in poses:
        lines.append(
            f"| {row['physical_camera_id']} | {row['height_metres']:.4f} | "
            f"{row['downward_optical_axis_component']:.4f} | "
            f"{row['pose_reprojection_rmse_px']:.3f} | {row['classification']} |"
        )
    lines.extend(
        [
            "",
            "## 畸变与可信度",
            "",
            "- 正式求解使用 OpenCV fisheye 模型并共享 `K` 与四维 `D=[k1,k2,k3,k4]`，不再固定 `D=0`。",
            "- 所有镜头按同一型号处理；逐机只保留独立 R/T。",
            "- BA 从拟合初始化基线启动，并按当前产品路线允许复用形成拟合的人工控制观测。",
            "",
            "## 已有特征轨迹交叉检查",
            "",
            (
                f"- 已映射 {report['feature_track_plane_validation'].get('evaluated_track_count', 0)} 条 verified 轨迹；"
                f"平面离散度统计见 `feature_track_plane_validation.json`。"
                if report["feature_track_plane_validation"].get("available")
                else f"- 未完成：{report['feature_track_plane_validation'].get('reason')}"
            ),
            "- 这些轨迹没有地面语义标签，小离散度只作为候选，不被计为独立地面真值。",
            "",
            "## 结论与下一步",
            "",
            report["conclusion"],
            "",
            "- BA 已在本轮自动执行；无需先运行 B0。",
            "- 独立留出观测仍可用于评价泛化误差，但不再作为 BA 的启用条件。",
            "",
        ]
    )
    return "\n".join(lines)


def run_shared_intrinsics_calibration(
    dataset_path: str | Path,
    output_path: str | Path,
    *,
    project_root: str | Path | None = None,
    config: SharedCalibrationConfig | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行一次真实数据共享 K 实验并生成完整诊断产物。"""
    config = config or SharedCalibrationConfig()
    dataset = Path(dataset_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    repository = (
        Path(project_root).expanduser().resolve()
        if project_root
        else next(
            (
                parent
                for parent in Path(__file__).resolve().parents
                if (parent / "code" / "pyproject.toml").is_file()
            ),
            Path(__file__).resolve().parents[3],
        )
    )
    calibration_path = find_calibration_path(
        dataset, include_calibration_subdir=False
    )
    scale_path = find_scale_path(dataset, include_calibration_subdir=False)
    missing = [
        label
        for label, path in (
            ("cali.txt/cali.json", calibration_path),
            ("scale.txt/scale.json", scale_path),
        )
        if path is None
    ]
    if missing:
        raise FileNotFoundError(f"共享内参实验缺少输入: {missing}")
    if dry_run:
        return {
            "version": EXPERIMENT_VERSION,
            "status": "dry_run",
            "writes_performed": False,
            "dataset_path": str(dataset),
            "output_path": str(output),
            "planned_outputs": [
                "scale_analysis.json",
                "camera_correspondence.json",
                "homography_diagnostics.json",
                "intrinsics_models.json",
                "intrinsics_groups.json",
                "intrinsics_validity.json",
                "fitted_initialization.json",
                "bundle_adjustment.json",
                "poses.json",
                "legacy_position_comparison.json",
                "feature_track_plane_validation.json",
                "fisheye_calibration_report.json",
                "fisheye_calibration_report.md",
                "shared_calibration_report.json",
                "shared_calibration_report.md",
                "homography_mapping.png",
                "camera_pose_map.png",
                "height_distribution.png",
            ],
        }

    np.random.seed(config.seed)
    output.mkdir(parents=True, exist_ok=True)
    assert calibration_path is not None and scale_path is not None
    scale, scale_metadata = load_scale_metadata(scale_path)
    _, copy_counts = _representative_records(calibration_path)
    loaded = load_store_inputs(
        calibration_path, None, _all_image_paths(dataset)
    )
    records: list[dict[str, Any]] = []
    images: dict[str, Path] = {}
    input_excluded: list[dict[str, Any]] = []
    for camera in loaded.camera_list():
        errors = [
            issue
            for issue in camera.input_issues
            if issue.get("severity") == "error"
        ]
        if camera.image_path is None or errors:
            input_excluded.append(
                {
                    "physical_camera_id": camera.device_serial,
                    "reasons": [
                        issue.get("message", issue.get("code", "输入错误"))
                        for issue in errors
                    ]
                    or ["没有可用截图"],
                }
            )
            continue
        images[camera.device_serial] = Path(camera.image_path)
        records.append(
            {
                "physical_camera_id": camera.device_serial,
                "camera_points": camera.camera_points_array(),
                "plane_points": camera.map_points_pct_array(),
                "business_record_count": copy_counts.get(camera.device_serial, 1),
                "calibration_resolution": list(camera.calibration_size)
                if camera.calibration_size
                else None,
                "coordinate_mode": camera.coordinate_mode,
                "calibration_to_image": camera.calibration_to_image,
            }
        )
    calibration_ids = sorted(loaded.cameras)
    identity = build_identity_audit(
        scale_metadata["device_ids"], calibration_ids, sorted(images)
    )
    homographies, excluded = _build_homographies(records, images, scale, config)
    excluded = input_excluded + excluded
    if len(homographies) < 2:
        raise RuntimeError("基础几何筛选后不足两个有效 Homography")
    resolutions = {tuple(row["image_resolution"]) for row in homographies}
    # 所有镜头按同一型号处理：先把逐图像素变换到同一个参考分辨率，
    # 再全局共享鱼眼 K/D。未知裁剪仍会在 loader 输入预检阶段被排除。
    image_size, homographies = canonicalize_homographies(homographies)
    for row in homographies:
        row["intrinsics_group"] = "shared_fisheye_all_cameras"
    fisheye = fit_shared_fisheye_bundle(
        homographies,
        image_size,
        config=FisheyeBundleConfig(
            initialization_max_nfev=config.fisheye_initialization_max_nfev,
            bundle_adjustment_max_nfev=config.fisheye_bundle_adjustment_max_nfev,
            robust_loss_scale_px=config.fisheye_robust_loss_scale_px,
            acceptable_reprojection_rmse_px=(
                config.fisheye_acceptable_reprojection_rmse_px
            ),
        ),
    )
    selected = "FISHEYE"
    selected_k = np.asarray(fisheye["shared_K"], dtype=np.float64)
    selected_d = np.asarray(fisheye["shared_D"], dtype=np.float64)
    poses = list(fisheye["poses"])

    # 联合 BA 后继续执行原有物理软检查；这些检查用于标记异常，不反向固定参数。
    positive_heights = [
        float(row["height_metres"]) for row in poses if row["height_metres"] > 0
    ]
    median_height = (
        float(np.median(positive_heights)) if positive_heights else float("nan")
    )
    for row in poses:
        downward = float(-np.asarray(row["optical_axis_world"])[2])
        height_deviation = (
            abs(float(row["height_metres"]) - median_height) / median_height
            if np.isfinite(median_height) and median_height > 0
            else float("inf")
        )
        reasons: list[str] = []
        if not row["camera_above_ground"]:
            reasons.append("相机中心不在地面上方")
        if not row["looks_toward_ground"] or downward < config.min_downward_axis_component:
            reasons.append("光轴没有稳定朝向地面")
        if not row["all_control_points_in_front"]:
            reasons.append("存在控制点位于相机后方")
        if row["pose_reprojection_rmse_px"] > config.max_pose_reprojection_rmse_px:
            reasons.append("鱼眼联合 BA 后重投影误差过大")
        if height_deviation > config.max_height_relative_deviation:
            reasons.append("高度偏离正高度中位数超过软阈值")
        row["height_relative_deviation_from_median"] = height_deviation
        row["downward_optical_axis_component"] = downward
        row["physical_plausibility_passed"] = not reasons
        row["physical_plausibility_reasons"] = reasons
        row["classification"] = (
            "fisheye_fit_passed" if not reasons else "fisheye_fit_suspect"
        )

    fisheye_model = {
        "definition": "opencv_fisheye_shared_K_D_joint_poses",
        "camera_model": "OPENCV_FISHEYE",
        "K": selected_k,
        "D": selected_d,
        "intrinsics_validation": fisheye["intrinsics_validation"],
        "initialization": fisheye["initialization"],
        "bundle_adjustment": fisheye["bundle_adjustment"],
    }
    model_reports = {"FISHEYE": fisheye_model}
    selection_reason = (
        "全部镜头按同一型号鱼眼镜头处理；共享 K/D，逐机位姿与同一组人工控制观测"
        "先形成拟合初始化基线，再执行鲁棒联合 BA。"
    )
    intrinsics_groups = [
        {
            "group_id": "shared_fisheye_all_cameras",
            "status": fisheye["intrinsics_validation"]["status"],
            "reason": selection_reason,
            "camera_ids": [row["physical_camera_id"] for row in homographies],
            "native_resolutions": sorted(resolutions),
            "canonical_resolution": list(image_size),
            "normalization": (
                "每台相机使用 diag(Wc/Wi,Hc/Hi,1) 进入统一参考像素系；"
                "K 在参考像素系共享，D 为无量纲鱼眼系数并全局共享。"
            ),
            "camera_model": "OPENCV_FISHEYE",
            "shared_K": selected_k,
            "shared_D": selected_d,
            "intrinsics_validation": fisheye["intrinsics_validation"],
            "pose_count": len(poses),
        }
    ]
    if poses:
        _enrich_pose_plan_validation(
            poses,
            homographies,
            scale,
            scale_metadata["mall_plan_coordinates_percent"],
        )
    feature_track_validation = _feature_track_plane_validation(
        repository, homographies, config
    )
    comparison = _legacy_position_comparison(repository, poses, scale)
    artifacts = _save_visualizations(
        dataset, output, scale, homographies, poses, comparison
    )

    participating_ids = [row["physical_camera_id"] for row in homographies]
    suspected = [
        row["physical_camera_id"]
        for row in poses
        if not row["physical_plausibility_passed"]
    ]
    selected_stable = bool(
        fisheye["bundle_adjustment"]["success"]
        and fisheye["intrinsics_validation"]["usable_for_sfm"]
    )
    physical_pass_rate = (
        sum(row["physical_plausibility_passed"] for row in poses) / len(poses)
        if poses
        else 0.0
    )
    internally_consistent = bool(
        selected_stable
        and physical_pass_rate >= 0.75
        and fisheye["intrinsics_validation"]["status"] == "credible"
    )
    conclusion = (
        "同型号鱼眼共享 K/D 与逐机位姿已完成联合拟合和鲁棒 BA，"
        "且内参可逆性和大多数逐机物理检查通过；结果可进入 Sfm 候选重建。"
        if internally_consistent
        else
        "鱼眼共享 K/D、逐机位姿拟合与 BA 已执行，但内参可逆性、数值收敛或"
        "逐机物理检查未充分通过；正式 Sfm 不得把本轮拟合参数直接标为可信。"
    )
    report = {
        "version": EXPERIMENT_VERSION,
        "status": (
            "complete"
            if internally_consistent
            else "completed_with_warnings"
        ),
        "writes_performed": True,
        "input": {
            "dataset_path": str(dataset),
            "calibration_file": str(calibration_path),
            "scale_file": str(scale_path),
            # 保留旧报告字段，值可能指向新 JSON 文件，避免已有报告读取器失效。
            "cali_txt": str(calibration_path),
            "scale_txt": str(scale_path),
            "image_count": len(images),
            "image_resolutions": [list(size) for size in sorted(resolutions)],
            "image_resolution": list(image_size),
            "image_resolution_compatibility": (
                "旧字段表示主内参组的规范分辨率；原生尺寸见 image_resolutions"
            ),
            "source_business_record_count": sum(copy_counts.values()),
            "independent_camera_correspondence_count": len(records),
            "resolution_diagnostics": loaded.input_diagnostics,
        },
        "config": config.as_dict(),
        "coordinate_conventions": {
            "image": "u right, v down, pixel",
            "plan_percent": "rx right, ry down, percent",
            "world": "X=rw right, Y=-rh plan-up, Z up; metre; ground Z=0",
            "homography": "world_plane_to_image, H[2,2]=1",
            "intrinsics_normalization": (
                "全部相机转换到统一参考分辨率；共享 K/D，原生 K 由逆尺度矩阵恢复"
            ),
            "extrinsics": "world_to_camera: x_camera=R*x_world+T",
            "camera_center": "C=-R^T*T",
            "height": "C_z in metres",
            "optical_axis_world": "R^T*[0,0,1]; negative Z means looking down",
        },
        "scale": scale.as_dict(),
        "scale_metadata": scale_metadata,
        "identity_audit": identity,
        "homographies": homographies,
        "excluded_cameras": excluded,
        "intrinsics_groups": intrinsics_groups,
        "intrinsics_models": model_reports,
        "selection": {
            "primary_group": "shared_fisheye_all_cameras",
            "selected_model": selected,
            "reason": selection_reason,
            "selected_K": selected_k,
            "selected_D": selected_d,
            "intrinsics_validation": fisheye["intrinsics_validation"],
            # 兼容旧 GUI/报告读取字段；鱼眼模型固定为 4 个系数。
            "distortion_D": selected_d,
            "distortion_observability": (
                "按同型号鱼眼假设在全部相机控制观测上联合拟合 k1～k4；"
                "拟合数据允许继续用于 BA，不再固定 D=0。"
            ),
        },
        "poses": poses,
        "legacy_position_comparison": comparison,
        "feature_track_plane_validation": feature_track_validation,
        "nonlinear_optimization": {
            "performed": "shared_fisheye_K_D_and_joint_pose_bundle_adjustment",
            "joint_K_D_R_T_ba_performed": True,
            "initialization": fisheye["initialization"],
            "bundle_adjustment": fisheye["bundle_adjustment"],
            "observation_policy": (
                "BA 从拟合初始化基线启动，并允许复用形成该基线的人工控制观测。"
            ),
        },
        "summary": {
            "included_camera_count": len(homographies),
            "pose_camera_count": len(poses),
            "intrinsics_group_count": len(intrinsics_groups),
            "included_camera_ids": participating_ids,
            "excluded_camera_count": len(excluded),
            "post_pose_suspect_camera_ids": suspected,
            "selected_model_stable": selected_stable,
            "physical_plausibility_pass_rate": physical_pass_rate,
            "internally_consistent_initialization": internally_consistent,
            "credible_for_physical_calibration": False,
            "independent_validation_available": False,
            "intrinsics_validation_status": fisheye["intrinsics_validation"]["status"],
            "intrinsics_usable_for_sfm": fisheye["intrinsics_validation"][
                "usable_for_sfm"
            ],
            "fitted_initialization_available": bool(
                fisheye["initialization"]["fit"]["success"]
            ),
            "bundle_adjustment_performed": True,
            "bundle_adjustment_success": bool(
                fisheye["bundle_adjustment"]["success"]
            ),
            "safe_as_ba_initialization": bool(
                fisheye["initialization"]["fit"]["success"]
                and fisheye["intrinsics_validation"]["usable_for_sfm"]
            ),
        },
        "artifacts": artifacts,
        "conclusion": conclusion,
    }

    _write_json(output / "scale_analysis.json", {"scale": report["scale"], "metadata": scale_metadata})
    _write_json(output / "camera_correspondence.json", identity)
    _write_json(
        output / "homography_diagnostics.json",
        {"homographies": homographies, "excluded_cameras": excluded},
    )
    _write_json(output / "intrinsics_models.json", model_reports)
    _write_json(output / "intrinsics_groups.json", intrinsics_groups)
    _write_json(
        output / "intrinsics_validity.json",
        fisheye["intrinsics_validation"],
    )
    _write_json(output / "fitted_initialization.json", fisheye["initialization"])
    _write_json(output / "bundle_adjustment.json", fisheye["bundle_adjustment"])
    _write_json(output / "poses.json", {"coordinate_conventions": report["coordinate_conventions"], "poses": poses})
    _write_json(output / "legacy_position_comparison.json", comparison)
    _write_json(output / "feature_track_plane_validation.json", feature_track_validation)
    _write_json(output / "fisheye_calibration_report.json", report)
    _write_json(output / "shared_calibration_report.json", report)
    markdown = _markdown_report(_json_value(report))
    (output / "fisheye_calibration_report.md").write_text(markdown, encoding="utf-8")
    (output / "shared_calibration_report.md").write_text(markdown, encoding="utf-8")
    return _json_value(report)


# 新名称供后续代码使用；保留旧函数名以兼容既有命令和报告调用方。
run_fisheye_calibration = run_shared_intrinsics_calibration
