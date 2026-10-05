"""多平面单应矩阵的共享针孔内参与逐相机位姿求解。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import cv2
import numpy as np
from scipy.optimize import least_squares


MODEL_NAMES = {
    "A": "fixed_center_shared_f",
    "B": "fixed_center_independent_fx_fy",
    "C": "zero_skew_free_fx_fy_cx_cy",
}


@dataclass(frozen=True)
class IntrinsicsSolveResult:
    """一个共享内参模型的线性求解结果。"""

    model: str
    success: bool
    K: np.ndarray | None
    constraint_matrix: np.ndarray
    singular_values: np.ndarray
    rank: int
    condition_number: float
    nullspace_gap: float | None
    positive_definite: bool
    plausible: bool
    failure_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """输出 JSON 友好的模型诊断。"""
        return {
            "model": self.model,
            "model_name": MODEL_NAMES[self.model],
            "success": self.success,
            "K": self.K.tolist() if self.K is not None else None,
            "constraint_matrix_shape": list(self.constraint_matrix.shape),
            "rank": self.rank,
            "singular_values": self.singular_values.tolist(),
            "condition_number": self.condition_number,
            "nullspace_gap": self.nullspace_gap,
            "positive_definite_B": self.positive_definite,
            "plausible": self.plausible,
            "failure_reason": self.failure_reason,
        }


def _v_rows(H: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """生成张正友 `v12` 和 `v11-v22` 的 6 元对称矩阵约束行。"""
    h1, h2 = H[:, 0], H[:, 1]

    def v(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return np.array(
            [
                a[0] * b[0],
                a[0] * b[1] + a[1] * b[0],
                a[1] * b[1],
                a[2] * b[0] + a[0] * b[2],
                a[2] * b[1] + a[1] * b[2],
                a[2] * b[2],
            ],
            dtype=np.float64,
        )

    return v(h1, h2), v(h1, h1) - v(h2, h2)


def _constraint_matrix(
    homographies: Iterable[np.ndarray],
    image_size: tuple[int, int],
    model: str,
) -> np.ndarray:
    """按 A/B/C 模型抽取需要估计的 B 矩阵参数列。"""
    width, height = image_size
    center_shift = np.array(
        [[1.0, 0.0, -width / 2.0], [0.0, 1.0, -height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    rows: list[np.ndarray] = []
    for homography in homographies:
        H = np.asarray(homography, dtype=np.float64).reshape(3, 3)
        if model in {"A", "B"}:
            H = center_shift @ H
        orthogonal, equal_norm = _v_rows(H)
        if model == "A":
            # B=diag(q,q,s)，只估计公共焦距与齐次尺度。
            rows.extend(
                [
                    np.array([orthogonal[0] + orthogonal[2], orthogonal[5]]),
                    np.array([equal_norm[0] + equal_norm[2], equal_norm[5]]),
                ]
            )
        elif model == "B":
            # B=diag(qx,qy,s)，中心化后分别估计 fx/fy。
            rows.extend(
                [
                    orthogonal[[0, 2, 5]],
                    equal_norm[[0, 2, 5]],
                ]
            )
        elif model == "C":
            # 零 skew 令 B12=0，其余五个常用针孔参数自由。
            rows.extend(
                [
                    orthogonal[[0, 2, 3, 4, 5]],
                    equal_norm[[0, 2, 3, 4, 5]],
                ]
            )
        else:
            raise ValueError(f"unknown intrinsics model: {model}")
    matrix = np.asarray(rows, dtype=np.float64)
    # 列尺度归一化仅用于改善 SVD 数值条件，恢复解时再反归一化。
    return matrix


def _matrix_diagnostics(matrix: np.ndarray) -> tuple[np.ndarray, int, float, float | None]:
    """计算约束矩阵秩、奇异值、条件数和最小奇异值间隔。"""
    singular = np.linalg.svd(matrix, compute_uv=False)
    rank = int(np.linalg.matrix_rank(matrix))
    smallest = float(singular[-1]) if singular.size else 0.0
    condition = float(singular[0] / smallest) if smallest > 0 else float("inf")
    gap = (
        float(singular[-2] / singular[-1])
        if singular.size >= 2 and singular[-1] > 0
        else None
    )
    return singular, rank, condition, gap


def _recover_model_c(parameters: np.ndarray) -> tuple[np.ndarray | None, bool, str | None]:
    """从零 skew 的 B 参数按张正友闭式公式恢复 K。"""
    b11, b22, b13, b23, b33 = [float(value) for value in parameters]
    B = np.array(
        [[b11, 0.0, b13], [0.0, b22, b23], [b13, b23, b33]],
        dtype=np.float64,
    )
    for sign in (1.0, -1.0):
        candidate = sign * B
        values = np.linalg.eigvalsh(candidate)
        positive = bool(np.all(values > 1e-12))
        if not positive:
            continue
        b11, b22, b13, b23, b33 = (
            candidate[0, 0],
            candidate[1, 1],
            candidate[0, 2],
            candidate[1, 2],
            candidate[2, 2],
        )
        denominator = b11 * b22
        if abs(denominator) < 1e-15:
            continue
        cy = -b23 / b22
        lam = b33 - b13 * b13 / b11 - b23 * b23 / b22
        if lam <= 0 or b11 <= 0 or b22 <= 0:
            continue
        fx = np.sqrt(lam / b11)
        fy = np.sqrt(lam / b22)
        cx = -b13 / b11
        K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])
        return K, True, None
    return None, False, "B 不能规范为正定矩阵，无法恢复实数焦距"


def solve_shared_intrinsics(
    homographies: Iterable[np.ndarray],
    image_size: tuple[int, int],
    model: str,
) -> IntrinsicsSolveResult:
    """使用多个 `world_plane_to_image` H 联立求共享 K。"""
    homography_list = [np.asarray(H, dtype=np.float64).reshape(3, 3) for H in homographies]
    matrix = _constraint_matrix(homography_list, image_size, model)
    singular, rank, condition, gap = _matrix_diagnostics(matrix)
    failure: str | None = None
    K: np.ndarray | None = None
    positive = False

    if len(homography_list) < 2:
        failure = "至少需要两个不同姿态的单应矩阵"
    elif not np.isfinite(matrix).all():
        failure = "约束矩阵含 NaN/Inf"
    else:
        # 对列做单位范数缩放后求最小右奇异向量，降低米制平移列的量级影响。
        column_scale = np.linalg.norm(matrix, axis=0)
        if np.any(column_scale < 1e-15):
            failure = "约束矩阵存在全零参数列"
        else:
            normalized = matrix / column_scale
            _, _, vt = np.linalg.svd(normalized, full_matrices=False)
            parameters = vt[-1] / column_scale
            if model == "A":
                q, scale = [float(value) for value in parameters]
                if q * scale <= 0 or abs(q) < 1e-15:
                    failure = "公共焦距平方为负或退化"
                else:
                    focal = np.sqrt(scale / q)
                    width, height = image_size
                    K = np.array(
                        [[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]]
                    )
                    positive = True
            elif model == "B":
                qx, qy, scale = [float(value) for value in parameters]
                if qx * scale <= 0 or qy * scale <= 0 or min(abs(qx), abs(qy)) < 1e-15:
                    failure = "fx/fy 平方为负或退化"
                else:
                    fx, fy = np.sqrt(scale / qx), np.sqrt(scale / qy)
                    width, height = image_size
                    K = np.array(
                        [[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]]
                    )
                    positive = True
            else:
                K, positive, failure = _recover_model_c(parameters)

    width, height = image_size
    plausible = False
    if K is not None and np.isfinite(K).all():
        fx, fy, cx, cy = float(K[0, 0]), float(K[1, 1]), float(K[0, 2]), float(K[1, 2])
        dimension = float(max(width, height))
        plausible = bool(
            0.1 * dimension <= fx <= 10.0 * dimension
            and 0.1 * dimension <= fy <= 10.0 * dimension
            and -0.25 * width <= cx <= 1.25 * width
            and -0.25 * height <= cy <= 1.25 * height
        )
        if not plausible and failure is None:
            failure = "K 可计算但焦距或主点超出集中定义的合理范围"

    success = K is not None and positive and np.isfinite(K).all()
    return IntrinsicsSolveResult(
        model=model,
        success=success,
        K=K,
        constraint_matrix=matrix,
        singular_values=singular,
        rank=rank,
        condition_number=condition,
        nullspace_gap=gap,
        positive_definite=positive,
        plausible=plausible,
        failure_reason=failure,
    )


def refine_intrinsics_constraints(
    homographies: Iterable[np.ndarray],
    image_size: tuple[int, int],
    model: str,
    initial_K: np.ndarray | None,
) -> dict[str, Any]:
    """用无量纲正交/等范数残差细化 A/B/C 的物理参数。

    线性 SVD 仍负责给出 B、秩和可解性诊断；该步骤消除 H 任意齐次尺度和
    不同约束行量级对最终参数的权重偏置，不使用相机高度等物理硬先验。
    """
    homography_list = [np.asarray(H, dtype=np.float64).reshape(3, 3) for H in homographies]
    width, height = image_size
    dimension = float(max(width, height))
    if initial_K is not None and np.isfinite(initial_K).all():
        fx0 = float(np.clip(initial_K[0, 0], 0.1 * dimension, 10.0 * dimension))
        fy0 = float(np.clip(initial_K[1, 1], 0.1 * dimension, 10.0 * dimension))
        cx0 = float(np.clip(initial_K[0, 2], -0.25 * width, 1.25 * width))
        cy0 = float(np.clip(initial_K[1, 2], -0.25 * height, 1.25 * height))
    else:
        fx0 = fy0 = 0.85 * dimension
        cx0, cy0 = width / 2.0, height / 2.0

    if model == "A":
        x0 = np.array([np.log(fx0)])
        lower = np.array([np.log(0.1 * dimension)])
        upper = np.array([np.log(10.0 * dimension)])
    elif model == "B":
        x0 = np.array([np.log(fx0), np.log(fy0)])
        lower = np.log([0.1 * dimension, 0.1 * dimension])
        upper = np.log([10.0 * dimension, 10.0 * dimension])
    elif model == "C":
        x0 = np.array([np.log(fx0), np.log(fy0), cx0, cy0])
        lower = np.array(
            [np.log(0.1 * dimension), np.log(0.1 * dimension), -0.25 * width, -0.25 * height]
        )
        upper = np.array(
            [np.log(10.0 * dimension), np.log(10.0 * dimension), 1.25 * width, 1.25 * height]
        )
    else:
        raise ValueError(f"unknown intrinsics model: {model}")

    def unpack(parameters: np.ndarray) -> np.ndarray:
        fx = float(np.exp(parameters[0]))
        fy = fx if model == "A" else float(np.exp(parameters[1]))
        cx = width / 2.0 if model in {"A", "B"} else float(parameters[2])
        cy = height / 2.0 if model in {"A", "B"} else float(parameters[3])
        return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])

    def residuals(parameters: np.ndarray) -> np.ndarray:
        inverse_K = np.linalg.inv(unpack(parameters))
        values: list[float] = []
        for H in homography_list:
            transformed = inverse_K @ H
            first, second = transformed[:, 0], transformed[:, 1]
            norm1, norm2 = np.linalg.norm(first), np.linalg.norm(second)
            if min(norm1, norm2) < 1e-15:
                values.extend([1.0e6, 1.0e6])
                continue
            # 两项均无量纲，每台相机和两类约束具有相同基础权重。
            values.append(float(np.dot(first, second) / (norm1 * norm2)))
            values.append(float((norm1 - norm2) / ((norm1 + norm2) * 0.5)))
        return np.asarray(values, dtype=np.float64)

    try:
        optimized = least_squares(
            residuals,
            x0,
            bounds=(lower, upper),
            max_nfev=1000,
            method="trf",
        )
        K = unpack(optimized.x)
        values = residuals(optimized.x)
        success = bool(
            optimized.success and np.isfinite(K).all() and np.isfinite(values).all()
        )
        return {
            "success": success,
            "K": K,
            "cost_half_sum_squared": float(optimized.cost),
            "constraint_rmse": float(np.sqrt(np.mean(values**2))),
            "optimality": float(optimized.optimality),
            "function_evaluations": int(optimized.nfev),
            "message": str(optimized.message),
        }
    except (ValueError, np.linalg.LinAlgError) as exc:
        return {
            "success": False,
            "K": None,
            "cost_half_sum_squared": None,
            "constraint_rmse": None,
            "optimality": None,
            "function_evaluations": 0,
            "message": str(exc),
        }


def project_ground_points(
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    world_xy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """按 `x_camera=R*x_world+t` 投影 Z=0 地面点并返回深度。"""
    xy = np.asarray(world_xy, dtype=np.float64).reshape(-1, 2)
    points = np.column_stack([xy, np.zeros(len(xy))])
    camera = (R @ points.T + np.asarray(t).reshape(3, 1)).T
    image_h = (K @ camera.T).T
    with np.errstate(divide="ignore", invalid="ignore"):
        image = image_h[:, :2] / image_h[:, 2:3]
    return image, camera[:, 2]


def decompose_homography_pose(
    homography: np.ndarray,
    K: np.ndarray,
    world_xy: np.ndarray,
    image_points: np.ndarray,
) -> dict[str, Any]:
    """分解 H，并在两种整体符号中选择位于地面上方且朝下的解。"""
    H = np.asarray(homography, dtype=np.float64).reshape(3, 3)
    inverse_K = np.linalg.inv(np.asarray(K, dtype=np.float64).reshape(3, 3))
    transformed = inverse_K @ H
    norm1, norm2 = np.linalg.norm(transformed[:, 0]), np.linalg.norm(transformed[:, 1])
    if min(norm1, norm2) < 1e-12:
        raise ValueError("K^-1 H 的旋转列退化")
    base_scale = 2.0 / (norm1 + norm2)

    candidates: list[dict[str, Any]] = []
    for sign in (1.0, -1.0):
        scale = sign * base_scale
        r1 = scale * transformed[:, 0]
        r2 = scale * transformed[:, 1]
        t = scale * transformed[:, 2]
        initial = np.column_stack([r1, r2, np.cross(r1, r2)])
        u, _, vt = np.linalg.svd(initial)
        R = u @ vt
        if np.linalg.det(R) < 0:
            u[:, -1] *= -1.0
            R = u @ vt
        center = -R.T @ t
        optical_axis_world = R.T @ np.array([0.0, 0.0, 1.0])
        projected, depth = project_ground_points(K, R, t, world_xy)
        errors = np.linalg.norm(projected - np.asarray(image_points), axis=1)
        camera_above = bool(center[2] > 0)
        points_in_front = bool(np.all(depth > 0))
        looks_down = bool(optical_axis_world[2] < 0)
        score = (
            4 * int(camera_above)
            + 3 * int(points_in_front)
            + 3 * int(looks_down)
            - float(np.sqrt(np.mean(errors**2))) / 1000.0
        )
        candidates.append(
            {
                "R": R,
                "T": t,
                "camera_center": center,
                "optical_axis_world": optical_axis_world,
                "depths": depth,
                "rmse_image_px": float(np.sqrt(np.mean(errors**2))),
                "max_error_image_px": float(np.max(errors)),
                "camera_above_ground": camera_above,
                "all_control_points_in_front": points_in_front,
                "looks_toward_ground": looks_down,
                "score": score,
                "homography_scale": scale,
            }
        )
    chosen = max(candidates, key=lambda item: item["score"])
    rvec, _ = cv2.Rodrigues(chosen["R"])
    chosen["rotation_vector"] = rvec.reshape(3)
    return chosen
