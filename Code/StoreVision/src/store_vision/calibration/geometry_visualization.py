"""无显示会话可用的图像对几何诊断可视化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from store_vision.calibration.pair_geometry import (
    GeometryDiagnosticsConfig,
    PairCorrespondences,
    stable_spatial_sample_indices,
)


def pair_directory_name(image_a: str, image_b: str) -> str:
    """生成稳定、可读且不包含路径分隔符的图像对目录名。"""

    def short(name: str) -> str:
        """截取图片主干名并清理路径分隔符。"""
        safe = Path(name).stem.replace("/", "_").replace("\\", "_")
        return safe[:14]

    return f"{short(image_a)}__{short(image_b)}"


def _read_pair_images(pair: PairCorrespondences, image_root: Path) -> tuple[np.ndarray, np.ndarray]:
    """读取图像对，缺图时明确失败而不生成占位图。"""
    image_a = cv2.imread(str(image_root / pair.image_a.name), cv2.IMREAD_COLOR)
    image_b = cv2.imread(str(image_root / pair.image_b.name), cv2.IMREAD_COLOR)
    if image_a is None:
        raise FileNotFoundError(f"diagnostic image missing or unreadable: {image_root / pair.image_a.name}")
    if image_b is None:
        raise FileNotFoundError(f"diagnostic image missing or unreadable: {image_root / pair.image_b.name}")
    return image_a, image_b


def _side_by_side(image_a: np.ndarray, image_b: np.ndarray) -> tuple[np.ndarray, float, float]:
    """按各自尺度等高拼接，同时返回两个图像到画布的坐标缩放。"""

    height = max(image_a.shape[0], image_b.shape[0])
    scale_a = height / image_a.shape[0]
    scale_b = height / image_b.shape[0]
    resized_a = cv2.resize(image_a, (round(image_a.shape[1] * scale_a), height))
    resized_b = cv2.resize(image_b, (round(image_b.shape[1] * scale_b), height))
    return np.hstack([resized_a, resized_b]), scale_a, scale_b


def _label(canvas: np.ndarray, lines: list[str]) -> None:
    """在画布顶部绘制诊断指标文字栏。"""
    overlay_height = 42 + 34 * len(lines)
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], overlay_height), (20, 20, 20), -1)
    for index, line in enumerate(lines):
        cv2.putText(
            canvas,
            line,
            (24, 42 + index * 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.78,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )


def _write(path: Path, image: np.ndarray) -> Path:
    """保存无损 PNG 并检查 OpenCV 写入结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
        raise OSError(f"could not write diagnostic visualization: {path}")
    return path


def _match_canvas(
    pair: PairCorrespondences,
    image_a: np.ndarray,
    image_b: np.ndarray,
    *,
    verified: bool,
    config: GeometryDiagnosticsConfig,
    metrics: dict[str, Any],
) -> np.ndarray:
    """绘制经固定空间抽样的 raw 或 verified 匹配线。"""
    indices = pair.verified_indices if verified else pair.raw_indices
    points_a, points_b = pair.points(verified=verified)
    canvas, scale_a, scale_b = _side_by_side(image_a, image_b)
    sample = stable_spatial_sample_indices(
        points_a,
        min(config.max_visualized_matches, len(points_a)),
        seed=config.seed,
        grid_cols=config.grid_cols,
        grid_rows=config.grid_rows,
    )
    offset = round(image_a.shape[1] * scale_a)
    rng = np.random.default_rng(config.seed)
    for index in sample:
        color = tuple(int(value) for value in rng.integers(60, 245, size=3))
        point_a = tuple(np.round(points_a[index] * scale_a).astype(int))
        point_b_xy = np.round(points_b[index] * scale_b).astype(int)
        point_b = (int(point_b_xy[0] + offset), int(point_b_xy[1]))
        cv2.line(canvas, point_a, point_b, color, 1, cv2.LINE_AA)
        cv2.circle(canvas, point_a, 3, color, -1, cv2.LINE_AA)
        cv2.circle(canvas, point_b, 3, color, -1, cv2.LINE_AA)
    kind = "verified" if verified else "raw"
    _label(
        canvas,
        [
            f"{kind}: total={len(indices)}, displayed={len(sample)}, seed={config.seed}",
            f"A: {pair.image_a.name}    B: {pair.image_b.name}",
            f"verified ratio={metrics['verified_ratio']:.3f}; min coverage={metrics['spatial_grid']['joint']['min_coverage_ratio']:.3f}",
        ],
    )
    return canvas


