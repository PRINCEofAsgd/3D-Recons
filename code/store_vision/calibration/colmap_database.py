"""从本轮 COLMAP SQLite 数据库生成匹配统计和真实匹配可视化。"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import cv2
import numpy as np


COLMAP_MAX_IMAGE_ID = 2**31 - 1


@dataclass(frozen=True)
class MatchPairStatistics:
    """一对图像在原始匹配和几何验证两个阶段的真实计数。"""

    image_a: str
    image_b: str
    raw_matches: int
    verified_matches: int

    @property
    def key(self) -> str:
        return f"{self.image_a} <-> {self.image_b}"


@dataclass
class MatchStatistics:
    """数据库匹配图的完整摘要。"""

    feature_count_by_image: dict[str, int] = field(default_factory=dict)
    pairs: list[MatchPairStatistics] = field(default_factory=list)
    connected_components: list[list[str]] = field(default_factory=list)
    isolated_images: list[str] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        return len(self.connected_components) == 1 if self.feature_count_by_image else False

    @property
    def verified_matches_by_pair(self) -> dict[str, int]:
        return {pair.key: pair.verified_matches for pair in self.pairs}

    @property
    def effective_pairs(self) -> list[MatchPairStatistics]:
        return [pair for pair in self.pairs if pair.verified_matches > 0]


def pair_id_to_image_ids(pair_id: int) -> tuple[int, int]:
    """按 COLMAP 官方数据库约定还原 pair_id 中的两个 image_id。"""

    image_id_b = pair_id % COLMAP_MAX_IMAGE_ID
    image_id_a = (pair_id - image_id_b) // COLMAP_MAX_IMAGE_ID
    return int(image_id_a), int(image_id_b)


def _read_pair_counts(connection: sqlite3.Connection, table: str) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for pair_id, rows in connection.execute(f"SELECT pair_id, rows FROM {table}"):
        counts[pair_id_to_image_ids(int(pair_id))] = int(rows)
    return counts


def _connected_components(names: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """不引入额外图论依赖，直接计算包含孤立图像的连通分量。"""

    adjacency = {name: set() for name in names}
    for image_a, image_b in edges:
        adjacency[image_a].add(image_b)
        adjacency[image_b].add(image_a)
    components: list[list[str]] = []
    unseen = set(names)
    while unseen:
        start = min(unseen)
        stack = [start]
        component: list[str] = []
        unseen.remove(start)
        while stack:
            current = stack.pop()
            component.append(current)
            neighbours = sorted(adjacency[current] & unseen, reverse=True)
            for neighbour in neighbours:
                unseen.remove(neighbour)
                stack.append(neighbour)
        components.append(sorted(component))
    return sorted(components, key=lambda item: (-len(item), item))


def read_match_statistics(database_path: str | Path) -> MatchStatistics:
    """只读本轮数据库，统计所有图像及所有组合（包括零匹配对）。"""

    database = Path(database_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"COLMAP database not found: {database}")
    # mode=ro 防止诊断阶段意外修改实验数据库。
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        image_rows = connection.execute(
            "SELECT images.image_id, images.name, COALESCE(keypoints.rows, 0) "
            "FROM images LEFT JOIN keypoints USING(image_id) ORDER BY images.name"
        ).fetchall()
        id_to_name = {int(image_id): str(name) for image_id, name, _ in image_rows}
        feature_counts = {str(name): int(rows) for _, name, rows in image_rows}
        raw_counts = _read_pair_counts(connection, "matches")
        verified_counts = _read_pair_counts(connection, "two_view_geometries")
    finally:
        connection.close()

    pairs: list[MatchPairStatistics] = []
    for image_id_a, image_id_b in combinations(sorted(id_to_name), 2):
        key = (image_id_a, image_id_b)
        pairs.append(
            MatchPairStatistics(
                image_a=id_to_name[image_id_a],
                image_b=id_to_name[image_id_b],
                raw_matches=raw_counts.get(key, 0),
                verified_matches=verified_counts.get(key, 0),
            )
        )
    edges = [(pair.image_a, pair.image_b) for pair in pairs if pair.verified_matches > 0]
    components = _connected_components(sorted(feature_counts), edges)
    connected_names = {name for edge in edges for name in edge}
    return MatchStatistics(
        feature_count_by_image=feature_counts,
        pairs=pairs,
        connected_components=components,
        isolated_images=sorted(set(feature_counts) - connected_names),
    )


def _statistics_payload(statistics: MatchStatistics) -> dict[str, Any]:
    return {
        "feature_count_by_image": statistics.feature_count_by_image,
        "pairs": [
            {
                "image_a": pair.image_a,
                "image_b": pair.image_b,
                "raw_matches": pair.raw_matches,
                "verified_matches": pair.verified_matches,
            }
            for pair in statistics.pairs
        ],
        "effective_match_relationships": [pair.key for pair in statistics.effective_pairs],
        "isolated_images": statistics.isolated_images,
        "connected_components": statistics.connected_components,
        "connected_component_count": len(statistics.connected_components),
        "is_connected": statistics.is_connected,
    }


def write_match_statistics(
    statistics: MatchStatistics,
    json_path: str | Path,
    csv_path: str | Path,
) -> tuple[Path, Path]:
    """同时输出适合程序读取的 JSON 和逐图像对 CSV。"""

    json_output = Path(json_path)
    csv_output = Path(csv_path)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(_statistics_payload(statistics), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with csv_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("image_a", "image_b", "raw_matches", "verified_matches"),
        )
        writer.writeheader()
        for pair in statistics.pairs:
            writer.writerow(
                {
                    "image_a": pair.image_a,
                    "image_b": pair.image_b,
                    "raw_matches": pair.raw_matches,
                    "verified_matches": pair.verified_matches,
                }
            )
    return json_output, csv_output


def draw_camera_match_graph(statistics: MatchStatistics, output_path: str | Path) -> Path:
    """绘制以几何验证匹配数为边权的相机匹配图。"""

    # 延迟导入避免 calibration-demo dry-run 触发字体缓存和 GUI 后端初始化。
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    names = sorted(statistics.feature_count_by_image)
    count = max(len(names), 1)
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    positions = {name: (float(np.cos(angle)), float(np.sin(angle))) for name, angle in zip(names, angles)}
    figure, axis = plt.subplots(figsize=(12, 10))
    max_matches = max((pair.verified_matches for pair in statistics.effective_pairs), default=1)
    for pair in statistics.effective_pairs:
        x_values = [positions[pair.image_a][0], positions[pair.image_b][0]]
        y_values = [positions[pair.image_a][1], positions[pair.image_b][1]]
        width = 0.8 + 4.2 * pair.verified_matches / max_matches
        axis.plot(x_values, y_values, color="#4c78a8", linewidth=width, alpha=0.65, zorder=1)
        middle_x = sum(x_values) / 2
        middle_y = sum(y_values) / 2
        axis.text(middle_x, middle_y, str(pair.verified_matches), fontsize=8, color="#1f3b5b")
    for name, (x_value, y_value) in positions.items():
        isolated = name in statistics.isolated_images
        axis.scatter(
            [x_value],
            [y_value],
            s=900,
            color="#e45756" if isolated else "#72b7b2",
            edgecolors="black",
            zorder=2,
        )
        axis.text(x_value, y_value - 0.14, name, ha="center", va="top", fontsize=8)
    axis.set_title(
        f"COLMAP verified match graph — components={len(statistics.connected_components)}, "
        f"connected={statistics.is_connected}"
    )
    axis.set_aspect("equal")
    axis.set_xlim(-1.45, 1.45)
    axis.set_ylim(-1.45, 1.45)
    axis.axis("off")
    figure.tight_layout()
    figure.savefig(output, dpi=160)
    plt.close(figure)
    return output


def _pair_blob(
    connection: sqlite3.Connection,
    table: str,
    image_id_a: int,
    image_id_b: int,
) -> np.ndarray:
    pair_id = COLMAP_MAX_IMAGE_ID * min(image_id_a, image_id_b) + max(image_id_a, image_id_b)
    row = connection.execute(
        f"SELECT rows, cols, data FROM {table} WHERE pair_id = ?", (pair_id,)
    ).fetchone()
    if row is None or int(row[0]) == 0 or row[2] is None:
        return np.empty((0, 2), dtype=np.uint32)
    return np.frombuffer(row[2], dtype=np.uint32).reshape(int(row[0]), int(row[1]))[:, :2]


def _keypoints(connection: sqlite3.Connection, image_id: int) -> list[cv2.KeyPoint]:
    row = connection.execute(
        "SELECT rows, cols, data FROM keypoints WHERE image_id = ?", (image_id,)
    ).fetchone()
    if row is None or row[2] is None:
        return []
    values = np.frombuffer(row[2], dtype=np.float32).reshape(int(row[0]), int(row[1]))
    return [cv2.KeyPoint(float(item[0]), float(item[1]), 3.0) for item in values]


def _safe_match_name(image_a: str, image_b: str) -> str:
    safe_a = image_a.replace("/", "_").replace("\\", "_")
    safe_b = image_b.replace("/", "_").replace("\\", "_")
    return f"{safe_a}__{safe_b}.png"


def draw_top_verified_matches(
    database_path: str | Path,
    image_root: str | Path,
    statistics: MatchStatistics,
    output_dir: str | Path,
    *,
    pair_count: int = 3,
    displayed_match_limit: int = 200,
) -> list[Path]:
    """选择几何验证数最高的图像对，读取数据库索引绘制真实内点。"""

    selected = sorted(
        statistics.effective_pairs,
        key=lambda pair: (-pair.verified_matches, pair.image_a, pair.image_b),
    )[:pair_count]
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{Path(database_path).resolve()}?mode=ro", uri=True)
    outputs: list[Path] = []
    try:
        image_ids = {
            str(name): int(image_id)
            for image_id, name in connection.execute("SELECT image_id, name FROM images")
        }
        for pair in selected:
            image_id_a = image_ids[pair.image_a]
            image_id_b = image_ids[pair.image_b]
            matches = _pair_blob(connection, "two_view_geometries", image_id_a, image_id_b)
            keypoints_a = _keypoints(connection, image_id_a)
            keypoints_b = _keypoints(connection, image_id_b)
            if len(matches) > displayed_match_limit:
                indices = np.linspace(0, len(matches) - 1, displayed_match_limit, dtype=int)
                matches_to_draw = matches[indices]
            else:
                matches_to_draw = matches
            draw_matches = [
                cv2.DMatch(_queryIdx=int(item[0]), _trainIdx=int(item[1]), _distance=0.0)
                for item in matches_to_draw
            ]
            image_a = cv2.imread(str(Path(image_root) / pair.image_a), cv2.IMREAD_COLOR)
            image_b = cv2.imread(str(Path(image_root) / pair.image_b), cv2.IMREAD_COLOR)
            if image_a is None or image_b is None:
                raise FileNotFoundError(f"Could not read match visualization images: {pair.key}")
            canvas = cv2.drawMatches(
                image_a,
                keypoints_a,
                image_b,
                keypoints_b,
                draw_matches,
                None,
                matchColor=(0, 220, 0),
                flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
            )
            title = (
                f"verified={pair.verified_matches}, displayed={len(draw_matches)}, "
                f"raw={pair.raw_matches}"
            )
            cv2.putText(canvas, title, (30, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            if canvas.shape[1] > 2400:
                scale = 2400 / canvas.shape[1]
                canvas = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            output = output_root / _safe_match_name(pair.image_a, pair.image_b)
            if not cv2.imwrite(str(output), canvas):
                raise OSError(f"Could not write match visualization: {output}")
            outputs.append(output)
    finally:
        connection.close()
    return outputs
