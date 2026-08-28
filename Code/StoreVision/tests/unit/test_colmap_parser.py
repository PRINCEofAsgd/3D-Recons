from __future__ import annotations

import pytest

from store_vision.calibration.colmap_parser import parse_text_model


def test_parse_colmap_text_model(tmp_path):
    (tmp_path / "cameras.txt").write_text(
        "# Camera list\n1 PINHOLE 1920 1080 1000 1001 960 540\n",
        encoding="utf-8",
    )
    (tmp_path / "images.txt").write_text(
        "# Image list\n7 1 0 0 0 1 2 3 1 camera one.jpg\n10 20 -1 30 40 12\n",
        encoding="utf-8",
    )
    (tmp_path / "points3D.txt").write_text(
        "1 0 0 0 255 0 0 0.5 7 0\n2 1 1 1 0 255 0 1.5 7 1\n",
        encoding="utf-8",
    )
    summary = parse_text_model(tmp_path)
    assert summary.registered_images == 1
    assert summary.points3d == 2
    assert summary.mean_reprojection_error == pytest.approx(1.0)
    assert summary.cameras[1].params == (1000.0, 1001.0, 960.0, 540.0)
    assert summary.images[7].name == "camera one.jpg"
    assert summary.images[7].pose.camera_center() == pytest.approx((-1.0, -2.0, -3.0))


def test_parse_colmap_text_model_requires_all_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="incomplete"):
        parse_text_model(tmp_path)
