from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from store_vision.calibration.geometry_diagnostics import (
    GeometryDiagnosticsRequest,
    _controlled_experiment_plan,
    _select_pairs,
    run_geometry_diagnostics,
)
from store_vision.calibration.intrinsics_config import (
    load_and_validate_intrinsics,
    validate_intrinsics_payload,
)
from store_vision.calibration.model_geometry import analyze_current_model
from store_vision.calibration.pair_geometry import (
    ColmapPairDatabase,
    GeometryDiagnosticsConfig,
    combined_coverage,
    diagnose_pair,
    displacement_statistics,
    estimate_geometry_models,
    image_ids_to_pair_id,
    pair_id_to_image_ids,
    parse_blob,
    rank_initial_pairs,
    spatial_coverage,
    stable_spatial_sample_indices,
)


NAMES = [
    "CAMERA-01-01.jpg",
    "CAMERA-04-01.jpg",
    "CAMERA-05-01.jpg",
    "CAMERA-08-01.jpg",
    "CAMERA-07-01.jpg",
    "CAMERA-09-01.jpg",
    "CAMERA-02-01.jpg",
    "CAMERA-03-01.jpg",
    "CAMERA-06-01.jpg",
]


def _projected_correspondences(count: int = 24) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(4)
    xyz = rng.uniform([-1, -0.7, 3], [1, 0.7, 7], size=(count, 3))
    points_a = xyz[:, :2] / xyz[:, 2:3]
    points_b = (xyz[:, :2] + np.array([0.35, 0.03])) / xyz[:, 2:3]
    return points_a * 45 + [60, 40], points_b * 45 + [60, 40]