def _grid_canvas(
    pair: PairCorrespondences,
    image_a: np.ndarray,
    image_b: np.ndarray,
    config: GeometryDiagnosticsConfig,
    metrics: dict[str, Any],
) -> np.ndarray:
    """绘制验证点、覆盖网格和集中度摘要。"""
    canvas, scale_a, scale_b = _side_by_side(image_a, image_b)
    widths = [round(image_a.shape[1] * scale_a), round(image_b.shape[1] * scale_b)]
    offset = 0
    for width in widths:
        for column in range(1, config.grid_cols):
            x_value = offset + round(width * column / config.grid_cols)
            cv2.line(canvas, (x_value, 0), (x_value, canvas.shape[0]), (255, 170, 0), 2)
        for row in range(1, config.grid_rows):
            y_value = round(canvas.shape[0] * row / config.grid_rows)
            cv2.line(canvas, (offset, y_value), (offset + width, y_value), (255, 170, 0), 2)
        offset += width
    points_a, points_b = pair.points(verified=True)
    for points, scale, x_offset in ((points_a, scale_a, 0), (points_b, scale_b, widths[0])):
        for point in points:
            x_value, y_value = np.round(point * scale).astype(int)
            cv2.circle(canvas, (int(x_value + x_offset), int(y_value)), 3, (0, 40, 255), -1)
    joint = metrics["spatial_grid"]["joint"]
    _label(
        canvas,
        [
            f"verified grid {config.grid_cols}x{config.grid_rows}: {len(points_a)} matches",
            f"coverage A={metrics['spatial_grid']['image_a']['coverage_ratio']:.3f}, B={metrics['spatial_grid']['image_b']['coverage_ratio']:.3f}",
            f"balance={joint['coverage_balance']:.3f}, concentration warning={joint['concentration_warning']}",
        ],
    )
    return canvas


def _displacement_canvas(
    pair: PairCorrespondences,
    image_a: np.ndarray,
    config: GeometryDiagnosticsConfig,
    metrics: dict[str, Any],
) -> np.ndarray:
    """在图 A 上缩放绘制图像空间位移向量。"""
    points_a, points_b = pair.points(verified=True)
    canvas = image_a.copy()
    sample = stable_spatial_sample_indices(
        points_a,
        min(config.max_visualized_matches, len(points_a)),
        seed=config.seed,
        grid_cols=config.grid_cols,
        grid_rows=config.grid_rows,
    )
    # 为了在单张图上可读，将真实位移向量统一缩短；文字明确其显示比例。
    display_scale = 0.20
    for index in sample:
        start = np.round(points_a[index]).astype(int)
        end = np.round(points_a[index] + (points_b[index] - points_a[index]) * display_scale).astype(int)
        cv2.arrowedLine(canvas, tuple(start), tuple(end), (20, 40, 255), 2, cv2.LINE_AA, tipLength=0.18)
    displacement = metrics["image_space_displacement"]
    residual = metrics["homography_compensated_residual"]
    _label(
        canvas,
        [
            f"image-space displacement vectors (display scale={display_scale:.2f}, sampled={len(sample)})",
            f"median={displacement['median'] or 0:.2f}px, p90={displacement['p90'] or 0:.2f}px, near-zero={displacement['near_zero_ratio']:.3f}",
            f"homography residual median={residual.get('median') or 0:.2f}px; not a triangulation-angle measurement",
        ],
    )
    return canvas


def write_pair_visualizations(
    pair: PairCorrespondences,
    image_root: str | Path,
    output_dir: str | Path,
    metrics: dict[str, Any],
    config: GeometryDiagnosticsConfig,
) -> dict[str, str]:
    """为一个图像对输出四类高分辨率可视化，不调用 ``plt.show``。"""

    root = Path(output_dir)
    image_a, image_b = _read_pair_images(pair, Path(image_root))
    outputs = {
        "raw_matches": _write(root / "raw_matches.png", _match_canvas(pair, image_a, image_b, verified=False, config=config, metrics=metrics)),
        "verified_matches": _write(root / "verified_matches.png", _match_canvas(pair, image_a, image_b, verified=True, config=config, metrics=metrics)),
        "verified_matches_grid": _write(root / "verified_matches_grid.png", _grid_canvas(pair, image_a, image_b, config, metrics)),
        "displacement_vectors": _write(root / "displacement_vectors.png", _displacement_canvas(pair, image_a, config, metrics)),
    }
    return {name: str(path) for name, path in outputs.items()}
