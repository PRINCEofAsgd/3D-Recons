from __future__ import annotations

import json
import sqlite3

import cv2
import numpy as np

from store_vision.calibration.colmap_database import (
    COLMAP_MAX_IMAGE_ID,
    draw_camera_match_graph,
    draw_top_verified_matches,
    read_match_statistics,
    write_match_statistics,
)


def _pair_id(image_id_a: int, image_id_b: int) -> int:
    return min(image_id_a, image_id_b) * COLMAP_MAX_IMAGE_ID + max(image_id_a, image_id_b)


def _database(path):
    """创建只含统计所需表的最小 COLMAP 兼容数据库。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE images(image_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE keypoints(image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE matches(pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE two_view_geometries(
            pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB
        );
        """
    )
    keypoints = np.array([[10, 10, 1, 0], [30, 30, 1, 0]], dtype=np.float32)
    for image_id, name, count in ((1, "a.jpg", 2), (2, "b.jpg", 2), (3, "c.jpg", 0)):
        connection.execute("INSERT INTO images VALUES (?, ?)", (image_id, name))
        blob = keypoints.tobytes() if count else None
        connection.execute(
            "INSERT INTO keypoints VALUES (?, ?, 4, ?)", (image_id, count, blob)
        )
    matches = np.array([[0, 0], [1, 1]], dtype=np.uint32)
    connection.execute(
        "INSERT INTO matches VALUES (?, 2, 2, ?)", (_pair_id(1, 2), matches.tobytes())
    )
    connection.execute(
        "INSERT INTO two_view_geometries VALUES (?, 2, 2, ?)",
        (_pair_id(1, 2), matches.tobytes()),
    )
    connection.commit()
    connection.close()


def test_database_statistics_and_outputs_are_derived_from_sqlite(tmp_path):
    database = tmp_path / "database.db"
    _database(database)
    statistics = read_match_statistics(database)
    assert statistics.feature_count_by_image == {"a.jpg": 2, "b.jpg": 2, "c.jpg": 0}
    assert len(statistics.pairs) == 3
    assert statistics.verified_matches_by_pair["a.jpg <-> b.jpg"] == 2
    assert statistics.isolated_images == ["c.jpg"]
    assert len(statistics.connected_components) == 2
    assert not statistics.is_connected

    json_path, csv_path = write_match_statistics(
        statistics, tmp_path / "match_statistics.json", tmp_path / "match_statistics.csv"
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["connected_component_count"] == 2
    assert len(payload["pairs"]) == 3
    assert csv_path.read_text(encoding="utf-8").count("\n") == 4
    assert draw_camera_match_graph(statistics, tmp_path / "graph.png").is_file()


def test_top_match_visualization_uses_verified_match_blob(tmp_path):
    database = tmp_path / "database.db"
    _database(database)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        assert cv2.imwrite(str(tmp_path / name), np.full((50, 50, 3), 220, dtype=np.uint8))
    outputs = draw_top_verified_matches(
        database,
        tmp_path,
        read_match_statistics(database),
        tmp_path / "matches",
    )
    assert len(outputs) == 1
    assert outputs[0].name == "a.jpg__b.jpg.png"
    assert cv2.imread(str(outputs[0])) is not None
