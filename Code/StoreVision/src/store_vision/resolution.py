"""异分辨率图片的坐标适配、尺寸分组和输入诊断。

本模块只变换坐标和矩阵，不改写源图。未知裁剪或宽高比变化不会被猜测为
普通缩放，防止程序生成数值存在但物理含义错误的 Homography/K/R/T。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np


def read_image_size(path: str | Path) -> tuple[int, int] | None:
    """只解码图片尺寸，返回 ``(W, H)``。"""
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    return int(image.shape[1]), int(image.shape[0])


def same_aspect_ratio(
    first: tuple[int, int],
    second: tuple[int, int],
    *,
    relative_tolerance: float = 0.001,
) -> bool:
    """判断两个尺寸能否视为同一视场的等比缩放。

    0.1% 容差只用于兼容编码器的少量边缘差异；实际坐标转换仍分别使用 x/y
    比例并在诊断中保存，不会隐藏两轴差异。
    """
    fw, fh = first
    sw, sh = second
    if min(fw, fh, sw, sh) <= 0:
        return False
    return abs(fw / fh - sw / sh) / max(fw / fh, sw / sh) <= relative_tolerance


def coordinates_are_normalized(points: np.ndarray) -> bool:
    """识别上游 0～1 相对坐标，不把普通的小像素四边形误判为归一化值。"""
    values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return bool(
        len(values)
        and np.isfinite(values).all()
        and float(values.min()) >= -0.01
        and float(values.max()) <= 1.01
    )


def points_inside_image(
    points: np.ndarray,
    image_size: tuple[int, int],
    *,
    tolerance_px: float = 2.0,
) -> bool:
    """检查全部点是否位于图片边界附近。"""
    values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    width, height = image_size
    return bool(
        len(values)
        and np.isfinite(values).all()
        and np.all(values[:, 0] >= -tolerance_px)
        and np.all(values[:, 1] >= -tolerance_px)
        and np.all(values[:, 0] <= width - 1 + tolerance_px)
        and np.all(values[:, 1] <= height - 1 + tolerance_px)
    )


def transform_points(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    """用 3×3 仿射/单应矩阵转换二维点。"""
    values = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(values, np.asarray(transform, dtype=np.float64)).reshape(-1, 2)


def adapt_calibration_points(
    points: np.ndarray,
    image_size: tuple[int, int],
    calibration_size: tuple[int, int] | None,
) -> tuple[np.ndarray, np.ndarray, str, list[dict[str, Any]]]:
    """将标定点转换到当前图片像素坐标系。

    支持像素坐标精确匹配、同宽高比缩放和 0～1 归一化坐标。未知裁剪或无
    标定尺寸的越界点返回 error，调用方应阻止几何阶段而不是继续猜测。
    """
    values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    width, height = image_size
    issues: list[dict[str, Any]] = []

    if coordinates_are_normalized(values):
        if calibration_size is not None and not same_aspect_ratio(
            calibration_size, image_size
        ):
            issues.append(
                {
                    "severity": "error",
                    "code": "normalized_aspect_mismatch",
                    "message": (
                        f"归一化标定声明尺寸 {calibration_size} 与图片尺寸 "
                        f"{image_size} 宽高比不同，无法确认裁剪关系"
                    ),
                }
            )
        transform = np.array(
            [[float(width), 0.0, 0.0], [0.0, float(height), 0.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        converted = transform_points(values, transform)
        mode = "normalized_to_image"
    elif calibration_size is not None:
        cal_width, cal_height = calibration_size
        if calibration_size == image_size:
            transform = np.eye(3, dtype=np.float64)
            converted = values.copy()
            mode = "pixel_exact"
        elif same_aspect_ratio(calibration_size, image_size):
            transform = np.array(
                [
                    [width / cal_width, 0.0, 0.0],
                    [0.0, height / cal_height, 0.0],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
            converted = transform_points(values, transform)
            mode = "pixel_scaled"
            issues.append(
                {
                    "severity": "info",
                    "code": "calibration_scaled",
                    "message": (
                        f"标定坐标已从 {calibration_size} 等比转换到图片 "
                        f"{image_size}"
                    ),
                }
            )
        else:
            transform = np.eye(3, dtype=np.float64)
            converted = values.copy()
            mode = "pixel_unsafe_aspect_mismatch"
            issues.append(
                {
                    "severity": "error",
                    "code": "calibration_aspect_mismatch",
                    "message": (
                        f"标定尺寸 {calibration_size} 与图片尺寸 {image_size} "
                        "宽高比不同，缺少明确裁剪/填边变换"
                    ),
                }
            )
    else:
        transform = np.eye(3, dtype=np.float64)
        converted = values.copy()
        mode = "pixel_inferred_exact"
        if not points_inside_image(converted, image_size):
            mode = "pixel_unresolved"
            issues.append(
                {
                    "severity": "error",
                    "code": "calibration_points_out_of_bounds",
                    "message": (
                        f"标定点超出图片 {image_size}，且输入没有提供标定分辨率，"
                        "不能安全推断缩放或裁剪关系"
                    ),
                }
            )

    if not points_inside_image(converted, image_size):
        issues.append(
            {
                "severity": "error",
                "code": "adapted_points_out_of_bounds",
                "message": f"转换后的标定点仍超出图片范围 {image_size}",
            }
        )
    return converted, transform, mode, issues


def canonicalize_homographies(
    rows: Iterable[dict[str, Any]],
) -> tuple[tuple[int, int], list[dict[str, Any]]]:
    """把同宽高比组的 H 和图像点转换到组内最大规范分辨率。"""
    values = list(rows)
    if not values:
        raise ValueError("规范化至少需要一个 Homography")
    canonical_size = max(
        (tuple(row["image_resolution"]) for row in values),
        key=lambda size: int(size[0]) * int(size[1]),
    )
    canonical_width, canonical_height = canonical_size
    converted: list[dict[str, Any]] = []
    for row in values:
        width, height = row["image_resolution"]
        transform = np.array(
            [
                [canonical_width / width, 0.0, 0.0],
                [0.0, canonical_height / height, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        item = dict(row)
        item["native_H_world_plane_to_image"] = np.asarray(
            row["H_world_plane_to_image"], dtype=np.float64
        )
        item["native_source_image_points_px"] = np.asarray(
            row["source_image_points_px"], dtype=np.float64
        )
        item["canonical_resolution"] = list(canonical_size)
        item["image_to_canonical_transform"] = transform
        item["H_world_plane_to_image_canonical"] = (
            transform @ item["native_H_world_plane_to_image"]
        )
        item["source_image_points_canonical_px"] = transform_points(
            item["native_source_image_points_px"], transform
        )
        converted.append(item)
    return canonical_size, converted


def group_rows_by_aspect(
    rows: Iterable[dict[str, Any]],
    *,
    relative_tolerance: float = 0.001,
) -> list[list[dict[str, Any]]]:
    """按宽高比聚类；不同裁剪模式不会被强行拉伸进同一个共享 K。"""
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        size = tuple(row["image_resolution"])
        target = next(
            (
                group
                for group in groups
                if same_aspect_ratio(
                    size,
                    tuple(group[0]["image_resolution"]),
                    relative_tolerance=relative_tolerance,
                )
            ),
            None,
        )
        if target is None:
            groups.append([row])
        else:
            target.append(row)
    return sorted(
        groups,
        key=lambda group: (
            -len(group),
            -max(
                int(row["image_resolution"][0]) * int(row["image_resolution"][1])
                for row in group
            ),
        ),
    )


def scaled_pixel_value(
    value: int | float,
    image_size: tuple[int, int],
    *,
    reference_long_side: int = 2560,
    minimum: int = 1,
    odd: bool = False,
) -> int:
    """把历史 4K 像素参数缩放到当前图片，供检测形态学/Hough 使用。"""
    scale = max(image_size) / max(reference_long_side, 1)
    result = max(minimum, int(round(float(value) * scale)))
    if odd and result % 2 == 0:
        result += 1
    return result
