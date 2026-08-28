from __future__ import annotations

import pytest

from store_vision.calibration import colmap_runner


def test_dry_run_builds_argument_list_without_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(colmap_runner, "detect_colmap", lambda: None)
    result = colmap_runner.extract_features(
        tmp_path / "database.db",
        tmp_path / "images with spaces",
        dry_run=True,
    )
    assert result.command[0] == "colmap"
    assert result.command[1] == "feature_extractor"
    image_path_index = result.command.index("--image_path") + 1
    assert result.command[image_path_index] == str(tmp_path / "images with spaces")
    assert result.command[result.command.index("--ImageReader.camera_model") + 1] == "SIMPLE_RADIAL"
    assert result.command[result.command.index("--ImageReader.single_camera") + 1] == "1"
    assert result.command[result.command.index("--FeatureExtraction.use_gpu") + 1] == "0"
    assert result.command[result.command.index("--default_random_seed") + 1] == "0"
    assert result.command[result.command.index("--FeatureExtraction.num_threads") + 1] == "1"


def test_missing_colmap_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(colmap_runner, "detect_colmap", lambda: None)
    with pytest.raises(colmap_runner.ColmapNotFoundError, match="Install COLMAP"):
        colmap_runner.extract_features(tmp_path / "database.db", tmp_path / "images")


def test_fitted_camera_params_and_mapper_freeze_are_explicit(tmp_path, monkeypatch):
    """拟合鱼眼参数和 Mapper 冻结选项必须作为独立 argv 传入。"""

    monkeypatch.setattr(colmap_runner, "detect_colmap", lambda: None)
    feature = colmap_runner.extract_features(
        tmp_path / "database.db",
        tmp_path / "images",
        dry_run=True,
        camera_model="OPENCV_FISHEYE",
        camera_params=[1000, 980, 500, 400, 0.1, 0.01, 0.001, 0.0001],
    )
    assert feature.command[feature.command.index("--ImageReader.camera_params") + 1] == (
        "1000,980,500,400,0.10000000000000001,0.01,0.001,0.0001"
    )

    mapper = colmap_runner.run_mapper(
        tmp_path / "database.db",
        tmp_path / "images",
        tmp_path / "sparse",
        dry_run=True,
        freeze_intrinsics=True,
        max_extra_param=2.0,
    )
    assert mapper.command[mapper.command.index("--Mapper.ba_refine_focal_length") + 1] == "0"
    assert mapper.command[mapper.command.index("--Mapper.max_extra_param") + 1] == "2"


def test_multi_candidate_commands_freeze_intrinsics_and_keep_two_view_tracks(
    tmp_path, monkeypatch
):
    """全局、补注册、三角化和 BA 分支必须生成明确且可审计的 argv。"""

    monkeypatch.setattr(colmap_runner, "detect_colmap", lambda: None)
    global_mapper = colmap_runner.run_global_mapper(
        tmp_path / "database.db",
        tmp_path / "images",
        tmp_path / "global",
        dry_run=True,
    )
    assert (
        global_mapper.command[
            global_mapper.command.index(
                "--GlobalMapper.track_min_num_views_per_track"
            )
            + 1
        ]
        == "2"
    )
    assert (
        global_mapper.command[
            global_mapper.command.index("--GlobalMapper.ba_refine_extra_params") + 1
        ]
        == "0"
    )

    registrator = colmap_runner.run_image_registrator(
        tmp_path / "database.db",
        tmp_path / "input",
        tmp_path / "registered",
        dry_run=True,
    )
    assert "--image_path" not in registrator.command
    triangulator = colmap_runner.run_point_triangulator(
        tmp_path / "database.db",
        tmp_path / "images",
        tmp_path / "seed",
        tmp_path / "triangulated",
        dry_run=True,
    )
    assert triangulator.command[triangulator.command.index("--clear_points") + 1] == "1"
    bundle = colmap_runner.run_bundle_adjuster(
        tmp_path / "triangulated",
        tmp_path / "optimized",
        dry_run=True,
    )
    assert (
        bundle.command[
            bundle.command.index("--BundleAdjustment.refine_extra_params") + 1
        ]
        == "0"
    )
