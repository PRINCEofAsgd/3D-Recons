"""共享鱼眼内参的数值、物理可逆性和 Sfm 可用性门禁。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True)
class FisheyeIntrinsicsValidationConfig:
    """集中定义鱼眼内参的保守物理范围。"""

    minimum_focal_ratio: float = 0.08
    maximum_focal_ratio: float = 4.0
    minimum_focal_aspect_ratio: float = 0.5
    maximum_focal_aspect_ratio: float = 2.0
    maximum_abs_distortion: float = 1.5
    minimum_radial_derivative: float = 1.0e-3
    maximum_supported_half_angle_rad: float = 2.2
    radial_sample_count: int = 4097


def fisheye_distorted_angle(theta: np.ndarray, D: Iterable[float]) -> np.ndarray:
    """计算 OpenCV fisheye 的 theta 到 theta_d 多项式映射。"""

    k1, k2, k3, k4 = np.asarray(tuple(D), dtype=np.float64).reshape(4)
    theta2 = theta * theta
    return theta * (
        1.0
        + k1 * theta2
        + k2 * theta2**2
        + k3 * theta2**3
        + k4 * theta2**4
    )


def fisheye_radial_derivative(theta: np.ndarray, D: Iterable[float]) -> np.ndarray:
    """计算 theta_d 对 theta 的导数，用于发现投影折返。"""

    k1, k2, k3, k4 = np.asarray(tuple(D), dtype=np.float64).reshape(4)
    theta2 = theta * theta
    return (
        1.0
        + 3.0 * k1 * theta2
        + 5.0 * k2 * theta2**2
        + 7.0 * k3 * theta2**3
        + 9.0 * k4 * theta2**4
    )


def _required_normalized_radius(
    K: np.ndarray,
    resolution: tuple[int, int],
) -> float:
    """计算图像四角相对主点所需覆盖的最大归一化半径。"""

    width, height = resolution
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    corners = np.asarray(
        ((0.0, 0.0), (width, 0.0), (0.0, height), (width, height)),
        dtype=np.float64,
    )
    radii = np.sqrt(
        ((corners[:, 0] - cx) / fx) ** 2
        + ((corners[:, 1] - cy) / fy) ** 2
    )
    return float(np.max(radii))


def validate_fisheye_intrinsics(
    K: Iterable[Iterable[float]],
    D: Iterable[float],
    resolution: tuple[int, int],
    *,
    fit_summary: dict[str, Any] | None = None,
    ba_summary: dict[str, Any] | None = None,
    config: FisheyeIntrinsicsValidationConfig | None = None,
) -> dict[str, Any]:
    """评价共享 K/D 是否可安全反投影并进入 Sfm。

    “可进入 Sfm”要求参数有限、主点位于图像内、鱼眼半径映射覆盖整幅
    图像且在该范围内严格单调。求解器未严格收敛会降为 warning，但不会
    掩盖物理不可逆等 hard failure。
    """

    cfg = config or FisheyeIntrinsicsValidationConfig()
    matrix = np.asarray(K, dtype=np.float64).reshape(3, 3)
    distortion = np.asarray(tuple(D), dtype=np.float64).reshape(4)
    width, height = (int(resolution[0]), int(resolution[1]))
    dimension = float(max(width, height))
    failures: list[str] = []
    warnings: list[str] = []

    finite = bool(np.isfinite(matrix).all() and np.isfinite(distortion).all())
    if not finite:
        failures.append("K/D 包含非有限数值")
    fx, fy = float(matrix[0, 0]), float(matrix[1, 1])
    cx, cy = float(matrix[0, 2]), float(matrix[1, 2])
    if fx <= 0 or fy <= 0:
        failures.append("焦距必须为正数")
    focal_ratio = min(fx, fy) / dimension if dimension > 0 else 0.0
    focal_aspect = fx / fy if fy > 0 else float("inf")
    if not (cfg.minimum_focal_ratio <= focal_ratio <= cfg.maximum_focal_ratio):
        failures.append("焦距与图像尺寸比例超出保守范围")
    if not (
        cfg.minimum_focal_aspect_ratio
        <= focal_aspect
        <= cfg.maximum_focal_aspect_ratio
    ):
        failures.append("fx/fy 比例超出同一像素尺度的保守范围")
    principal_point_inside = 0.0 <= cx <= width and 0.0 <= cy <= height
    if not principal_point_inside:
        failures.append("主点位于图像范围外")
    if float(np.max(np.abs(distortion))) > cfg.maximum_abs_distortion:
        failures.append("鱼眼畸变系数绝对值超过配置上限")

    required_radius = (
        _required_normalized_radius(matrix, (width, height))
        if finite and fx > 0 and fy > 0
        else float("inf")
    )
    theta = np.linspace(
        0.0,
        cfg.maximum_supported_half_angle_rad,
        cfg.radial_sample_count,
        dtype=np.float64,
    )
    theta_d = fisheye_distorted_angle(theta, distortion)
    derivative = fisheye_radial_derivative(theta, distortion)
    reached = np.flatnonzero(theta_d >= required_radius)
    coverage_reached = bool(len(reached))
    if coverage_reached:
        reach_index = int(reached[0])
        supported_half_angle = float(theta[reach_index])
        minimum_derivative = float(np.min(derivative[: reach_index + 1]))
    else:
        supported_half_angle = None
        minimum_derivative = float(np.min(derivative))
        failures.append("鱼眼投影在允许角度内不能覆盖图像四角")
    radial_monotonic = bool(
        coverage_reached and minimum_derivative > cfg.minimum_radial_derivative
    )
    if coverage_reached and not radial_monotonic:
        failures.append("鱼眼投影在图像所需视场内发生折返或不可逆")

    fit_accepted = bool((fit_summary or {}).get("accepted_by_reprojection_threshold"))
    ba_accepted = bool((ba_summary or {}).get("accepted_by_reprojection_threshold"))
    fit_converged = bool((fit_summary or {}).get("solver_converged"))
    ba_converged = bool((ba_summary or {}).get("solver_converged"))
    if fit_summary is not None and not fit_accepted:
        failures.append("拟合初始化未通过重投影阈值")
    if ba_summary is not None and not ba_accepted:
        failures.append("联合 BA 未通过重投影阈值")
    if fit_summary is not None and not fit_converged:
        warnings.append("拟合初始化达到求值上限或未满足严格收敛条件")
    if ba_summary is not None and not ba_converged:
        warnings.append("联合 BA 达到求值上限或未满足严格收敛条件")

    usable_for_sfm = not failures
    status = "credible" if usable_for_sfm and not warnings else (
        "warning" if usable_for_sfm else "rejected"
    )
    return {
        "status": status,
        "usable_for_sfm": usable_for_sfm,
        "hard_failure_count": len(failures),
        "warning_count": len(warnings),
        "hard_failures": failures,
        "warnings": warnings,
        "resolution": [width, height],
        "focal_ratio_to_max_dimension": focal_ratio,
        "focal_aspect_ratio": focal_aspect,
        "principal_point_inside_image": principal_point_inside,
        "principal_point_normalized": [
            cx / width if width else None,
            cy / height if height else None,
        ],
        "maximum_abs_distortion": float(np.max(np.abs(distortion))),
        "required_normalized_corner_radius": required_radius,
        "coverage_reached": coverage_reached,
        "radial_monotonic_through_image": radial_monotonic,
        "minimum_radial_derivative_through_image": minimum_derivative,
        "supported_half_angle_degrees": (
            math.degrees(supported_half_angle)
            if supported_half_angle is not None
            else None
        ),
        "fit_solver_converged": fit_converged,
        "ba_solver_converged": ba_converged,
        "fit_accepted_by_reprojection_threshold": fit_accepted,
        "ba_accepted_by_reprojection_threshold": ba_accepted,
    }


def provisional_fisheye_intrinsics(
    initial_K: Iterable[Iterable[float]],
    fitted_D: Iterable[float],
    resolution: tuple[int, int],
    *,
    minimum_scale: float = 1.0e-6,
) -> tuple[np.ndarray, np.ndarray, float, dict[str, Any]]:
    """从拟合 D 生成保持非零、物理可逆的试算内参。

    该结果不是认证内参。它只在正式拟合 K/D 被门禁拒绝时为多候选 Sfm
    提供一个接近稳定初值的非零畸变分支，最终仍必须通过三维模型验收。
    """

    K = np.asarray(initial_K, dtype=np.float64).reshape(3, 3).copy()
    D = np.asarray(tuple(fitted_D), dtype=np.float64).reshape(4)
    if not np.any(np.abs(D) > 0.0):
        raise ValueError("拟合 D 全为零，无法构造非零鱼眼试算参数")
    width, height = resolution
    # 初始单应解偶尔略微越界；试算分支只做像素范围内裁剪，不声称重新标定。
    K[0, 2] = float(np.clip(K[0, 2], 0.0, float(width)))
    K[1, 2] = float(np.clip(K[1, 2], 0.0, float(height)))
    scales = [1.0]
    while scales[-1] > minimum_scale:
        scales.append(max(minimum_scale, scales[-1] * 0.5))
        if scales[-1] == minimum_scale:
            break
    for scale in scales:
        candidate = D * scale
        validation = validate_fisheye_intrinsics(K, candidate, resolution)
        if validation["usable_for_sfm"]:
            return K, candidate, float(scale), validation
    validation = validate_fisheye_intrinsics(K, D * minimum_scale, resolution)
    return K, D * minimum_scale, float(minimum_scale), validation
