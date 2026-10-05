"""拟合位姿种子模型生成测试。"""

from __future__ import annotations

import json
import sqlite3
import struct

import numpy as np

from store_vision.calibration.colmap_parser import parse_images_text
from store_vision.calibration.pose_seed import write_pose_seed_model


def test_pose_seed_contains_database_keypoints_and_fitted_poses(tmp_path):
    database = tmp_path / "database.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE cameras (camera_id INTEGER, model INTEGER, width INTEGER, "
            "height INTEGER, params BLOB)"
        )
        connection.execute(
            "CREATE TABLE images (image_id INTEGER, name TEXT, camera_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE keypoints (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB)"
        )
        connection.execute(
            "INSERT INTO cameras VALUES (1, 5, 1000, 800, ?)",
            (sqlite3.Binary(struct.pack("<8d", 400, 400, 500, 400, 0, 0, 0, 0)),),
        )
        for image_id in (1, 2):
            connection.execute(
                "INSERT INTO images VALUES (?, ?, 1)",
                (image_id, f"camera-{image_id}.jpg"),
            )
            points = np.asarray([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
            connection.execute(
                "INSERT INTO keypoints VALUES (?, 2, 2, ?)",
                (image_id, sqlite3.Binary(points.tobytes())),
            )
    report = tmp_path / "fisheye_calibration_report.json"
    report.write_text(
        json.dumps(
            {
                "poses": [
                    {
                        "image_name": f"camera-{image_id}.jpg",
                        "R": np.eye(3).tolist(),
                        "T_metres": [float(image_id), 0.0, 0.0],
                    }
                    for image_id in (1, 2)
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = write_pose_seed_model(database, report, tmp_path / "seed")
    images = parse_images_text(tmp_path / "seed" / "images.txt")

    assert summary["pose_count"] == 2
    assert summary["keypoint_count"] == 4
    assert len(images) == 2
    assert all(record.metadata["point2d_count"] == 2 for record in images.values())
    assert all(
        record.metadata["point3d_observation_count"] == 0
        for record in images.values()
    )
