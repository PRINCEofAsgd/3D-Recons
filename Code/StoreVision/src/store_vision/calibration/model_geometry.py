"""COLMAP 文本模型的二视图结构、轨迹和角度诊断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from store_vision.calibration.colmap_parser import parse_cameras_text, parse_images_text


@dataclass(frozen=True)
class ModelPoint:
    point_id: int
    xyz: np.ndarray
    error: float
    track: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ModelImageObservations:
    image_id: int
    name: str
    point3d_by_feature_index: dict[int, int]


def _physical_lines(path: Path) -> list[str]:
    """保留模型数据物理行，同时过滤注释。"""
    return [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


def parse_model_observations(path: str | Path) -> dict[str, ModelImageObservations]:
    """解析 images.txt 第二行，保留数据库 keypoint 索引到 3D 点的映射。"""

    lines = _physical_lines(Path(path))
    output: dict[str, ModelImageObservations] = {}
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        fields = lines[index].split()
        if len(fields) < 10:
            raise ValueError(f"invalid COLMAP image line: {lines[index]}")
        image_id = int(fields[0])
        name = " ".join(fields[9:])
        observation_fields = lines[index + 1].split() if index + 1 < len(lines) else []
        if len(observation_fields) % 3:
            raise ValueError(f"invalid COLMAP POINTS2D row for {name}")
        mapping: dict[int, int] = {}
        for feature_index in range(len(observation_fields) // 3):
            point_id = int(observation_fields[feature_index * 3 + 2])
            if point_id >= 0:
                mapping[feature_index] = point_id
        output[name] = ModelImageObservations(image_id, name, mapping)
        index += 2
    return output


def parse_model_points(path: str | Path) -> dict[int, ModelPoint]:
    """解析 points3D.txt 的坐标、误差和完整轨迹。"""
    points: dict[int, ModelPoint] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 8 or (len(fields) - 8) % 2:
            raise ValueError(f"invalid COLMAP point3D line: {line}")
        track = tuple((int(fields[index]), int(fields[index + 1])) for index in range(8, len(fields), 2))
        point_id = int(fields[0])
        points[point_id] = ModelPoint(
            point_id,
            np.asarray([float(value) for value in fields[1:4]], dtype=np.float64),
            float(fields[7]),
            track,
        )
    return points


def _stats(values: list[float] | np.ndarray) -> dict[str, float | None]:
    """生成模型数值的统一摘要。"""
    array = np.asarray(values, dtype=np.float64)
    if not len(array):
        return {"min": None, "mean": None, "median": None, "p90": None, "max": None}
    return {
        "min": float(array.min()),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "p90": float(np.percentile(array, 90)),
        "max": float(array.max()),
    }


def _triangulation_angles(points: dict[int, ModelPoint], centers: list[np.ndarray]) -> dict[str, Any]:
    """从二视图模型射线计算仅供模型内部参考的夹角。"""
    if len(centers) != 2:
        return {"status": "unavailable", "reason": "exactly two registered camera centers are required"}
    angles: list[float] = []
    for point in points.values():
        ray_a = centers[0] - point.xyz
        ray_b = centers[1] - point.xyz
        denominator = np.linalg.norm(ray_a) * np.linalg.norm(ray_b)
        if denominator <= 0:
            continue
        angle = np.degrees(np.arccos(np.clip(float(np.dot(ray_a, ray_b) / denominator), -1.0, 1.0)))
        angles.append(float(angle))
    stats = _stats(angles)
    array = np.asarray(angles)
    return {
        "status": "model_internal_only",
        **stats,
        "below_0_5_degree_ratio": float(np.mean(array < 0.5)) if len(array) else None,
        "below_1_degree_ratio": float(np.mean(array < 1.0)) if len(array) else None,
        "above_2_degree_ratio": float(np.mean(array > 2.0)) if len(array) else None,
        "warning": "Angles are internally consistent with this model, but its abnormal estimated intrinsics make them unreliable as physical scene angles.",
    }


def analyze_current_model(model_path: str | Path) -> dict[str, Any]:
    """读取当前文本模型，输出事实统计和明确的全局对齐限制。"""

    root = Path(model_path)
    required = [root / name for name in ("cameras.txt", "images.txt", "points3D.txt")]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {
            "status": "unavailable",
            "reason": f"COLMAP text model missing: {', '.join(missing)}",
            "model_path": str(root),
        }
    cameras = parse_cameras_text(required[0])
    images = parse_images_text(required[1], cameras)
    observations = parse_model_observations(required[1])
    points = parse_model_points(required[2])
    camera_rows: list[dict[str, Any]] = []
    centers: list[np.ndarray] = []
    for image_id, record in sorted(images.items(), key=lambda item: item[1].name):
        if record.pose is None:
            continue
        center = np.asarray(record.pose.camera_center(), dtype=np.float64)
        centers.append(center)
        intrinsics = record.intrinsics
        camera_rows.append(
            {
                "image_id": image_id,
                "image_name": record.name,
                "camera_id": record.camera_id,
                "camera_model": intrinsics.model if intrinsics else None,
                "width": intrinsics.width if intrinsics else None,
                "height": intrinsics.height if intrinsics else None,
                "params": list(intrinsics.params) if intrinsics else [],
                "camera_center_xyz": center.tolist(),
            }
        )
    baseline = float(np.linalg.norm(centers[0] - centers[1])) if len(centers) == 2 else None
    errors = np.asarray([point.error for point in points.values()], dtype=np.float64)
    track_lengths = np.asarray([len(point.track) for point in points.values()], dtype=int)
    xyz = np.asarray([point.xyz for point in points.values()], dtype=np.float64)
    track_distribution = {
        str(length): int(np.count_nonzero(track_lengths == length))
        for length in sorted(set(track_lengths.tolist()))
    }
    depth_by_camera: dict[str, dict[str, float | None]] = {}
    for image_id, record in images.items():
        if record.pose is None:
            continue
        rotation = record.pose.rotation_matrix()
        translation = np.asarray(record.pose.translation_xyz)
        depths = [(rotation @ point.xyz + translation)[2] for point in points.values()]
        depth_by_camera[record.name] = _stats(depths)
    abnormal_reasons: list[str] = []
    for camera in cameras.values():
        if camera.model == "SIMPLE_RADIAL" and len(camera.params) == 4:
            focal, _, _, radial = camera.params
            if focal > max(camera.width, camera.height) * 2.5:
                abnormal_reasons.append("estimated focal length is unusually large relative to image size")
            if abs(radial) > 1.0:
                abnormal_reasons.append("estimated radial coefficient magnitude is unusually large")
    mostly_two_view = bool(len(track_lengths) and np.mean(track_lengths <= 2) >= 0.95)
    return {
        "status": "complete",
        "model_path": str(root),
        "registered_image_count": len(images),
        "registered_images": [row["image_name"] for row in camera_rows],
        "cameras": camera_rows,
        "baseline_model_units": baseline,
        "sparse_point_count": len(points),
        "track_lengths": {
            "distribution": track_distribution,
            "min": int(track_lengths.min()) if len(track_lengths) else None,
            "median": float(np.median(track_lengths)) if len(track_lengths) else None,
            "max": int(track_lengths.max()) if len(track_lengths) else None,
            "ratio_at_most_two": float(np.mean(track_lengths <= 2)) if len(track_lengths) else None,
            "almost_all_two_view": mostly_two_view,
        },
        "reprojection_error_px": {
            "mean": float(errors.mean()) if len(errors) else None,
            "median": float(np.median(errors)) if len(errors) else None,
            "max": float(errors.max()) if len(errors) else None,
        },
        "world_xyz_range": {
            "min": xyz.min(axis=0).tolist() if len(xyz) else None,
            "max": xyz.max(axis=0).tolist() if len(xyz) else None,
        },
        "bounding_box": {
            "minimum_xyz": xyz.min(axis=0).tolist() if len(xyz) else None,
            "maximum_xyz": xyz.max(axis=0).tolist() if len(xyz) else None,
            "extent_xyz": np.ptp(xyz, axis=0).tolist() if len(xyz) else None,
        },
        "depth_by_camera": depth_by_camera,
        "triangulation_angle_degrees": _triangulation_angles(points, centers),
        "intrinsics_assessment": {
            "trustworthy": not abnormal_reasons,
            "warnings": sorted(set(abnormal_reasons)),
            "consequence": "Model-derived depths and triangulation angles are diagnostic only when intrinsics are untrustworthy.",
        },
        "global_alignment_support": {
            "supported": len(images) >= 3 and not mostly_two_view,
            "reasons": [
                "Only two of the nine database images are registered.",
                "Tracks of length two do not connect additional cameras into a global model.",
                "The estimated intrinsics are flagged as untrustworthy.",
            ] if len(images) == 2 else [],
        },
        "observation_count_by_image": {
            name: len(item.point3d_by_feature_index) for name, item in observations.items()
        },
    }