def _make_database(path: Path, names: list[str], *, include_all_pairs: bool = True) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cameras(camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER);
        CREATE TABLE images(image_id INTEGER PRIMARY KEY, name TEXT UNIQUE, camera_id INTEGER);
        CREATE TABLE keypoints(image_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE matches(pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB);
        CREATE TABLE two_view_geometries(pair_id INTEGER PRIMARY KEY, rows INTEGER, cols INTEGER, data BLOB, config INTEGER, F BLOB, E BLOB, H BLOB, qvec BLOB, tvec BLOB);
        """
    )
    connection.execute("INSERT INTO cameras VALUES (1, 1, 120, 80, NULL, 0)")
    points_a, _ = _projected_correspondences(24)
    for image_id, name in enumerate(names, 1):
        keypoints = np.column_stack([points_a + image_id, np.ones((24, 2))]).astype(np.float32)
        connection.execute("INSERT INTO images VALUES (?, ?, 1)", (image_id, name))
        connection.execute("INSERT INTO keypoints VALUES (?, 24, 4, ?)", (image_id, keypoints.tobytes()))
    if include_all_pairs:
        matches = np.column_stack([np.arange(24), np.arange(24)]).astype(np.uint32)
        for image_id_a in range(1, len(names) + 1):
            for image_id_b in range(image_id_a + 1, len(names) + 1):
                pair_id = image_ids_to_pair_id(image_id_a, image_id_b)
                raw = matches
                verified = matches[: 8 + (image_id_a + image_id_b) % 12]
                connection.execute("INSERT INTO matches VALUES (?, ?, 2, ?)", (pair_id, len(raw), raw.tobytes()))
                connection.execute(
                    "INSERT INTO two_view_geometries VALUES (?, ?, 2, ?, 2, NULL, NULL, NULL, NULL, NULL)",
                    (pair_id, len(verified), verified.tobytes()),
                )
    connection.commit()
    connection.close()


def _make_model(root: Path) -> None:
    root.mkdir(parents=True)
    root.joinpath("cameras.txt").write_text("1 PINHOLE 120 80 100 100 60 40\n", encoding="utf-8")
    observations = " ".join(f"{10+i} {20+i} {i+1}" for i in range(12))
    root.joinpath("images.txt").write_text(
        f"7 1 0 0 0 0 0 0 1 {NAMES[6]}\n{observations}\n"
        f"8 1 0 0 0 -1 0 0 1 {NAMES[7]}\n{observations}\n",
        encoding="utf-8",
    )
    root.joinpath("points3D.txt").write_text(
        "".join(f"{i+1} {i*0.1} 0 {4+i*0.1} 255 255 255 {0.1+i*0.01} 7 {i} 8 {i}\n" for i in range(12)),
        encoding="utf-8",
    )


@pytest.fixture
def diagnostic_fixture(tmp_path):
    database = tmp_path / "database.db"
    images = tmp_path / "images"
    images.mkdir()
    _make_database(database, NAMES)
    for index, name in enumerate(NAMES):
        image = np.full((80, 120, 3), 40 + index * 10, dtype=np.uint8)
        assert cv2.imwrite(str(images / name), image)
    model = tmp_path / "model"
    _make_model(model)
    return database, images, model


def test_pair_id_round_trip():
    assert pair_id_to_image_ids(image_ids_to_pair_id(9, 2)) == (2, 9)


def test_pair_id_rejects_invalid_values():
    with pytest.raises(ValueError):
        image_ids_to_pair_id(2, 2)
    with pytest.raises(ValueError):
        pair_id_to_image_ids(0)


def test_keypoints_blob_parsing(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader:
        assert reader.keypoints(NAMES[0]).shape == (24, 4)


def test_raw_matches_blob_parsing(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader:
        assert len(reader.pair_indices(NAMES[0], NAMES[1], verified=False)) == 24


def test_two_view_geometry_blob_parsing(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader:
        assert len(reader.pair_indices(NAMES[0], NAMES[1], verified=True)) > 0


def test_image_name_to_id_mapping(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader:
        assert reader.image(NAMES[4]).image_id == 5


def test_empty_image_pair_returns_empty_arrays(tmp_path):
    database = tmp_path / "empty.db"
    _make_database(database, ["a.jpg", "b.jpg"], include_all_pairs=False)
    with ColmapPairDatabase(database) as reader:
        pair = reader.pair("a.jpg", "b.jpg")
        assert pair.points()[0].shape == (0, 2)


def test_nonexistent_image_pair_has_actionable_error(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader, pytest.raises(KeyError, match="not present"):
        reader.pair("missing.jpg", NAMES[0])


def test_empty_match_blob_shape():
    assert parse_blob(None, 0, 2, np.uint32, label="matches").shape == (0, 2)


def test_malformed_blob_is_rejected():
    with pytest.raises(ValueError, match="expected"):
        parse_blob(b"123", 1, 2, np.uint32, label="matches")


def test_reverse_pair_swaps_match_columns(tmp_path):
    database = tmp_path / "reverse.db"
    _make_database(database, ["a.jpg", "b.jpg"], include_all_pairs=False)
    connection = sqlite3.connect(database)
    value = np.array([[2, 3]], np.uint32)
    connection.execute("INSERT INTO matches VALUES (?, 1, 2, ?)", (image_ids_to_pair_id(1, 2), value.tobytes()))
    connection.commit()
    connection.close()
    with ColmapPairDatabase(database) as reader:
        assert reader.pair_indices("b.jpg", "a.jpg", verified=False).tolist() == [[3, 2]]


def test_missing_database_table_is_reported(tmp_path):
    database = tmp_path / "bad.db"
    sqlite3.connect(database).close()
    with pytest.raises(sqlite3.DatabaseError, match="missing tables"):
        ColmapPairDatabase(database)


def test_grid_coverage_counts_cells():
    config = GeometryDiagnosticsConfig(grid_cols=2, grid_rows=2)
    result = spatial_coverage(np.array([[1, 1], [99, 1], [1, 99]]), 100, 100, config)
    assert result["occupied_cells"] == 3
    assert result["coverage_ratio"] == pytest.approx(0.75)


def test_concentration_warning_for_single_cell():
    config = GeometryDiagnosticsConfig(grid_cols=2, grid_rows=2, high_cell_ratio=0.5)
    coverage = spatial_coverage(np.array([[1, 1], [2, 2], [3, 3]]), 100, 100, config)
    assert combined_coverage(coverage, coverage, config)["concentration_warning"]


def test_displacement_statistics_and_near_zero_ratio():
    result = displacement_statistics(np.array([[0, 0], [0, 0]]), np.array([[3, 4], [30, 40]]), GeometryDiagnosticsConfig(near_zero_px=10))
    assert result["median"] == pytest.approx(27.5)
    assert result["near_zero_ratio"] == pytest.approx(0.5)


def test_homography_estimation_succeeds():
    points_a, points_b = _projected_correspondences()
    assert estimate_geometry_models(points_a, points_b, GeometryDiagnosticsConfig())["homography"]["success"]


def test_homography_estimation_degrades_with_too_few_points():
    result = estimate_geometry_models(np.zeros((3, 2)), np.ones((3, 2)), GeometryDiagnosticsConfig())
    assert not result["homography"]["success"]
    assert "at least 4" in result["homography"]["failure_reason"]


def test_fundamental_estimation_succeeds():
    points_a, points_b = _projected_correspondences()
    assert estimate_geometry_models(points_a, points_b, GeometryDiagnosticsConfig())["fundamental"]["success"]


def test_fundamental_estimation_degrades_with_too_few_points():
    result = estimate_geometry_models(np.zeros((7, 2)), np.ones((7, 2)), GeometryDiagnosticsConfig())
    assert not result["fundamental"]["success"]
    assert "at least 8" in result["fundamental"]["failure_reason"]


def test_ranking_is_stable(diagnostic_fixture):
    database, _, _ = diagnostic_fixture
    with ColmapPairDatabase(database) as reader:
        diagnostics = [diagnose_pair(pair, GeometryDiagnosticsConfig()) for pair in reader.all_pairs()]
    first = rank_initial_pairs(diagnostics, GeometryDiagnosticsConfig())
    second = rank_initial_pairs(diagnostics, GeometryDiagnosticsConfig())
    assert first == second


def test_visualization_sampling_is_seed_stable():
    points = np.column_stack([np.arange(100), np.arange(100) % 9])
    first = stable_spatial_sample_indices(points, 15, seed=3)
    second = stable_spatial_sample_indices(points, 15, seed=3)
    assert np.array_equal(first, second)


def _valid_intrinsics(images: list[str]) -> dict:
    return {
        "camera_groups": [
            {
                "group_id": f"g{index}", "camera_model": "PINHOLE", "width": 120, "height": 80,
                "params": {"fx": 100, "fy": 100, "cx": 60, "cy": 40}, "distortion": None,
                "images": [image],
            }
            for index, image in enumerate(images)
        ]
    }


def test_intrinsics_missing_required_field():
    payload = _valid_intrinsics(["a.jpg"])
    payload["camera_groups"][0]["params"].pop("fx")
    result = validate_intrinsics_payload(payload, {"a.jpg": (120, 80)})
    assert any("fx" in error for error in result["errors"])


def test_intrinsics_duplicate_group_assignment():
    payload = _valid_intrinsics(["a.jpg", "b.jpg"])
    payload["camera_groups"][1]["images"] = ["a.jpg", "b.jpg"]
    result = validate_intrinsics_payload(payload, {"a.jpg": (120, 80), "b.jpg": (120, 80)})
    assert any("multiple groups" in error for error in result["errors"])


def test_intrinsics_unassigned_image():
    result = validate_intrinsics_payload(_valid_intrinsics(["a.jpg"]), {"a.jpg": (120, 80), "b.jpg": (120, 80)})
    assert result["unassigned_images"] == ["b.jpg"]


def test_intrinsics_resolution_mismatch():
    result = validate_intrinsics_payload(_valid_intrinsics(["a.jpg"]), {"a.jpg": (1920, 1080)})
    assert any("does not match" in error for error in result["errors"])


def test_intrinsics_unsupported_camera_model():
    payload = _valid_intrinsics(["a.jpg"])
    payload["camera_groups"][0]["camera_model"] = "MYSTERY"
    result = validate_intrinsics_payload(payload, {"a.jpg": (120, 80)})
    assert any("unsupported" in error for error in result["errors"])


def test_missing_intrinsics_does_not_guess_values():
    result = load_and_validate_intrinsics(None, {"a.jpg": (120, 80)})
    assert result["status"] == "missing_required_intrinsics"
    assert not result["ready_for_known_intrinsics"]


def test_current_model_track_length_statistics(tmp_path):
    model = tmp_path / "model"
    _make_model(model)
    result = analyze_current_model(model)
    assert result["track_lengths"]["distribution"] == {"2": 12}
    assert result["sparse_point_count"] == 12


def test_current_model_missing_files_degrades(tmp_path):
    result = analyze_current_model(tmp_path)
    assert result["status"] == "unavailable"


def test_controlled_experiments_are_blocked_without_intrinsics(tmp_path):
    request = GeometryDiagnosticsRequest(tmp_path / "db", tmp_path / "images", tmp_path / "model", tmp_path / "reports")
    plan = _controlled_experiment_plan(request, {"ready_for_known_intrinsics": False}, [])
    assert [item["status"] for item in plan["experiments"][1:]] == ["blocked"] * 4


def test_dry_run_does_not_write_files(diagnostic_fixture, tmp_path):
    database, images, model = diagnostic_fixture
    output = tmp_path / "reports"
    result = run_geometry_diagnostics(GeometryDiagnosticsRequest(database, images, model, output, dry_run=True))
    assert result["writes_performed"] is False
    assert not output.exists()


def test_report_fields_and_markdown_are_generated(diagnostic_fixture, tmp_path):
    database, images, model = diagnostic_fixture
    output = tmp_path / "reports"
    result = run_geometry_diagnostics(GeometryDiagnosticsRequest(database, images, model, output))
    assert {
        "geometry_diagnostics", "current_model_geometry", "intrinsics_configuration",
        "controlled_experiments", "unregistered_image_diagnostics",
    } <= result.keys()
    assert (output / "geometry_diagnostics_report.md").is_file()
    assert (output / "initial_pair_ranking.csv").is_file()
    assert len(list((output / "pair_diagnostics").glob("*/verified_matches.png"))) >= 4


def test_repeated_run_with_overwrite_is_stable(diagnostic_fixture, tmp_path):
    database, images, model = diagnostic_fixture
    output = tmp_path / "reports"
    request = GeometryDiagnosticsRequest(database, images, model, output)
    run_geometry_diagnostics(request)
    first = (output / "initial_pair_ranking.json").read_bytes()
    run_geometry_diagnostics(GeometryDiagnosticsRequest(database, images, model, output, overwrite=True))
    assert (output / "initial_pair_ranking.json").read_bytes() == first


def test_existing_diagnostics_require_overwrite(diagnostic_fixture, tmp_path):
    database, images, model = diagnostic_fixture
    output = tmp_path / "reports"
    request = GeometryDiagnosticsRequest(database, images, model, output)
    run_geometry_diagnostics(request)
    with pytest.raises(FileExistsError, match="overwrite"):
        run_geometry_diagnostics(request)


def test_dataset_skips_unavailable_builtin_default_pairs():
    """GUI 对任意数据集运行诊断时，不应被示例默认图像对阻塞。"""

    rows = [
        {
            "image_a": "camera-a.jpg",
            "image_b": "camera-b.jpg",
            "verified_matches": 20,
            "spatial_grid": {"joint": {"min_coverage_ratio": 0.5}},
        }
    ]
    pairs, reasons = _select_pairs(rows, (), False)
    assert pairs == [("camera-a.jpg", "camera-b.jpg")]
    assert reasons["camera-a.jpg <-> camera-b.jpg"] == [
        "best_spatial_coverage",
        "most_verified_matches",
        "weak_positive_pair",
    ]


def test_missing_explicit_pair_still_fails():
    rows = [
        {
            "image_a": "camera-a.jpg",
            "image_b": "camera-b.jpg",
            "verified_matches": 20,
            "spatial_grid": {"joint": {"min_coverage_ratio": 0.5}},
        }
    ]
    with pytest.raises(KeyError, match="requested diagnostic image pair"):
        _select_pairs(rows, (("missing.jpg", "camera-b.jpg"),), False)
