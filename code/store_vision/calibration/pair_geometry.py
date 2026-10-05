"""COLMAP 图像对数据库读取与可复现几何诊断。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from store_vision.calibration.colmap_database import COLMAP_MAX_IMAGE_ID


@dataclass(frozen=True)
class GeometryDiagnosticsConfig:
    """集中保存几何诊断阈值、评分权重和复现参数。"""

    grid_cols: int = 8
    grid_rows: int = 5
    near_zero_px: float = 10.0
    homography_ransac_px: float = 4.0
    fundamental_ransac_px: float = 2.0
    ransac_confidence: float = 0.999
    seed: int = 0
    max_visualized_matches: int = 200
    low_coverage_ratio: float = 0.25
    high_cell_ratio: float = 0.35
    coverage_imbalance: float = 0.45
    homography_dominance_margin: float = 0.08
    homography_dominance_ratio: float = 0.70
    ranking_weights: dict[str, float] = field(
        default_factory=lambda: {
            "verified_count": 0.22,
            "verified_ratio": 0.14,
            "spatial_coverage": 0.20,
            "coverage_balance": 0.12,
            "displacement": 0.12,
            "geometry_success": 0.10,
            "homography_penalty": 0.06,
            "concentration_penalty": 0.04,
        }
    )

    def __post_init__(self) -> None:
        """拒绝无效网格和可视化数量，避免运行中产生隐式行为。"""
        if self.grid_cols < 1 or self.grid_rows < 1:
            raise ValueError("grid dimensions must be positive")
        if self.max_visualized_matches < 1:
            raise ValueError("max_visualized_matches must be positive")


@dataclass(frozen=True)
class DatabaseImage:
    image_id: int
    camera_id: int | None
    name: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class PairCorrespondences:
    """一对图像的真实索引与像素坐标；字段顺序始终跟随调用者。"""

    image_a: DatabaseImage
    image_b: DatabaseImage
    raw_indices: np.ndarray
    verified_indices: np.ndarray
    keypoints_a: np.ndarray
    keypoints_b: np.ndarray

    def points(self, verified: bool = True) -> tuple[np.ndarray, np.ndarray]:
        indices = self.verified_indices if verified else self.raw_indices
        if indices.size == 0:
            empty = np.empty((0, 2), dtype=np.float64)
            return empty, empty.copy()
        if int(np.max(indices[:, 0])) >= len(self.keypoints_a):
            raise ValueError(f"match index exceeds keypoints for {self.image_a.name}")
        if int(np.max(indices[:, 1])) >= len(self.keypoints_b):
            raise ValueError(f"match index exceeds keypoints for {self.image_b.name}")
        return (
            self.keypoints_a[indices[:, 0], :2].astype(np.float64),
            self.keypoints_b[indices[:, 1], :2].astype(np.float64),
        )


def image_ids_to_pair_id(image_id_a: int, image_id_b: int) -> int:
    """按 COLMAP 官方约定编码无序 image pair。"""

    if image_id_a == image_id_b:
        raise ValueError("COLMAP image pair requires two different image ids")
    if min(image_id_a, image_id_b) <= 0 or max(image_id_a, image_id_b) >= COLMAP_MAX_IMAGE_ID:
        raise ValueError("COLMAP image id is outside the supported range")
    return min(image_id_a, image_id_b) * COLMAP_MAX_IMAGE_ID + max(image_id_a, image_id_b)


def pair_id_to_image_ids(pair_id: int) -> tuple[int, int]:
    """解码 COLMAP pair_id，并拒绝无效的自配对或越界值。"""

    if pair_id <= 0:
        raise ValueError("COLMAP pair_id must be positive")
    image_id_b = pair_id % COLMAP_MAX_IMAGE_ID
    image_id_a = (pair_id - image_id_b) // COLMAP_MAX_IMAGE_ID
    if image_id_a <= 0 or image_id_b <= image_id_a or image_id_b >= COLMAP_MAX_IMAGE_ID:
        raise ValueError(f"invalid COLMAP pair_id: {pair_id}")
    return int(image_id_a), int(image_id_b)


def parse_blob(
    blob: bytes | None,
    rows: int,
    cols: int,
    dtype: np.dtype[Any] | type[np.generic],
    *,
    label: str,
) -> np.ndarray:
    """严格解析 COLMAP 数组 BLOB，空行返回形状稳定的空数组。"""

    if rows < 0 or cols < 1:
        raise ValueError(f"invalid {label} shape: ({rows}, {cols})")
    np_dtype = np.dtype(dtype)
    if rows == 0:
        if blob not in (None, b""):
            raise ValueError(f"{label} has data but zero rows")
        return np.empty((0, cols), dtype=np_dtype)
    if blob is None:
        raise ValueError(f"{label} blob is missing for {rows} rows")
    expected = rows * cols * np_dtype.itemsize
    if len(blob) != expected:
        raise ValueError(f"{label} blob has {len(blob)} bytes; expected {expected}")
    return np.frombuffer(blob, dtype=np_dtype).reshape(rows, cols).copy()


class ColmapPairDatabase:
    """只读访问 COLMAP SQLite，统一处理表缺失、方向和空图像对。"""

    REQUIRED_TABLES = {"images", "cameras", "keypoints", "matches", "two_view_geometries"}

    def __init__(self, database_path: str | Path):
        """以只读 URI 打开数据库并立即验证所需表。"""
        self.path = Path(database_path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"COLMAP database not found: {self.path}")
        self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(self.REQUIRED_TABLES - tables)
        if missing:
            self.close()
            raise sqlite3.DatabaseError(f"COLMAP database missing tables: {', '.join(missing)}")
        self._images = self._read_images()

    def __enter__(self) -> "ColmapPairDatabase":
        """进入只读数据库上下文。"""
        return self

    def __exit__(self, *_: object) -> None:
        """退出上下文时关闭数据库连接。"""
        self.close()

    def close(self) -> None:
        """幂等关闭 SQLite 连接。"""
        if self.connection is not None:
            self.connection.close()
            self.connection = None  # type: ignore[assignment]

    def _read_images(self) -> dict[str, DatabaseImage]:
        """联合 cameras 表读取图片标识和真实分辨率。"""
        rows = self.connection.execute(
            "SELECT i.image_id, i.camera_id, i.name, c.width, c.height "
            "FROM images i LEFT JOIN cameras c ON c.camera_id=i.camera_id ORDER BY i.name"
        ).fetchall()
        return {
            str(row["name"]): DatabaseImage(
                int(row["image_id"]),
                int(row["camera_id"]) if row["camera_id"] is not None else None,
                str(row["name"]),
                int(row["width"]) if row["width"] is not None else None,
                int(row["height"]) if row["height"] is not None else None,
            )
            for row in rows
        }

    @property
    def images(self) -> dict[str, DatabaseImage]:
        """返回隔离的图片名称到数据库记录映射。"""
        return dict(self._images)

    def image(self, name: str) -> DatabaseImage:
        """按名称获取图片，不存在时返回可操作错误。"""
        try:
            return self._images[name]
        except KeyError as exc:
            raise KeyError(f"image is not present in COLMAP database: {name}") from exc

    def keypoints(self, image: str | DatabaseImage) -> np.ndarray:
        """解析指定图片的 float32 关键点数组。"""
        record = self.image(image) if isinstance(image, str) else image
        row = self.connection.execute(
            "SELECT rows, cols, data FROM keypoints WHERE image_id=?", (record.image_id,)
        ).fetchone()
        if row is None:
            return np.empty((0, 2), dtype=np.float32)
        return parse_blob(row["data"], int(row["rows"]), int(row["cols"]), np.float32, label="keypoints")

    def pair_indices(self, image_a: str, image_b: str, *, verified: bool) -> np.ndarray:
        """读取原始或几何验证匹配，并纠正反向请求的索引列。"""
        record_a, record_b = self.image(image_a), self.image(image_b)
        table = "two_view_geometries" if verified else "matches"
        pair_id = image_ids_to_pair_id(record_a.image_id, record_b.image_id)
        row = self.connection.execute(
            f"SELECT rows, cols, data FROM {table} WHERE pair_id=?", (pair_id,)
        ).fetchone()
        if row is None:
            return np.empty((0, 2), dtype=np.uint32)
        indices = parse_blob(
            row["data"], int(row["rows"]), int(row["cols"]), np.uint32, label=table
        )[:, :2]
        # 数据库存储顺序按较小 image_id；调用者反向请求时交换索引列。
        if record_a.image_id > record_b.image_id:
            indices = indices[:, ::-1].copy()
        return indices

    def pair(self, image_a: str, image_b: str) -> PairCorrespondences:
        """组装一个图像对的关键点与两阶段匹配。"""
        if image_a == image_b:
            raise ValueError("pair requires two different image names")
        record_a, record_b = self.image(image_a), self.image(image_b)
        return PairCorrespondences(
            record_a,
            record_b,
            self.pair_indices(image_a, image_b, verified=False),
            self.pair_indices(image_a, image_b, verified=True),
            self.keypoints(record_a),
            self.keypoints(record_b),
        )

    def all_pairs(self) -> Iterable[PairCorrespondences]:
        """按名称稳定顺序遍历数据库中的全部无序图像对。"""
        for image_a, image_b in combinations(sorted(self._images), 2):
            yield self.pair(image_a, image_b)


def spatial_coverage(
    points: np.ndarray,
    width: int,
    height: int,
    config: GeometryDiagnosticsConfig,
) -> dict[str, Any]:
    """统计匹配点的网格覆盖、中心/边界和四个半区分布。"""

    total_cells = config.grid_cols * config.grid_rows
    if width <= 0 or height <= 0:
        raise ValueError("image width and height must be positive")
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(points) == 0:
        return {
            "count": 0,
            "occupied_cells": 0,
            "total_cells": total_cells,
            "coverage_ratio": 0.0,
            "max_cell_count": 0,
            "max_cell_ratio": 0.0,
            "center_region_ratio": 0.0,
            "border_region_ratio": 0.0,
            "left_half_ratio": 0.0,
            "right_half_ratio": 0.0,
            "top_half_ratio": 0.0,
            "bottom_half_ratio": 0.0,
            "grid_counts": [[0] * config.grid_cols for _ in range(config.grid_rows)],
        }
    xs = np.clip(points[:, 0], 0.0, np.nextafter(float(width), 0.0))
    ys = np.clip(points[:, 1], 0.0, np.nextafter(float(height), 0.0))
    cols = np.floor(xs / width * config.grid_cols).astype(int)
    rows = np.floor(ys / height * config.grid_rows).astype(int)
    counts = np.zeros((config.grid_rows, config.grid_cols), dtype=np.int64)
    np.add.at(counts, (rows, cols), 1)
    border = (cols == 0) | (cols == config.grid_cols - 1) | (rows == 0) | (rows == config.grid_rows - 1)
    center = (xs >= width * 0.25) & (xs < width * 0.75) & (ys >= height * 0.25) & (ys < height * 0.75)
    count = len(points)
    return {
        "count": count,
        "occupied_cells": int(np.count_nonzero(counts)),
        "total_cells": total_cells,
        "coverage_ratio": float(np.count_nonzero(counts) / total_cells),
        "max_cell_count": int(counts.max(initial=0)),
        "max_cell_ratio": float(counts.max(initial=0) / count),
        "center_region_ratio": float(np.mean(center)),
        "border_region_ratio": float(np.mean(border)),
        "left_half_ratio": float(np.mean(xs < width / 2)),
        "right_half_ratio": float(np.mean(xs >= width / 2)),
        "top_half_ratio": float(np.mean(ys < height / 2)),
        "bottom_half_ratio": float(np.mean(ys >= height / 2)),
        "grid_counts": counts.tolist(),
    }


def combined_coverage(
    coverage_a: dict[str, Any],
    coverage_b: dict[str, Any],
    config: GeometryDiagnosticsConfig,
) -> dict[str, Any]:
    ratios = [float(coverage_a["coverage_ratio"]), float(coverage_b["coverage_ratio"])]
    maximum = max(ratios)
    balance = min(ratios) / maximum if maximum > 0 else 0.0
    reasons: list[str] = []
    if min(ratios) < config.low_coverage_ratio:
        reasons.append("low_grid_coverage")
    if max(float(coverage_a["max_cell_ratio"]), float(coverage_b["max_cell_ratio"])) > config.high_cell_ratio:
        reasons.append("high_single_cell_concentration")
    if balance < config.coverage_imbalance:
        reasons.append("unbalanced_image_coverage")
    return {
        "min_coverage_ratio": min(ratios),
        "mean_coverage_ratio": float(np.mean(ratios)),
        "coverage_balance": balance,
        "concentration_warning": bool(reasons),
        "concentration_reasons": reasons,
    }


def _distribution(values: np.ndarray) -> dict[str, Any]:
    """生成统一的描述统计字段，空数组以空值降级。"""
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    if len(values) == 0:
        return {name: None for name in ("min", "max", "mean", "median", "std", "p10", "p25", "p50", "p75", "p90", "p95")}
    percentiles = np.percentile(values, [10, 25, 50, 75, 90, 95])
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "std": float(values.std()),
        **{f"p{label}": float(value) for label, value in zip((10, 25, 50, 75, 90, 95), percentiles)},
    }


def displacement_statistics(
    points_a: np.ndarray,
    points_b: np.ndarray,
    config: GeometryDiagnosticsConfig,
) -> dict[str, Any]:
    """计算图像空间位移代理；不将像素位移解释为三角化角。"""

    points_a = np.asarray(points_a, dtype=np.float64).reshape(-1, 2)
    points_b = np.asarray(points_b, dtype=np.float64).reshape(-1, 2)
    if len(points_a) != len(points_b):
        raise ValueError("paired point arrays must have equal lengths")
    vectors = points_b - points_a
    values = np.linalg.norm(vectors, axis=1)
    if len(values):
        angles = (np.arctan2(vectors[:, 1], vectors[:, 0]) + 2 * np.pi) % (2 * np.pi)
        histogram, _ = np.histogram(angles, bins=np.linspace(0, 2 * np.pi, 9))
    else:
        histogram = np.zeros(8, dtype=int)
    return {
        "metric": "image-space displacement proxy",
        "count": len(values),
        **_distribution(values),
        "near_zero_threshold_px": config.near_zero_px,
        "near_zero_ratio": float(np.mean(values <= config.near_zero_px)) if len(values) else 0.0,
        "direction_histogram": histogram.tolist(),
        "direction_bins_degrees": [0, 45, 90, 135, 180, 225, 270, 315, 360],
        "limitation": "Pixel displacement is not a triangulation angle and may include rotation, scale and perspective effects.",
    }


def _model_result(matrix: np.ndarray | None, mask: np.ndarray | None, count: int, reason: str | None = None) -> dict[str, Any]:
    """将 OpenCV 模型和内点掩码规范化为 JSON 结构。"""
    success = matrix is not None and mask is not None
    inliers = int(np.count_nonzero(mask)) if success else 0
    return {
        "success": success,
        "input_points": count,
        "inliers": inliers,
        "inlier_ratio": float(inliers / count) if count else 0.0,
        "matrix": np.asarray(matrix).tolist() if matrix is not None else [],
        "failure_reason": None if success else reason or "robust estimation returned no model",
    }


def estimate_geometry_models(
    points_a: np.ndarray,
    points_b: np.ndarray,
    config: GeometryDiagnosticsConfig,
) -> dict[str, Any]:
    """让 H 与 F 在同一批真实对应点上竞争，并给出保守退化提示。"""

    points_a = np.asarray(points_a, dtype=np.float64).reshape(-1, 2)
    points_b = np.asarray(points_b, dtype=np.float64).reshape(-1, 2)
    if len(points_a) != len(points_b):
        raise ValueError("paired point arrays must have equal lengths")
    count = len(points_a)
    cv2.setRNGSeed(config.seed)
    if count >= 4:
        try:
            homography, h_mask = cv2.findHomography(
                points_a, points_b, cv2.RANSAC, config.homography_ransac_px,
                maxIters=5000, confidence=config.ransac_confidence,
            )
            h_result = _model_result(homography, h_mask, count)
        except cv2.error as exc:
            h_result = _model_result(None, None, count, f"OpenCV homography estimation failed: {exc}")
    else:
        h_result = _model_result(None, None, count, "at least 4 correspondences are required")
    cv2.setRNGSeed(config.seed)
    if count >= 8:
        try:
            fundamental, f_mask = cv2.findFundamentalMat(
                points_a, points_b, cv2.FM_RANSAC,
                config.fundamental_ransac_px, config.ransac_confidence, 5000,
            )
            if fundamental is not None and fundamental.shape != (3, 3):
                fundamental = fundamental[:3, :3]
            f_result = _model_result(fundamental, f_mask, count)
        except cv2.error as exc:
            f_result = _model_result(None, None, count, f"OpenCV fundamental estimation failed: {exc}")
    else:
        f_result = _model_result(None, None, count, "at least 8 correspondences are required")
    h_ratio, f_ratio = h_result["inlier_ratio"], f_result["inlier_ratio"]
    dominance = bool(
        h_result["success"]
        and h_ratio >= config.homography_dominance_ratio
        and h_ratio - f_ratio >= config.homography_dominance_margin
    )
    evidence: list[str] = []
    if dominance:
        evidence.append("A single homography explains a high fraction of correspondences and exceeds the F inlier ratio.")
    if h_result["success"] and not f_result["success"]:
        evidence.append("Homography estimation succeeded while fundamental estimation failed.")
    if not evidence:
        evidence.append("The tested correspondences do not provide strong homography-dominance evidence under configured thresholds.")
    confidence = "medium" if dominance and count >= 50 else "low"
    return {
        "input_correspondence_set": "verified_matches",
        "homography": h_result,
        "fundamental": f_result,
        "diagnosis": {
            "h_to_f_inlier_ratio": float(h_ratio / f_ratio) if f_ratio > 0 else None,
            "h_minus_f_inliers": h_result["inliers"] - f_result["inliers"],
            "homography_dominant": dominance,
            "possible_planar_degeneracy": dominance,
            "possible_pure_rotation": dominance,
            "confidence": confidence,
            "evidence": evidence,
            "limitation": "Without trustworthy intrinsics this is a conservative cue, not proof of a planar scene or pure rotation.",
        },
    }


def residual_displacement(
    points_a: np.ndarray,
    points_b: np.ndarray,
    homography: list[list[float]] | np.ndarray,
    config: GeometryDiagnosticsConfig,
) -> dict[str, Any]:
    matrix = np.asarray(homography, dtype=np.float64)
    if matrix.shape != (3, 3) or len(points_a) == 0:
        return {"metric": "homography-compensated residual displacement", "count": 0, **_distribution(np.array([])), "failure_reason": "homography unavailable"}
    projected = cv2.perspectiveTransform(np.asarray(points_a, np.float64).reshape(-1, 1, 2), matrix).reshape(-1, 2)
    values = np.linalg.norm(projected - np.asarray(points_b, np.float64), axis=1)
    return {
        "metric": "homography-compensated residual displacement",
        "count": len(values),
        **_distribution(values),
        "near_zero_threshold_px": config.near_zero_px,
        "near_zero_ratio": float(np.mean(values <= config.near_zero_px)),
        "failure_reason": None,
    }


def diagnose_pair(pair: PairCorrespondences, config: GeometryDiagnosticsConfig) -> dict[str, Any]:
    points_a, points_b = pair.points(verified=True)
    width_a, height_a = pair.image_a.width or 0, pair.image_a.height or 0
    width_b, height_b = pair.image_b.width or 0, pair.image_b.height or 0
    coverage_a = spatial_coverage(points_a, width_a, height_a, config)
    coverage_b = spatial_coverage(points_b, width_b, height_b, config)
    joint = combined_coverage(coverage_a, coverage_b, config)
    geometry = estimate_geometry_models(points_a, points_b, config)
    displacement = displacement_statistics(points_a, points_b, config)
    residual = residual_displacement(points_a, points_b, geometry["homography"]["matrix"], config)
    return {
        "image_a": pair.image_a.name,
        "image_b": pair.image_b.name,
        "raw_matches": len(pair.raw_indices),
        "verified_matches": len(pair.verified_indices),
        "verified_ratio": float(len(pair.verified_indices) / len(pair.raw_indices)) if len(pair.raw_indices) else 0.0,
        "spatial_grid": {"image_a": coverage_a, "image_b": coverage_b, "joint": joint},
        "image_space_displacement": displacement,
        "homography_compensated_residual": residual,
        "geometry_models": geometry,
    }


def stable_spatial_sample_indices(
    points: np.ndarray,
    limit: int,
    *,
    seed: int,
    grid_cols: int = 8,
    grid_rows: int = 5,
) -> np.ndarray:
    """固定种子、按空间单元轮询抽样，避免只展示数据库前 N 条。"""

    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(points) <= limit:
        return np.arange(len(points), dtype=int)
    minimum = points.min(axis=0)
    span = np.maximum(points.max(axis=0) - minimum, 1.0)
    cells_x = np.minimum(((points[:, 0] - minimum[0]) / span[0] * grid_cols).astype(int), grid_cols - 1)
    cells_y = np.minimum(((points[:, 1] - minimum[1]) / span[1] * grid_rows).astype(int), grid_rows - 1)
    rng = np.random.default_rng(seed)
    buckets: dict[int, list[int]] = {}
    for index, cell in enumerate((cells_y * grid_cols + cells_x).tolist()):
        buckets.setdefault(cell, []).append(index)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    selected: list[int] = []
    while len(selected) < limit:
        progressed = False
        for cell in sorted(buckets):
            if buckets[cell] and len(selected) < limit:
                selected.append(buckets[cell].pop())
                progressed = True
        if not progressed:
            break
    return np.asarray(selected, dtype=int)


def rank_initial_pairs(pair_diagnostics: list[dict[str, Any]], config: GeometryDiagnosticsConfig) -> list[dict[str, Any]]:
    """生成项目自定义初始化候选分数；它不是 COLMAP 官方评分。"""

    if not pair_diagnostics:
        return []
    max_verified = max(int(item["verified_matches"]) for item in pair_diagnostics) or 1
    medians = [float(item["image_space_displacement"]["median"] or 0.0) for item in pair_diagnostics]
    displacement_scale = float(np.percentile(medians, 90)) or 1.0
    rows: list[dict[str, Any]] = []
    for item in pair_diagnostics:
        joint = item["spatial_grid"]["joint"]
        geometry = item["geometry_models"]
        h_ratio = float(geometry["homography"]["inlier_ratio"])
        f_ratio = float(geometry["fundamental"]["inlier_ratio"])
        concentration = bool(joint["concentration_warning"])
        breakdown = {
            "verified_count": int(item["verified_matches"]) / max_verified,
            "verified_ratio": float(item["verified_ratio"]),
            "spatial_coverage": float(joint["min_coverage_ratio"]),
            "coverage_balance": float(joint["coverage_balance"]),
            "displacement": min(float(item["image_space_displacement"]["median"] or 0.0) / displacement_scale, 1.0),
            "geometry_success": (int(geometry["homography"]["success"]) + int(geometry["fundamental"]["success"])) / 2,
            "homography_penalty": max(0.0, h_ratio - f_ratio),
            "concentration_penalty": 1.0 if concentration else 0.0,
        }
        positive = sum(
            breakdown[key] * config.ranking_weights[key]
            for key in ("verified_count", "verified_ratio", "spatial_coverage", "coverage_balance", "displacement", "geometry_success")
        )
        penalties = sum(
            breakdown[key] * config.ranking_weights[key]
            for key in ("homography_penalty", "concentration_penalty")
        )
        warnings: list[str] = []
        if concentration:
            warnings.extend(joint["concentration_reasons"])
        if geometry["diagnosis"]["homography_dominant"]:
            warnings.append("possible_planar_or_rotation_degeneracy")
        rows.append(
            {
                "image_a": item["image_a"],
                "image_b": item["image_b"],
                "raw_matches": item["raw_matches"],
                "verified_matches": item["verified_matches"],
                "verified_ratio": item["verified_ratio"],
                "coverage_a": item["spatial_grid"]["image_a"]["coverage_ratio"],
                "coverage_b": item["spatial_grid"]["image_b"]["coverage_ratio"],
                "min_coverage": joint["min_coverage_ratio"],
                "median_displacement_px": item["image_space_displacement"]["median"],
                "homography_inlier_ratio": h_ratio,
                "fundamental_inlier_ratio": f_ratio,
                "concentration_warning": concentration,
                "final_score": float(max(0.0, positive - penalties)),
                "score_breakdown": breakdown,
                "warnings": sorted(set(warnings)),
                "scoring_note": "Project diagnostic heuristic; not an official COLMAP initial-pair score.",
            }
        )
    rows.sort(key=lambda row: (-row["final_score"], -row["verified_matches"], row["image_a"], row["image_b"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def config_payload(config: GeometryDiagnosticsConfig) -> dict[str, Any]:
    """把集中配置转换为可序列化报告字段。"""
    return asdict(config)
