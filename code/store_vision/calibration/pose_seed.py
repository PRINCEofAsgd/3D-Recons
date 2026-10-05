"""把联合拟合位姿和 COLMAP 特征数据库组合为可三角化的文本模型。"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np


CAMERA_MODEL_NAMES = {
    0: "SIMPLE_PINHOLE",
    1: "PINHOLE",
    2: "SIMPLE_RADIAL",
    3: "RADIAL",
    4: "OPENCV",
    5: "OPENCV_FISHEYE",
}


def _rotation_to_quaternion_wxyz(rotation: np.ndarray) -> tuple[float, float, float, float]:
    """把正交旋转矩阵稳定转换成 COLMAP 使用的 wxyz 四元数。"""

    matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    # 输入来自数值 BA，先投影到最近的合法旋转矩阵，避免舍入误差。
    u, _, vh = np.linalg.svd(matrix)
    matrix = u @ vh
    if np.linalg.det(matrix) < 0:
        u[:, -1] *= -1
        matrix = u @ vh
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = 2.0 * np.sqrt(trace + 1.0)
        quaternion = (
            0.25 * scale,
            (matrix[2, 1] - matrix[1, 2]) / scale,
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[1, 0] - matrix[0, 1]) / scale,
        )
    else:
        axis = int(np.argmax(np.diag(matrix)))
        if axis == 0:
            scale = 2.0 * np.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2])
            quaternion = (
                (matrix[2, 1] - matrix[1, 2]) / scale,
                0.25 * scale,
                (matrix[0, 1] + matrix[1, 0]) / scale,
                (matrix[0, 2] + matrix[2, 0]) / scale,
            )
        elif axis == 1:
            scale = 2.0 * np.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2])
            quaternion = (
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[0, 1] + matrix[1, 0]) / scale,
                0.25 * scale,
                (matrix[1, 2] + matrix[2, 1]) / scale,
            )
        else:
            scale = 2.0 * np.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1])
            quaternion = (
                (matrix[1, 0] - matrix[0, 1]) / scale,
                (matrix[0, 2] + matrix[2, 0]) / scale,
                (matrix[1, 2] + matrix[2, 1]) / scale,
                0.25 * scale,
            )
    normalized = np.asarray(quaternion, dtype=np.float64)
    normalized /= np.linalg.norm(normalized)
    if normalized[0] < 0:
        normalized *= -1
    return tuple(float(value) for value in normalized)


def _load_pose_map(report_path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    poses = payload.get("poses")
    if not isinstance(poses, list):
        sibling = report_path.parent / "poses.json"
        if sibling.is_file():
            poses = json.loads(sibling.read_text(encoding="utf-8")).get("poses")
    if not isinstance(poses, list):
        raise ValueError(f"拟合报告缺少 poses：{report_path}")
    pose_map: dict[str, dict[str, Any]] = {}
    for pose in poses:
        if not isinstance(pose, dict) or not pose.get("image_name"):
            continue
        name = str(pose["image_name"])
        pose_map[name] = pose
        pose_map.setdefault(Path(name).name, pose)
    return pose_map


def write_pose_seed_model(
    database_path: str | Path,
    calibration_report_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """生成包含全部数据库关键点、共享 K/D 和拟合位姿的 COLMAP 文本模型。"""

    database = Path(database_path)
    report = Path(calibration_report_path)
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    pose_map = _load_pose_map(report)

    with sqlite3.connect(database) as connection:
        camera_rows = connection.execute(
            "SELECT camera_id, model, width, height, params FROM cameras ORDER BY camera_id"
        ).fetchall()
        image_rows = connection.execute(
            "SELECT image_id, name, camera_id FROM images ORDER BY image_id"
        ).fetchall()
        keypoint_rows = {
            # SQLite 允许零行关键点的 BLOB 为 NULL；种子模型仍需保留该图片。
            int(image_id): (int(rows), int(cols), bytes(data or b""))
            for image_id, rows, cols, data in connection.execute(
                "SELECT image_id, rows, cols, data FROM keypoints"
            )
        }
    if not camera_rows or not image_rows:
        raise ValueError("COLMAP 数据库缺少 camera 或 image 记录")

    cameras: list[str] = []
    for camera_id, model_id, width, height, blob in camera_rows:
        model = CAMERA_MODEL_NAMES.get(int(model_id))
        if model is None:
            raise ValueError(f"位姿种子暂不支持 COLMAP camera model {model_id}")
        params = struct.unpack(f"<{len(blob) // 8}d", bytes(blob))
        cameras.append(
            " ".join(
                [
                    str(camera_id),
                    model,
                    str(width),
                    str(height),
                    *(f"{value:.17g}" for value in params),
                ]
            )
        )

    camera_ids = [int(row[0]) for row in camera_rows]
    rig_id_by_camera = {camera_id: index + 1 for index, camera_id in enumerate(camera_ids)}
    rig_lines = [
        f"{rig_id_by_camera[camera_id]} 1 CAMERA {camera_id}"
        for camera_id in camera_ids
    ]
    frame_lines: list[str] = []
    image_lines: list[str] = []
    missing_poses: list[str] = []
    for frame_id, (image_id, name, camera_id) in enumerate(image_rows, start=1):
        pose = pose_map.get(str(name)) or pose_map.get(Path(str(name)).name)
        if pose is None:
            missing_poses.append(str(name))
            continue
        rotation = np.asarray(pose.get("R"), dtype=np.float64).reshape(3, 3)
        translation = np.asarray(
            pose.get("T_metres", pose.get("T")), dtype=np.float64
        ).reshape(3)
        quaternion = _rotation_to_quaternion_wxyz(rotation)
        pose_values = " ".join(
            f"{value:.17g}" for value in (*quaternion, *translation)
        )
        frame_lines.append(
            f"{frame_id} {rig_id_by_camera[int(camera_id)]} {pose_values} "
            f"1 CAMERA {camera_id} {image_id}"
        )
        image_lines.append(
            f"{image_id} {pose_values} {camera_id} {name}"
        )
        rows, cols, blob = keypoint_rows.get(int(image_id), (0, 0, b""))
        if rows and cols < 2:
            raise ValueError(f"图片 {name} 的 keypoints 列数小于 2")
        keypoints = (
            np.frombuffer(blob, dtype=np.float32).reshape(rows, cols)
            if rows
            else np.empty((0, max(cols, 2)), dtype=np.float32)
        )
        image_lines.append(
            " ".join(
                f"{float(row[0]):.17g} {float(row[1]):.17g} -1"
                for row in keypoints
            )
        )
    if missing_poses:
        raise ValueError(
            "拟合位姿未覆盖数据库图片：" + ", ".join(sorted(missing_poses))
        )

    headers = {
        "rigs.txt": (
            "# Rig calib list with one line of data per calib:\n"
            "#   RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[]\n"
            f"# Number of rigs: {len(rig_lines)}\n"
        ),
        "cameras.txt": (
            "# Camera list with one line of data per camera:\n"
            "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
            f"# Number of cameras: {len(cameras)}\n"
        ),
        "frames.txt": (
            "# Frame list with one line of data per frame:\n"
            "#   FRAME_ID, RIG_ID, RIG_FROM_WORLD, NUM_DATA_IDS, DATA_IDS[]\n"
            f"# Number of frames: {len(frame_lines)}\n"
        ),
        "images.txt": (
            "# Image list with two lines of data per image:\n"
            "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
            "#   POINTS2D[] as (X, Y, POINT3D_ID)\n"
            f"# Number of images: {len(image_lines) // 2}\n"
        ),
        "points3D.txt": (
            "# 3D point list with one line of data per point:\n"
            "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]\n"
            "# Number of points: 0\n"
        ),
    }
    contents = {
        "rigs.txt": rig_lines,
        "cameras.txt": cameras,
        "frames.txt": frame_lines,
        "images.txt": image_lines,
        "points3D.txt": [],
    }
    for filename, lines in contents.items():
        (output / filename).write_text(
            headers[filename] + "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )
    return {
        "status": "ready",
        "source": str(report),
        "output": str(output),
        "pose_count": len(image_rows),
        "camera_count": len(camera_rows),
        "keypoint_count": sum(row[0] for row in keypoint_rows.values()),
        "purpose": "prior_assisted_triangulation_candidate_only",
    }
