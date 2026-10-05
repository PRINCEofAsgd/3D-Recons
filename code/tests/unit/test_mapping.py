"""Mapping: overlap / stitch / 2.5D."""

from __future__ import annotations

import json
from pathlib import Path

import cv2

from store_vision.calibration import calibrate_homographies, calibrate_shared_fisheye
from store_vision.config import StoreConfig
from store_vision.data import load_store_folder
from store_vision.mapping import build_map25d, compute_overlaps, stitch_cameras_to_floor
from store_vision.mapping.map25d_review import (
    build_reviewed_collection,
    default_included_feature_ids,
    write_review_outputs,
)


def test_overlap_computes_pairs(fixture_dir):
    ds = load_store_folder(fixture_dir)
    calibrate_shared_fisheye(ds)
    calibrate_homographies(ds)
    summary = compute_overlaps(ds)
    assert summary["n_cameras"] >= 9
    assert summary["n_pairs_overlapping"] >= 1


def test_stitch_produces_image(fixture_dir):
    ds = load_store_folder(fixture_dir)
    cfg = StoreConfig()
    calibrate_shared_fisheye(ds)
    calibrate_homographies(ds)
    floor, mosaic = stitch_cameras_to_floor(ds, cfg)
    assert floor.shape == mosaic.shape
    assert floor.shape[0] > 100 and floor.shape[1] > 100


def test_map25d_geojson_shape():
    from store_vision.data.models import MapObject25D

    obj = MapObject25D(
        id="t0",
        label="table",
        polygon_cm=[(0, 0), (100, 0), (100, 50), (0, 50)],
        height_cm=70.0,
    )
    fc = build_map25d([obj])
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    f = fc["features"][0]
    assert f["properties"]["height_cm"] == 70.0
    assert f["geometry"]["type"] == "Polygon"


def test_map25d_review_can_include_weak_candidate_without_overwriting_source(
    tmp_path,
):
    """人工复核可纳入弱候选，并单独生成派生结果和选择审计。"""

    from store_vision.data.models import MapObject25D

    automatic = MapObject25D(
        id="table_000",
        label="table",
        polygon_cm=[(0, 0), (100, 0), (100, 50), (0, 50)],
        height_cm=70,
        meta={"review_default_included": True},
    )
    weak = MapObject25D(
        id="review_weak_000",
        label="table",
        polygon_cm=[(200, 0), (300, 0), (300, 50), (200, 50)],
        height_cm=70,
        meta={"review_default_included": False},
    )
    candidates = build_map25d([automatic, weak])
    assert default_included_feature_ids(candidates) == {"table_000"}

    reviewed = build_reviewed_collection(
        candidates, {"table_000", "review_weak_000"}
    )
    assert len(reviewed["features"]) == 2
    assert len(candidates["features"]) == 2

    geojson_path, preview_path = write_review_outputs(
        candidates,
        {"review_weak_000"},
        tmp_path,
    )
    assert geojson_path.is_file()
    assert preview_path is None
    audit = json.loads(
        (tmp_path / "review" / "object_selection.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["included_ids"] == ["review_weak_000"]
    assert audit["excluded_ids"] == ["table_000"]


def test_sfm_assisted_map25d_writes_trial_metadata_and_preview(
    fixture_dir,
    tmp_path,
):
    """SfM 只筛选视觉相机，2.5D 仍明确记录平面定位和固定高度来源。"""

    from store_vision.data import load_store_folder
    from store_vision.mapping.sfm_assisted import run_sfm_assisted_map25d

    dataset = load_store_folder(fixture_dir)
    image_names = [
        Path(camera.image_path).name
        for camera in dataset.camera_list()
        if camera.image_path
    ]
    first = next(camera for camera in dataset.camera_list() if camera.image_size)
    width, height = first.image_size
    focal = 0.85 * max(width, height)
    reports = tmp_path / "sfm" / "reports"
    reports.mkdir(parents=True)
    (reports / "sfm_intrinsics.json").write_text(
        json.dumps(
            {
                "dataset_path": str(fixture_dir.resolve()),
                "canonical_resolution": [width, height],
                "shared_K": [
                    [focal, 0.0, width / 2.0],
                    [0.0, focal, height / 2.0],
                    [0.0, 0.0, 1.0],
                ],
                "shared_D": [0.01, -0.001, 0.0001, -0.00001],
                "routing_source": "fitted",
                "credible_calibration": True,
            }
        ),
        encoding="utf-8",
    )
    (reports / "sfm_registration_summary.json").write_text(
        json.dumps(
            {
                "status": "all_camera_visual",
                "selected_candidate": "global",
                "full_visual_registration": True,
                "visually_registered_images": image_names,
                "per_image_point3d_observations": {
                    name: 20 for name in image_names
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_sfm_assisted_map25d(
        fixture_dir,
        reports.parent,
        tmp_path / "trial",
    )

    assert result.preview_path.is_file()
    assert (result.output_dir / "map25d_sfm_trial.geojson").is_file()
    assert (
        result.output_dir / "reports" / "map25d_sfm_trial_summary.json"
    ).is_file()
    assert result.summary["sfm_registered_camera_count"] == len(image_names)
    assert (
        result.map25d["metadata"]["trust_level"]
        == "observation_supported_not_independently_validated"
    )
    assert "SfM 只用于确认" in result.summary["limitations"][0]
