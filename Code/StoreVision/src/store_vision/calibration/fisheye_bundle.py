"""同型号鱼眼相机的共享 K/D、逐机位姿拟合与联合 BA。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from store_vision.calibration.intrinsics_validation import (
    validate_fisheye_intrinsics,
)
from store_vision.calibration.shared_intrinsics import (
    decompose_homography_pose,
    refine_intrinsics_constraints,
    solve_shared_intrinsics,
)


@dataclass(frozen=True)
class FisheyeBundleConfig:
    """集中保存鱼眼拟合和 BA 的数值边界。"""

    initialization_max_nfev: int = 300
    bundle_adjustment_max_nfev: int = 500
    robust_loss_scale_px: float = 4.0
    acceptable_reprojection_rmse_px: float = 30.0
    distortion_coefficient_limit: float = 1.5
    minimum_camera_count: int = 2


def project_fisheye_points(
    K: np.ndarray,
    D: np.ndarray,
    rotation_vector: np.ndarray,
    translation: np.ndarray,
    world_xy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """按 OpenCV fisheye 模型投影 Z=0 地面点，并返回相机坐标深度。"""

    xy = np.asarray(world_xy, dtype=np.float64).reshape(-1, 2)
    world = np.column_stack([xy, np.zeros(len(xy), dtype=np.float64)])
    rvec = np.asarray(rotation_vector, dtype=np.float64).reshape(3)
    tvec = np.asarray(translation, dtype=np.float64).reshape(3)
    image, _ = cv2.fisheye.projectPoints(
        world.reshape(1, -1, 3),
        rvec.reshape(3, 1),
        tvec.reshape(3, 1),
        np.asarray(K, dtype=np.float64).reshape(3, 3),
        np.asarray(D, dtype=np.float64).reshape(4, 1),
    )
    rotation, _ = cv2.Rodrigues(rvec)
    camera = (rotation @ world.T + tvec.reshape(3, 1)).T
    return image.reshape(-1, 2), camera[:, 2]


def _initial_shared_k(
    homographies: list[np.ndarray],
    image_size: tuple[int, int],
) -> tuple[np.ndarray, dict[str, Any]]:
    """用针孔单应约束只生成鱼眼非线性拟合的有限初值。"""

    candidates: list[tuple[str, dict[str, Any]]] = []
    for model in ("C", "B", "A"):
        linear = solve_shared_intrinsics(homographies, image_size, model)
        refined = refine_intrinsics_constraints(
            homographies, image_size, model, linear.K
        )
        candidates.append(
            (
                model,
                {
                    "linear_success": linear.success,
                    "linear_plausible": linear.plausible,
                    "linear_failure_reason": linear.failure_reason,
                    "refined": refined,
                },
            )
        )
        if refined.get("success") and refined.get("K") is not None:
            return np.asarray(refined["K"], dtype=np.float64), {
                "source": f"pinhole_homography_model_{model}",
                "diagnostics": dict(candidates),
                "usage": "仅用于鱼眼拟合初始值，不作为最终相机模型或 D=0 结论。",
            }

    width, height = image_size
    focal = 0.85 * max(width, height)
    return (
        np.array(
            [[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        ),
        {
            "source": "bounded_default",
            "diagnostics": dict(candidates),
            "usage": "单应约束未给出有限初值，使用有界默认值启动鱼眼拟合。",
        },
    )


def _initial_poses(
    rows: list[dict[str, Any]],
    K: np.ndarray,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """由每机单应矩阵生成位于地面上方、朝向地面的初始位姿。"""

    poses: list[tuple[np.ndarray, np.ndarray]] = []
    for row in rows:
        pose = decompose_homography_pose(
            np.asarray(row["H_world_plane_to_image_canonical"], dtype=np.float64),
            K,
            np.asarray(row["source_world_points_metres"], dtype=np.float64),
            np.asarray(row["source_image_points_canonical_px"], dtype=np.float64),
        )
        poses.append(
            (
                np.asarray(pose["rotation_vector"], dtype=np.float64).reshape(3),
                np.asarray(pose["T"], dtype=np.float64).reshape(3),
            )
        )
    return poses


def _pack(
    K: np.ndarray,
    D: np.ndarray,
    poses: Iterable[tuple[np.ndarray, np.ndarray]],
) -> np.ndarray:
    """把共享 K/D 和逐机 rvec/tvec 打包为优化变量。"""

    values = [
        np.log(float(K[0, 0])),
        np.log(float(K[1, 1])),
        float(K[0, 2]),
        float(K[1, 2]),
        *np.asarray(D, dtype=np.float64).reshape(4).tolist(),
    ]
    for rvec, tvec in poses:
        values.extend(np.asarray(rvec).reshape(3).tolist())
        values.extend(np.asarray(tvec).reshape(3).tolist())
    return np.asarray(values, dtype=np.float64)


def _unpack(
    parameters: np.ndarray,
    camera_count: int,
) -> tuple[np.ndarray, np.ndarray, list[tuple[np.ndarray, np.ndarray]]]:
    """从优化变量恢复共享 K/D 和逐机位姿。"""

    fx, fy = np.exp(parameters[:2])
    cx, cy = parameters[2:4]
    K = np.array(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    D = np.asarray(parameters[4:8], dtype=np.float64)
    poses = []
    for index in range(camera_count):
        offset = 8 + index * 6
        poses.append(
            (
                np.asarray(parameters[offset : offset + 3], dtype=np.float64),
                np.asarray(parameters[offset + 3 : offset + 6], dtype=np.float64),
            )
        )
    return K, D, poses


def _bounds(
    x0: np.ndarray,
    image_size: tuple[int, int],
    camera_count: int,
    config: FisheyeBundleConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """限制内参和畸变范围，位姿保留足够大的数据相关搜索空间。"""

    width, height = image_size
    dimension = float(max(width, height))
    lower = np.full_like(x0, -np.inf)
    upper = np.full_like(x0, np.inf)
    lower[:8] = [
        np.log(0.08 * dimension),
        np.log(0.08 * dimension),
        0.0,
        0.0,
        *([-config.distortion_coefficient_limit] * 4),
    ]
    upper[:8] = [
        np.log(12.0 * dimension),
        np.log(12.0 * dimension),
        float(width),
        float(height),
        *([config.distortion_coefficient_limit] * 4),
    ]
    # Rodrigues 向量允许跨越一次完整旋转；平移用初值尺度给出宽松边界。
    for index in range(camera_count):
        offset = 8 + index * 6
        translation_scale = max(
            100.0, float(np.max(np.abs(x0[offset + 3 : offset + 6]))) * 20.0
        )
        lower[offset : offset + 3] = -2.0 * np.pi
        upper[offset : offset + 3] = 2.0 * np.pi
        lower[offset + 3 : offset + 6] = -translation_scale
        upper[offset + 3 : offset + 6] = translation_scale
    return lower, upper


def _jacobian_sparsity(rows: list[dict[str, Any]]) -> lil_matrix:
    """声明每个观测只依赖共享 K/D 和所属相机位姿，加速数值差分。"""

    residual_count = sum(len(row["source_world_points_metres"]) * 3 for row in rows)
    parameter_count = 8 + 6 * len(rows)
    sparsity = lil_matrix((residual_count, parameter_count), dtype=np.int8)
    pixel_cursor = 0
    depth_cursor = sum(len(row["source_world_points_metres"]) * 2 for row in rows)
    for camera_index, row in enumerate(rows):
        count = len(row["source_world_points_metres"])
        pixel_block = slice(pixel_cursor, pixel_cursor + count * 2)
        depth_block = slice(depth_cursor, depth_cursor + count)
        sparsity[pixel_block, :8] = 1
        offset = 8 + camera_index * 6
        sparsity[pixel_block, offset : offset + 6] = 1
        # 深度只与当前位姿有关，与 K/D 无关。
        sparsity[depth_block, offset : offset + 6] = 1
        pixel_cursor += count * 2
        depth_cursor += count
    return sparsity


def _residual_function(rows: list[dict[str, Any]]):
    """构造像素重投影和正深度软约束残差。"""

    def residuals(parameters: np.ndarray) -> np.ndarray:
        K, D, poses = _unpack(parameters, len(rows))
        pixel_values: list[np.ndarray] = []
        depth_values: list[np.ndarray] = []
        for row, (rvec, tvec) in zip(rows, poses):
            projected, depth = project_fisheye_points(
                K,
                D,
                rvec,
                tvec,
                np.asarray(row["source_world_points_metres"], dtype=np.float64),
            )
            observed = np.asarray(
                row["source_image_points_canonical_px"], dtype=np.float64
            )
            pixel_values.append((projected - observed).reshape(-1))
            # 初始单应分解通常已满足正深度；该软约束只阻止翻到相机后方。
            depth_values.append(np.minimum(depth - 1.0e-4, 0.0) * 100.0)
        return np.concatenate([*pixel_values, *depth_values])

    return residuals


def _stage_summary(
    result: Any,
    residuals: np.ndarray,
    observation_count: int,
    acceptable_reprojection_rmse_px: float,
) -> dict[str, Any]:
    """生成拟合初始化或 BA 阶段的稳定诊断字段。"""

    pixel_residuals = residuals[: observation_count * 2].reshape(-1, 2)
    norms = np.linalg.norm(pixel_residuals, axis=1)
    rmse = float(np.sqrt(np.mean(norms**2)))
    finite = bool(np.isfinite(result.x).all() and np.isfinite(norms).all())
    # 稀疏数值差分可能在达到 nfev 上限前仍未满足严格梯度阈值；像素误差
    # 已进入可用范围时保留“达到误差阈值”的成功判定，并原样报告求解器消息。
    accepted_by_error = bool(
        finite and rmse <= acceptable_reprojection_rmse_px
    )
    return {
        "success": bool(finite and (result.success or accepted_by_error)),
        "solver_converged": bool(result.success),
        "accepted_by_reprojection_threshold": accepted_by_error,
        "acceptance_reprojection_threshold_px": acceptable_reprojection_rmse_px,
        "message": str(result.message),
        "function_evaluations": int(result.nfev),
        "cost_half_sum_squared": float(result.cost),
        "optimality": float(result.optimality),
        "reprojection_rmse_px": rmse,
        "reprojection_median_px": float(np.median(norms)),
        "reprojection_max_px": float(np.max(norms)),
    }


def fit_shared_fisheye_bundle(
    rows: Iterable[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    config: FisheyeBundleConfig | None = None,
) -> dict[str, Any]:
    """先拟合共享鱼眼 K/D 和位姿，再以该拟合基线执行同数据联合 BA。"""

    config = config or FisheyeBundleConfig()
    values = list(rows)
    if len(values) < config.minimum_camera_count:
        raise ValueError(
            f"共享鱼眼拟合至少需要 {config.minimum_camera_count} 台有效相机"
        )
    homographies = [
        np.asarray(row["H_world_plane_to_image_canonical"], dtype=np.float64)
        for row in values
    ]
    K0, initialization = _initial_shared_k(homographies, image_size)
    # 求解器硬约束主点必须位于图像内；单应初值偶尔略微越界时先裁入
    # 合法域，避免 least_squares 因初值不满足 bounds 直接中止整轮标定。
    K0 = np.asarray(K0, dtype=np.float64).copy()
    K0[0, 2] = float(np.clip(K0[0, 2], 0.0, float(image_size[0])))
    K0[1, 2] = float(np.clip(K0[1, 2], 0.0, float(image_size[1])))
    poses0 = _initial_poses(values, K0)
    x0 = _pack(K0, np.zeros(4, dtype=np.float64), poses0)
    lower, upper = _bounds(x0, image_size, len(values), config)
    residual_function = _residual_function(values)
    sparsity = _jacobian_sparsity(values)
    observation_count = sum(
        len(row["source_world_points_metres"]) for row in values
    )

    fitted = least_squares(
        residual_function,
        x0,
        bounds=(lower, upper),
        jac_sparsity=sparsity,
        method="trf",
        loss="linear",
        x_scale="jac",
        max_nfev=config.initialization_max_nfev,
    )
    fitted_residuals = residual_function(fitted.x)
    fitted_summary = _stage_summary(
        fitted,
        fitted_residuals,
        observation_count,
        config.acceptable_reprojection_rmse_px,
    )

    # BA 明确从拟合结果启动，并允许复用形成拟合基线的控制观测。
    bundle = least_squares(
        residual_function,
        fitted.x,
        bounds=(lower, upper),
        jac_sparsity=sparsity,
        method="trf",
        loss="soft_l1",
        f_scale=config.robust_loss_scale_px,
        x_scale="jac",
        max_nfev=config.bundle_adjustment_max_nfev,
    )
    bundle_residuals = residual_function(bundle.x)
    bundle_summary = _stage_summary(
        bundle,
        bundle_residuals,
        observation_count,
        config.acceptable_reprojection_rmse_px,
    )
    K, D, poses = _unpack(bundle.x, len(values))
    intrinsics_validation = validate_fisheye_intrinsics(
        K,
        D,
        image_size,
        fit_summary=fitted_summary,
        ba_summary=bundle_summary,
    )

    pose_rows: list[dict[str, Any]] = []
    for row, (rvec, tvec) in zip(values, poses):
        rotation, _ = cv2.Rodrigues(rvec)
        center = -rotation.T @ tvec
        optical_axis = rotation.T @ np.array([0.0, 0.0, 1.0])
        projected, depth = project_fisheye_points(
            K,
            D,
            rvec,
            tvec,
            np.asarray(row["source_world_points_metres"], dtype=np.float64),
        )
        errors = np.linalg.norm(
            projected
            - np.asarray(row["source_image_points_canonical_px"], dtype=np.float64),
            axis=1,
        )
        pose_rows.append(
            {
                "physical_camera_id": row["physical_camera_id"],
                "image_name": row["image_name"],
                "convention": "world_to_camera: x_camera=R*x_world+T",
                "R": rotation,
                "rotation_vector": rvec,
                "T_metres": tvec,
                "camera_center_world_metres": center,
                "height_metres": float(center[2]),
                "optical_axis_world": optical_axis,
                "camera_above_ground": bool(center[2] > 0),
                "all_control_points_in_front": bool(np.all(depth > 0)),
                "looks_toward_ground": bool(optical_axis[2] < 0),
                "pose_reprojection_rmse_px": float(np.sqrt(np.mean(errors**2))),
                "pose_reprojection_max_px": float(np.max(errors)),
                "native_image_resolution": row["image_resolution"],
                "intrinsics_resolution": list(image_size),
            }
        )

    return {
        "camera_model": "OPENCV_FISHEYE",
        "same_camera_model_assumption": True,
        "shared_K": K,
        "shared_D": D,
        "intrinsics_validation": intrinsics_validation,
        "initialization": {
            **initialization,
            "initial_K": K0,
            "initial_D": np.zeros(4, dtype=np.float64),
            "fit": fitted_summary,
        },
        "bundle_adjustment": {
            **bundle_summary,
            "performed": True,
            "initialization_source": "fitted_initialization",
            "uses_fitted_calibration_observations": True,
            "loss": "soft_l1",
            "loss_scale_px": config.robust_loss_scale_px,
        },
        "poses": pose_rows,
        "observation_count": observation_count,
        "camera_count": len(values),
    }
