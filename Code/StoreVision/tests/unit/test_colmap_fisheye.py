"""拟合鱼眼 K/D 接入 COLMAP 数据库的专项测试。"""

from __future__ import annotations

import json
import sqlite3
import struct

import numpy as np
import pytest

from store_vision.calibration.colmap_fisheye import (
    apply_fitted_fisheye_to_database,
    load_fitted_fisheye_calibration,
)


def _write_report(root, dataset):
    """写入包含最终共享 K/D 和验收指标的最小正式报告。"""

    root.mkdir()
    report = root / "fisheye_calibration_report.json"
    report.write_text(
        json.dumps(
            {
                "input": {"dataset_path": str(dataset.resolve())},
                "selection": {
                    "selected_model": "FISHEYE",
                    "selected_K": [
                        [1000.0, 0.0, 1900.0],
                        [0.0, 980.0, 1100.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "selected_D": [0.1, 0.02, -0.003, 0.0004],
                },
                "intrinsics_groups": [{"canonical_resolution": [3840, 2160]}],
                "nonlinear_optimization": {
                    "initialization": {
                        "fit": {
                            "accepted_by_reprojection_threshold": False,
                            "reprojection_rmse_px": 42.0,
                        }
                    },
                    "bundle_adjustment": {
                        "accepted_by_reprojection_threshold": True,
                        "reprojection_rmse_px": 3.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _write_database(path):
    """构造三张图、三套默认相机记录的 COLMAP 最小数据库。"""

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE cameras (
                camera_id INTEGER PRIMARY KEY,
                model INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                params BLOB,
                prior_focal_length INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE images (
                image_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                camera_id INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE rigs (rig_id INTEGER PRIMARY KEY, "
            "ref_sensor_id INTEGER NOT NULL, ref_sensor_type INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE rig_sensors (rig_id INTEGER NOT NULL, "
            "sensor_id INTEGER NOT NULL, sensor_type INTEGER NOT NULL, "
            "sensor_from_rig BLOB)"
        )
        connection.execute(
            "CREATE TABLE frames (frame_id INTEGER PRIMARY KEY, rig_id INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE frame_data (frame_id INTEGER NOT NULL, data_id INTEGER NOT NULL, "
            "sensor_id INTEGER NOT NULL, sensor_type INTEGER NOT NULL)"
        )
        default_4k = sqlite3.Binary(struct.pack("<8d", *([0.0] * 8)))
        default_1080 = sqlite3.Binary(struct.pack("<8d", *([0.0] * 8)))
        connection.executemany(
            "INSERT INTO cameras VALUES (?, 5, ?, ?, ?, 0)",
            [
                (1, 3840, 2160, default_4k),
                (2, 3840, 2160, default_4k),
                (3, 1920, 1080, default_1080),
            ],
        )
        connection.executemany(
            "INSERT INTO images VALUES (?, ?, ?)",
            [(1, "a.jpg", 1), (2, "b.jpg", 2), (3, "c.jpg", 3)],
        )
        connection.executemany(
            "INSERT INTO rigs VALUES (?, ?, 0)",
            [(1, 1), (2, 2), (3, 3)],
        )
        connection.executemany(
            "INSERT INTO frames VALUES (?, ?)",
            [(1, 1), (2, 2), (3, 3)],
        )
        connection.executemany(
            "INSERT INTO frame_data VALUES (?, ?, ?, 0)",
            [(1, 1, 1), (2, 2, 2), (3, 3, 3)],
        )


def test_load_and_apply_fitted_fisheye_groups_by_resolution(tmp_path):
    """同尺寸图片共享 camera，1080p K 为 4K K 的一半且 D 不变。"""

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    calibration = load_fitted_fisheye_calibration(
        _write_report(tmp_path / "fit", dataset),
        expected_dataset=dataset,
    )
    database = tmp_path / "database.db"
    _write_database(database)

    summary = apply_fitted_fisheye_to_database(database, calibration)

    assert summary["camera_group_count"] == 2
    assert not summary["fit_accepted_by_reprojection_threshold"]
    with sqlite3.connect(database) as connection:
        cameras = connection.execute(
            "SELECT camera_id, model, width, height, params, prior_focal_length "
            "FROM cameras ORDER BY width DESC"
        ).fetchall()
        image_cameras = connection.execute(
            "SELECT camera_id FROM images ORDER BY image_id"
        ).fetchall()
        rig_count = connection.execute("SELECT COUNT(*) FROM rigs").fetchone()[0]
        rig_sensor_count = connection.execute(
            "SELECT COUNT(*) FROM rig_sensors"
        ).fetchone()[0]
        frame_rigs = connection.execute(
            "SELECT rig_id FROM frames ORDER BY frame_id"
        ).fetchall()
        frame_sensors = connection.execute(
            "SELECT sensor_id FROM frame_data ORDER BY data_id"
        ).fetchall()
    assert len(cameras) == 2
    assert image_cameras[0] == image_cameras[1]
    assert rig_count == 2
    assert rig_sensor_count == 0
    assert frame_rigs[0] == frame_rigs[1]
    assert frame_sensors[0] == frame_sensors[1]
    params_4k = struct.unpack("<8d", cameras[0][4])
    params_1080 = struct.unpack("<8d", cameras[1][4])
    assert cameras[0][1] == cameras[1][1] == 5
    assert cameras[0][5] == cameras[1][5] == 1
    assert np.allclose(params_1080[:4], np.asarray(params_4k[:4]) * 0.5)
    assert np.allclose(params_1080[4:], params_4k[4:])

    matching_summary = apply_fitted_fisheye_to_database(
        database,
        calibration,
        parameter_source="fitted",
    )
    with sqlite3.connect(database) as connection:
        matching_blob = connection.execute(
            "SELECT params FROM cameras ORDER BY width DESC LIMIT 1"
        ).fetchone()[0]
    matching_params = struct.unpack("<8d", matching_blob)
    assert matching_summary["database_parameter_source"] == "fitted"
    assert np.allclose(matching_params[4:], [0.1, 0.02, -0.003, 0.0004])


def test_fitted_report_rejects_other_dataset(tmp_path):
    """不得把其他门店拟合 K/D 静默接入当前 Sfm。"""

    source_dataset = tmp_path / "source"
    current_dataset = tmp_path / "current"
    source_dataset.mkdir()
    current_dataset.mkdir()
    report = _write_report(tmp_path / "fit", source_dataset)

    with pytest.raises(ValueError, match="其他数据集"):
        load_fitted_fisheye_calibration(report, expected_dataset=current_dataset)
