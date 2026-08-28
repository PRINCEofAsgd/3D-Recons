"""基于中间层 K/D/R/t 的参数驱动世界平面拼接。

流程与精确相机参数项目一致：按相机模型去畸变、由 world_to_camera 外参
推导 Z=0 平面单应、建立统一米制画布、输出逐相机映射、覆盖/重叠诊断、
透明叠加和距离权重羽化融合。当前模块不依赖 ``cali`` 的运行时 H。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from store_vision import __version__
from store_vision.data.workspace import (
    IntermediateRuntime,
    load_intermediate_runtime,
    model_undistort_image,
)

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class ParameterStitchConfig:
    """参数驱动拼接的可复现画布与融合配置。"""

    plane_z: float = 0.0
    pixels_per_metre: float = 40.0
    fallback_radius_metres: float = 20.0
    max_ground_distance_metres: float = 20.0
    diagnostic_alpha: float = 0.35
    feather_radius_pixels: int = 80
    crop_margin_pixels: int = 40
    max_canvas_long_edge: int = 5000
    max_canvas_pixels: int = 20_000_000
    jpeg_quality: int = 94


@dataclass(frozen=True)
class WorldCanvas:
    """右手世界地面与向下增长图像画布之间的变换。"""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    pixels_per_metre: float
    width: int
    height: int
    y_axis_direction: str

    def world_to_canvas_matrix(self) -> np.ndarray:
        """世界 X 向右、Y 向平面上方；画布 v 向下。"""

        scale = self.pixels_per_metre
        if self.y_axis_direction == "down":
            return np.asarray(
                [
                    [scale, 0.0, -self.x_min * scale],
                    [0.0, scale, -self.y_min * scale],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
        return np.asarray(
            [
                [scale, 0.0, -self.x_min * scale],
                [0.0, -scale, self.y_max * scale],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    def canvas_xy(self) -> tuple[np.ndarray, np.ndarray]:
        """返回每列/每行对应的世界 X/Y 坐标。"""

        x = (
            np.arange(self.width, dtype=np.float32)
            / self.pixels_per_metre
            + self.x_min
        )
        if self.y_axis_direction == "down":
            y = (
                np.arange(self.height, dtype=np.float32)
                / self.pixels_per_metre
                + self.y_min
            )
        else:
            y = (
                self.y_max
                - np.arange(self.height, dtype=np.float32)
                / self.pixels_per_metre
            )
        return x, y


@dataclass(frozen=True)
class StitchingWorkflowResult:
    """一次独立参数驱动拼接运行的界面交付结果。"""

    output_dir: Path
    candidate_name: str
    fused_path: Path
    alpha_path: Path
    coverage_path: Path
    overlap_path: Path
    report_path: Path
    summary: dict[str, Any]
    intermediate_path: Path


def _camera_center(camera: dict[str, Any]) -> np.ndarray:
    """由 world_to_camera R/t 计算世界光心。"""

    rotation = np.asarray(camera["R"], dtype=np.float64)
    translation = np.asarray(camera["t"], dtype=np.float64).reshape(3)
    return -rotation.T @ translation


def _scale_world_bounds(runtime: IntermediateRuntime) -> tuple[float, ...] | None:
    """优先从 scale 的 0～100% 地面范围恢复米制画布边界。"""

    frames = runtime.manifest.get("frames", {})
    plan_item = frames.get("plan_registration")
    if not plan_item:
        return None
    try:
        plan = json.loads(
            (runtime.package_path / str(plan_item)).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    model = plan.get("scale", {}).get("scale_model", {})
    required = (
        "x_metres_per_percent",
        "x_intercept_metres",
        "y_metres_per_percent",
        "y_intercept_metres",
    )
    if any(not isinstance(model.get(key), (int, float)) for key in required):
        return None
    x_values = [
        float(model["x_intercept_metres"]),
        float(model["x_intercept_metres"])
        + float(model["x_metres_per_percent"]) * 100.0,
    ]
    # StoreVision 世界 Y 为平面图 rh 的相反数。
    y_values = [
        -float(model["y_intercept_metres"]),
        -(
            float(model["y_intercept_metres"])
            + float(model["y_metres_per_percent"]) * 100.0
        ),
    ]
    return min(x_values), min(y_values), max(x_values), max(y_values)


def _derive_canvas(
    runtime: IntermediateRuntime,
    config: ParameterStitchConfig,
) -> WorldCanvas:
    """由米制平面边界或相机中心构建带尺寸保护的统一画布。"""

    bounds = _scale_world_bounds(runtime)
    cameras = list(runtime.camera_rig.get("cameras", {}).values())
    if not cameras:
        raise ValueError("中间层没有可用于拼接的相机")
    if bounds is None:
        centers = np.asarray([_camera_center(camera) for camera in cameras])
        radius = config.fallback_radius_metres
        bounds = (
            math.floor(float(centers[:, 0].min() - radius)),
            math.floor(float(centers[:, 1].min() - radius)),
            math.ceil(float(centers[:, 0].max() + radius)),
            math.ceil(float(centers[:, 1].max() + radius)),
        )
    x_min, y_min, x_max, y_max = map(float, bounds)
    # 给 scale 边界保留少量空间，避免边界像素被数值舍入裁掉。
    margin = 0.25
    x_min -= margin
    y_min -= margin
    x_max += margin
    y_max += margin
    world_width = max(x_max - x_min, 1e-6)
    world_height = max(y_max - y_min, 1e-6)
    requested = config.pixels_per_metre
    width = max(1, int(math.ceil(world_width * requested)))
    height = max(1, int(math.ceil(world_height * requested)))
    long_edge_scale = min(
        1.0, config.max_canvas_long_edge / max(width, height)
    )
    pixel_scale = min(
        1.0,
        math.sqrt(config.max_canvas_pixels / max(width * height, 1)),
    )
    actual = requested * min(long_edge_scale, pixel_scale)
    ground_item = runtime.manifest.get("frames", {}).get("ground_plane")
    axis_definition = ""
    if ground_item:
        try:
            ground = json.loads(
                (runtime.package_path / str(ground_item)).read_text(
                    encoding="utf-8"
                )
            )
            axis_definition = str(ground.get("axis_definition", ""))
        except (OSError, json.JSONDecodeError):
            axis_definition = ""
    return WorldCanvas(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        pixels_per_metre=actual,
        width=max(1, int(math.ceil(world_width * actual))),
        height=max(1, int(math.ceil(world_height * actual))),
        y_axis_direction="down" if "Y down" in axis_definition else "up",
    )


def _image_to_canvas(
    camera: dict[str, Any],
    new_k: np.ndarray,
    canvas: WorldCanvas,
    plane_z: float,
) -> np.ndarray:
    """计算去畸变像素到世界地面画布的单应矩阵。"""

    rotation = np.asarray(camera["R"], dtype=np.float64)
    translation = np.asarray(camera["t"], dtype=np.float64).reshape(3, 1)
    plane_translation = rotation[:, 2:3] * plane_z + translation
    world_to_image = new_k @ np.column_stack(
        [
            rotation[:, 0],
            rotation[:, 1],
            plane_translation.reshape(3),
        ]
    )
    if abs(float(np.linalg.det(world_to_image))) < 1e-12:
        raise ValueError(f"相机 {camera.get('camera_id')} 的地面单应不可逆")
    return canvas.world_to_canvas_matrix() @ np.linalg.inv(world_to_image)


def _front_mask(
    support: np.ndarray,
    camera: dict[str, Any],
    canvas: WorldCanvas,
    plane_z: float,
    max_ground_distance_metres: float,
) -> np.ndarray:
    """只保留相机前方且处于有效地面半径内的像素。"""

    x, y = canvas.canvas_xy()
    rotation = np.asarray(camera["R"], dtype=np.float64)
    translation = np.asarray(camera["t"], dtype=np.float64).reshape(3)
    depth = (
        rotation[2, 0] * x[None, :]
        + rotation[2, 1] * y[:, None]
        + rotation[2, 2] * plane_z
        + translation[2]
    )
    center = _camera_center(camera)
    ground_distance = np.sqrt(
        (x[None, :] - center[0]) ** 2
        + (y[:, None] - center[1]) ** 2
    )
    return (
        (support > 0)
        & (depth > 1e-6)
        & (ground_distance <= max_ground_distance_metres)
    ).astype(np.uint8)


def _write_image(
    path: Path,
    image: np.ndarray,
    quality: int,
) -> None:
    """写图并检查 OpenCV 编码结果。"""

    params = (
        [cv2.IMWRITE_JPEG_QUALITY, quality]
        if path.suffix.lower() in {".jpg", ".jpeg"}
        else []
    )
    if not cv2.imwrite(str(path), image, params):
        raise ValueError(f"无法写入图片：{path}")


def _crop_bounds(
    count: np.ndarray,
    margin: int,
) -> tuple[int, int, int, int]:
    """按覆盖并集裁剪公共画布。"""

    covered = (count > 0).astype(np.uint8)
    if not np.any(covered):
        raise ValueError("全部相机映射后没有有效地面像素")
    x, y, width, height = cv2.boundingRect(covered)
    return (
        max(0, x - margin),
        max(0, y - margin),
        min(count.shape[1], x + width + margin),
        min(count.shape[0], y + height + margin),
    )


def _overlap_visualization(count: np.ndarray) -> np.ndarray:
    """将覆盖相机数量编码为便于诊断的颜色。"""

    normalized = np.clip(count.astype(np.uint16) * 36, 0, 255)
    color = cv2.applyColorMap(normalized.astype(np.uint8), cv2.COLORMAP_TURBO)
    color[count == 0] = 0
    return color


def _coverage_polygons(
    mask: np.ndarray,
    canvas: WorldCanvas,
) -> list[list[list[float]]]:
    """从有效掩膜提取简化后的世界地面覆盖多边形。"""

    contours, _ = cv2.findContours(
        (mask * 255).astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    polygons: list[list[list[float]]] = []
    minimum_area = max(100.0, mask.size * 0.00005)
    for contour in contours:
        if cv2.contourArea(contour) < minimum_area:
            continue
        epsilon = max(2.0, cv2.arcLength(contour, True) * 0.002)
        simplified = cv2.approxPolyDP(contour, epsilon, True)
        points = simplified.reshape(-1, 2).astype(np.float64)
        world_x = (
            points[:, 0] / canvas.pixels_per_metre + canvas.x_min
        )
        world_y = (
            canvas.y_min + points[:, 1] / canvas.pixels_per_metre
            if canvas.y_axis_direction == "down"
            else canvas.y_max - points[:, 1] / canvas.pixels_per_metre
        )
        polygons.append(
            [
                [round(float(x), 4), round(float(y), 4)]
                for x, y in zip(world_x, world_y)
            ]
        )
    return polygons


def run_parameter_stitching(
    intermediate_path: str | Path,
    output_dir: str | Path,
    *,
    candidate_name: str | None = None,
    config: ParameterStitchConfig | None = None,
    progress: ProgressCallback | None = None,
) -> StitchingWorkflowResult:
    """运行参数驱动平面映射、覆盖诊断与羽化融合。"""

    runtime = load_intermediate_runtime(
        intermediate_path,
        candidate_name=candidate_name,
    )
    if not runtime.capabilities.stitching.ready:
        raise ValueError(
            "图像拼接所需中间数据不完整："
            + "、".join(runtime.capabilities.stitching.missing)
        )
    config = config or ParameterStitchConfig()
    if config.pixels_per_metre <= 0:
        raise ValueError("pixels_per_metre 必须大于 0")
    if (
        config.fallback_radius_metres <= 0
        or config.max_ground_distance_metres <= 0
    ):
        raise ValueError("画布回退半径和地面有效半径必须大于 0")
    if config.max_canvas_long_edge <= 0 or config.max_canvas_pixels <= 0:
        raise ValueError("画布尺寸上限必须大于 0")
    if not 0.0 < config.diagnostic_alpha <= 1.0:
        raise ValueError("diagnostic_alpha 必须在 (0, 1] 范围")
    if config.feather_radius_pixels < 0 or config.crop_margin_pixels < 0:
        raise ValueError("羽化半径和裁剪边距不能小于 0")
    if not 1 <= config.jpeg_quality <= 100:
        raise ValueError("jpeg_quality 必须在 [1, 100] 范围")
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    cameras_dir = output / "cameras"
    cameras_dir.mkdir(exist_ok=True)
    canvas = _derive_canvas(runtime, config)
    height, width = canvas.height, canvas.width

    accumulator = np.zeros((height, width, 3), dtype=np.float32)
    weight_sum = np.zeros((height, width), dtype=np.float32)
    alpha = np.zeros((height, width, 3), dtype=np.uint8)
    coverage = np.zeros((height, width, 3), dtype=np.uint8)
    overlap_count = np.zeros((height, width), dtype=np.uint8)
    reports: list[dict[str, Any]] = []
    palette = [
        (56, 189, 248),
        (251, 146, 60),
        (74, 222, 128),
        (244, 114, 182),
        (196, 181, 253),
        (250, 204, 21),
        (45, 212, 191),
        (248, 113, 113),
        (129, 140, 248),
        (163, 230, 53),
    ]
    rig_cameras = list(runtime.camera_rig.get("cameras", {}).values())
    if progress:
        progress(2, "已建立统一世界地面画布")
    for index, camera in enumerate(rig_cameras):
        image_path = Path(str(camera.get("image_path", "")))
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(
                f"无法读取相机 {camera.get('camera_id')} 图片：{image_path}"
            )
        undistorted, new_k = model_undistort_image(image, camera)
        transform = _image_to_canvas(
            camera, new_k, canvas, config.plane_z
        )
        warped = cv2.warpPerspective(
            undistorted,
            transform,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        support = cv2.warpPerspective(
            np.full(image.shape[:2], 255, dtype=np.uint8),
            transform,
            (width, height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
        )
        valid = _front_mask(
            support,
            camera,
            canvas,
            config.plane_z,
            config.max_ground_distance_metres,
        )
        valid_bool = valid > 0
        warped[~valid_bool] = 0
        distance = cv2.distanceTransform(
            valid, cv2.DIST_L2, cv2.DIST_MASK_3
        )
        weight = np.minimum(
            distance / max(config.feather_radius_pixels, 1), 1.0
        ).astype(np.float32)
        accumulator += warped.astype(np.float32) * weight[..., None]
        weight_sum += weight

        visible = alpha[valid_bool].astype(np.float32)
        pixels = warped[valid_bool].astype(np.float32)
        alpha[valid_bool] = np.clip(
            visible * (1.0 - config.diagnostic_alpha)
            + pixels * config.diagnostic_alpha,
            0,
            255,
        ).astype(np.uint8)
        overlap_count = np.minimum(
            overlap_count.astype(np.uint16) + valid.astype(np.uint16),
            255,
        ).astype(np.uint8)
        color = np.asarray(palette[index % len(palette)], dtype=np.float32)
        coverage[valid_bool] = np.clip(
            coverage[valid_bool].astype(np.float32) * 0.7 + color * 0.3,
            0,
            255,
        ).astype(np.uint8)

        camera_id = str(camera.get("camera_id"))
        _write_image(
            cameras_dir / f"{camera_id}_warped.jpg",
            warped,
            config.jpeg_quality,
        )
        _write_image(
            cameras_dir / f"{camera_id}_mask.png",
            valid * 255,
            config.jpeg_quality,
        )
        reports.append(
            {
                "camera_id": camera_id,
                "camera_model": camera.get("camera_model"),
                "camera_center_world_metres": _camera_center(camera).tolist(),
                "valid_pixels": int(np.count_nonzero(valid)),
                "valid_ratio_of_working_canvas": (
                    float(np.count_nonzero(valid)) / max(width * height, 1)
                ),
                "coverage_polygons_world_xy": _coverage_polygons(
                    valid,
                    canvas,
                ),
                "image_to_canvas": transform.tolist(),
                "warped_image": f"cameras/{camera_id}_warped.jpg",
                "valid_mask": f"cameras/{camera_id}_mask.png",
            }
        )
        if progress:
            progress(
                5 + int((index + 1) / len(rig_cameras) * 75),
                f"已映射 {camera_id}",
            )

    fused_float = np.zeros((height, width, 3), dtype=np.float32)
    np.divide(
        accumulator,
        weight_sum[..., None],
        out=fused_float,
        where=weight_sum[..., None] > 1e-8,
    )
    fused = np.clip(fused_float, 0, 255).astype(np.uint8)
    x0, y0, x1, y1 = _crop_bounds(
        overlap_count, config.crop_margin_pixels
    )
    crop = np.s_[y0:y1, x0:x1]
    fused = fused[crop]
    alpha = alpha[crop]
    coverage = coverage[crop]
    overlap_count = overlap_count[crop]
    overlap_mask = (overlap_count >= 2).astype(np.uint8) * 255
    overlap_visual = _overlap_visualization(overlap_count)
    # 与精确参数项目保持一致：逐机结果也裁到最终公共画布，便于直接逐层
    # 对照，而不是让报告中的单机图和融合图使用两个像素坐标系。
    for camera_report in reports:
        warped_path = output / camera_report["warped_image"]
        mask_path = output / camera_report["valid_mask"]
        warped_image = cv2.imread(str(warped_path), cv2.IMREAD_COLOR)
        mask_image = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if warped_image is None or mask_image is None:
            raise ValueError(
                f"无法重新读取逐机拼接产物：{camera_report['camera_id']}"
            )
        _write_image(
            warped_path,
            warped_image[crop],
            config.jpeg_quality,
        )
        _write_image(
            mask_path,
            mask_image[crop],
            config.jpeg_quality,
        )
        camera_report["valid_ratio_of_output_canvas"] = (
            camera_report["valid_pixels"]
            / max((x1 - x0) * (y1 - y0), 1)
        )

    output_y_min = (
        canvas.y_min + y0 / canvas.pixels_per_metre
        if canvas.y_axis_direction == "down"
        else canvas.y_max - y1 / canvas.pixels_per_metre
    )
    output_y_max = (
        canvas.y_min + y1 / canvas.pixels_per_metre
        if canvas.y_axis_direction == "down"
        else canvas.y_max - y0 / canvas.pixels_per_metre
    )
    output_canvas = WorldCanvas(
        x_min=canvas.x_min + x0 / canvas.pixels_per_metre,
        y_min=output_y_min,
        x_max=canvas.x_min + x1 / canvas.pixels_per_metre,
        y_max=output_y_max,
        pixels_per_metre=canvas.pixels_per_metre,
        width=x1 - x0,
        height=y1 - y0,
        y_axis_direction=canvas.y_axis_direction,
    )

    fused_path = output / "mosaic_fused.jpg"
    alpha_path = output / "mosaic_alpha.jpg"
    coverage_path = output / "coverage_visualization.png"
    overlap_path = output / "overlap_visualization.png"
    _write_image(fused_path, fused, config.jpeg_quality)
    _write_image(alpha_path, alpha, config.jpeg_quality)
    _write_image(coverage_path, coverage, config.jpeg_quality)
    _write_image(overlap_path, overlap_visual, config.jpeg_quality)
    _write_image(
        output / "overlap_mask.png",
        overlap_mask,
        config.jpeg_quality,
    )
    _write_image(
        output / "overlap_count.png",
        overlap_count,
        config.jpeg_quality,
    )
    histogram = {
        str(value): int(np.count_nonzero(overlap_count == value))
        for value in range(1, int(overlap_count.max()) + 1)
    }
    summary = {
        "covered_pixels": int(np.count_nonzero(overlap_count >= 1)),
        "overlap_pixels": int(np.count_nonzero(overlap_count >= 2)),
        "maximum_camera_count": int(overlap_count.max()),
        "count_histogram": histogram,
        "camera_count": len(reports),
    }
    ground_contract: dict[str, Any] = {}
    ground_item = runtime.manifest.get("frames", {}).get("ground_plane")
    if ground_item:
        try:
            ground_contract = json.loads(
                (runtime.package_path / str(ground_item)).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            ground_contract = {}
    report = {
        "schema_version": 1,
        "package_type": "store_vision_stitching_output",
        "application_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "dataset_id": runtime.manifest.get("dataset_id"),
        "source_intermediate": str(runtime.package_path),
        "calibration_candidate": runtime.candidate_name,
        "candidate_source": runtime.camera_rig.get("source"),
        "candidate_quality": runtime.manifest.get(
            "calibration_candidates", {}
        ).get(runtime.candidate_name, {}).get("quality_status"),
        "coordinate_contract": {
            "world_to_camera": "X_camera=R*X_world+t",
            "target_plane": f"Z={config.plane_z}",
            "world_axes": ground_contract.get("axis_definition"),
            "canvas_axes": "u right, v down",
            "world_unit": ground_contract.get("unit"),
            "verification_status": ground_contract.get(
                "verification_status", "verified"
            ),
        },
        "config": asdict(config),
        "working_canvas": asdict(canvas),
        "output_canvas": asdict(output_canvas),
        "crop": {
            "x": x0,
            "y": y0,
            "width": x1 - x0,
            "height": y1 - y0,
        },
        "summary": summary,
        "cameras": reports,
        "outputs": {
            "fused": fused_path.name,
            "alpha_diagnostic": alpha_path.name,
            "coverage_visualization": coverage_path.name,
            "overlap_visualization": overlap_path.name,
            "overlap_mask": "overlap_mask.png",
            "overlap_count": "overlap_count.png",
            "per_camera": "cameras",
        },
        "raw_inputs_modified": False,
    }
    report_path = output / "manifest.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if progress:
        progress(100, "参数驱动图像拼接完成")
    return StitchingWorkflowResult(
        output_dir=output,
        candidate_name=runtime.candidate_name,
        fused_path=fused_path,
        alpha_path=alpha_path,
        coverage_path=coverage_path,
        overlap_path=overlap_path,
        report_path=report_path,
        summary=summary,
        intermediate_path=runtime.package_path,
    )


__all__ = [
    "ParameterStitchConfig",
    "StitchingWorkflowResult",
    "WorldCanvas",
    "run_parameter_stitching",
]
