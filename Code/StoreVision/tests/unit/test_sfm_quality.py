"""SfM 候选必须由真实三维观测而非空壳位姿验收。"""

from __future__ import annotations

from store_vision.calibration.sfm_quality import (
    evaluate_sfm_candidate,
    select_best_sfm_candidate,
)


def _write_text_model(root, *, with_points):
    root.mkdir()
    (root / "cameras.txt").write_text(
        "1 OPENCV_FISHEYE 1000 800 400 400 500 400 0 0 0 0\n",
        encoding="utf-8",
    )
    image_lines = []
    point_ids = list(range(1, 9)) if with_points else []
    observations = " ".join(
        f"{point_id}.0 {point_id}.0 {point_id}" for point_id in point_ids
    )
    for image_id in range(1, 4):
        image_lines.extend(
            [
                f"{image_id} 1 0 0 0 0 0 0 1 camera-{image_id}.jpg",
                observations,
            ]
        )
    (root / "images.txt").write_text(
        "\n".join(image_lines) + "\n", encoding="utf-8"
    )
    points = [
        f"{point_id} {point_id} 0 1 255 255 255 0.5 "
        f"1 {point_id - 1} 2 {point_id - 1} 3 {point_id - 1}"
        for point_id in point_ids
    ]
    (root / "points3D.txt").write_text(
        "\n".join(points) + ("\n" if points else ""),
        encoding="utf-8",
    )


def test_all_camera_visual_candidate_passes(tmp_path):
    model = tmp_path / "model"
    _write_text_model(model, with_points=True)

    quality = evaluate_sfm_candidate(
        "global",
        model,
        model,
        [f"camera-{index}.jpg" for index in range(1, 4)],
    )

    assert quality.status == "all_camera_visual"
    assert quality.full_visual_registration
    assert quality.total_point3d_observations == 24
    assert all(value == 8 for value in quality.per_image_point3d_observations.values())


def test_registered_images_without_points_never_win(tmp_path):
    empty_model = tmp_path / "empty"
    visual_model = tmp_path / "visual"
    _write_text_model(empty_model, with_points=False)
    _write_text_model(visual_model, with_points=True)
    names = [f"camera-{index}.jpg" for index in range(1, 4)]
    empty = evaluate_sfm_candidate("empty", empty_model, empty_model, names)
    visual = evaluate_sfm_candidate("visual", visual_model, visual_model, names)

    assert empty.status == "registered_without_geometry"
    assert not empty.accepted
    assert select_best_sfm_candidate([empty, visual]) == visual
