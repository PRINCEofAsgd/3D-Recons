"""把 2.5D GeoJSON 渲染成带平面图上下文的静态试算预览。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure


def _feature_ring(feature: dict[str, Any]) -> np.ndarray:
    """读取 GeoJSON Polygon 首个环，并去除重复闭合点。"""

    coordinates = feature.get("geometry", {}).get("coordinates", [])
    if not coordinates:
        return np.empty((0, 2), dtype=np.float64)
    ring = np.asarray(coordinates[0], dtype=np.float64)
    if ring.ndim != 2 or ring.shape[1] < 2:
        return np.empty((0, 2), dtype=np.float64)
    ring = ring[:, :2]
    if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    return ring


def render_map25d_preview(
    geojson: dict[str, Any],
    floor_plan_path: str | Path,
    output_path: str | Path,
) -> Path:
    """输出“平面定位 + 固定高度挤出”的双视图，明确它不是稠密重建。"""

    floor = cv2.imread(str(floor_plan_path))
    if floor is None:
        raise FileNotFoundError(f"无法读取平面图：{floor_plan_path}")
    floor_rgb = cv2.cvtColor(floor, cv2.COLOR_BGR2RGB)
    metadata = geojson.get("metadata", {})
    plan_width = float(metadata.get("plan_width_cm", 2000.0))
    plan_height = float(metadata.get("plan_height_cm", 1200.0))
    features = list(geojson.get("features", []))

    figure = Figure(figsize=(15, 7), constrained_layout=True)
    FigureCanvasAgg(figure)
    plan_ax = figure.add_subplot(1, 2, 1)
    map_ax = figure.add_subplot(1, 2, 2, projection="3d")
    palette = [
        "#1f77b4",
        "#ff7f0e",
        "#2ca02c",
        "#d62728",
        "#9467bd",
        "#8c564b",
        "#e377c2",
        "#17becf",
    ]

    # 平面图使用左上角为原点，与 cali/GeoJSON 的 XY 方向保持一致。
    plan_ax.imshow(
        floor_rgb,
        extent=(0.0, plan_width, plan_height, 0.0),
        alpha=0.82,
    )
    for index, feature in enumerate(features):
        ring = _feature_ring(feature)
        if len(ring) < 3:
            continue
        color = palette[index % len(palette)]
        closed = np.vstack([ring, ring[0]])
        plan_ax.fill(ring[:, 0], ring[:, 1], color=color, alpha=0.32)
        plan_ax.plot(closed[:, 0], closed[:, 1], color=color, linewidth=2.0)
        centre = np.mean(ring, axis=0)
        plan_ax.text(
            centre[0],
            centre[1],
            str(feature.get("id", f"table_{index:03d}")),
            fontsize=8,
            ha="center",
            va="center",
            color="black",
        )
    plan_ax.set_xlim(0.0, plan_width)
    plan_ax.set_ylim(plan_height, 0.0)
    plan_ax.set_aspect("equal", adjustable="box")
    plan_ax.set_xlabel("floor X (cm)")
    plan_ax.set_ylabel("floor Y (cm)")
    plan_ax.set_title(f"Floor-plan table trial ({len(features)} candidates)")

    # 右侧只挤出固定业务高度；这不是由 SfM 点云恢复的桌面表面。
    stride_y = max(1, floor_rgb.shape[0] // 80)
    stride_x = max(1, floor_rgb.shape[1] // 120)
    texture = floor_rgb[::stride_y, ::stride_x] / 255.0
    grid_x = np.linspace(0.0, plan_width, texture.shape[1])
    grid_y = np.linspace(0.0, plan_height, texture.shape[0])
    xx, yy = np.meshgrid(grid_x, grid_y)
    map_ax.plot_surface(
        xx,
        yy,
        np.zeros_like(xx),
        facecolors=texture,
        shade=False,
        alpha=0.55,
        linewidth=0,
    )
    maximum_height = 1.0
    for index, feature in enumerate(features):
        ring = _feature_ring(feature)
        if len(ring) < 3:
            continue
        height = float(feature.get("properties", {}).get("height_cm", 70.0))
        maximum_height = max(maximum_height, height)
        color = palette[index % len(palette)]
        closed = np.vstack([ring, ring[0]])
        map_ax.plot(
            closed[:, 0],
            closed[:, 1],
            np.zeros(len(closed)),
            color=color,
            linewidth=1.0,
        )
        map_ax.plot(
            closed[:, 0],
            closed[:, 1],
            np.full(len(closed), height),
            color=color,
            linewidth=2.0,
        )
        for point in ring:
            map_ax.plot(
                [point[0], point[0]],
                [point[1], point[1]],
                [0.0, height],
                color=color,
                linewidth=1.0,
            )
    map_ax.set_xlim(0.0, plan_width)
    map_ax.set_ylim(plan_height, 0.0)
    map_ax.set_zlim(0.0, maximum_height * 1.5)
    map_ax.set_box_aspect((plan_width, plan_height, maximum_height * 6.0))
    map_ax.set_xlabel("X (cm)")
    map_ax.set_ylabel("Y (cm)")
    map_ax.set_zlabel("fixed height (cm)")
    map_ax.set_title("2.5D extrusion (not dense 3D)")
    map_ax.view_init(elev=36, azim=-62)

    trust = metadata.get("trust_level", "trial")
    route = metadata.get("position_source", "manual floor homography")
    sfm_status = metadata.get("sfm_status", "not available")
    figure.suptitle(
        f"Store 2.5D preview | trust={trust} | SfM={sfm_status}\n"
        f"XY source: {route}; Z source: fixed business height",
        fontsize=11,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=150)
    return output
