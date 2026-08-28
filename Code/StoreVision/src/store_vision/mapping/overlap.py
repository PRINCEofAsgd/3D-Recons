"""Pairwise camera overlap computation on the floor plane."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from store_vision.data.models import StoreDataset
from store_vision.geometry.projection import apply_homography


def _camera_floor_polygon(calib, fw: int, fh: int) -> np.ndarray:
    """Project image rectangle through H -> floor px polygon (clipped to plan)."""
    if calib.homography is None or not calib.image_path:
        return np.empty((0, 2))
    img = cv2.imread(calib.image_path)
    if img is None:
        return np.empty((0, 2))
    h, w = img.shape[:2]
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    floor = apply_homography(calib.homography, corners)
    floor[:, 0] = np.clip(floor[:, 0], 0, fw)
    floor[:, 1] = np.clip(floor[:, 1], 0, fh)
    return floor


def _polygon_intersection_area(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    ret, inter = cv2.intersectConvexConvex(
        a.astype(np.float32), b.astype(np.float32)
    )
    if ret <= 0 or inter is None:
        return 0.0
    return float(cv2.contourArea(inter))


def compute_overlaps(
    dataset: StoreDataset,
    out_dir: Path | None = None,
) -> dict:
    if not dataset.floor_plan_size:
        return {"pairs": []}
    fw, fh = dataset.floor_plan_size

    cams = [c for c in dataset.camera_list() if c.homography is not None and c.image_path]
    poly_per_cam = {c.device_serial: _camera_floor_polygon(c, fw, fh) for c in cams}

    pairs: list[dict] = []
    for i, c1 in enumerate(cams):
        for c2 in cams[i + 1 :]:
            a = poly_per_cam[c1.device_serial]
            b = poly_per_cam[c2.device_serial]
            inter_area = _polygon_intersection_area(a, b)
            if inter_area <= 0:
                continue
            pairs.append(
                {
                    "a": c1.device_serial,
                    "b": c2.device_serial,
                    "intersection_area_floor_px": inter_area,
                    "a_floor_polygon": a.tolist(),
                    "b_floor_polygon": b.tolist(),
                }
            )

    summary = {
        "floor_size": [fw, fh],
        "n_cameras": len(cams),
        "n_pairs_overlapping": len(pairs),
        "pairs": pairs,
        "per_camera_floor_polygon": {k: v.tolist() for k, v in poly_per_cam.items()},
    }

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "overlaps.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        # Visualize on a blank canvas.
        canvas = np.zeros((fh, fw, 3), dtype=np.uint8)
        colors = [
            (255, 80, 80), (80, 255, 80), (80, 80, 255),
            (255, 255, 80), (255, 80, 255), (80, 255, 255),
            (200, 150, 80), (150, 80, 200), (80, 200, 150),
        ]
        for i, c in enumerate(cams):
            poly = poly_per_cam[c.device_serial]
            if poly.size == 0:
                continue
            cv2.polylines(canvas, [poly.astype(np.int32)], True, colors[i % len(colors)], 2)
        for p in pairs:
            a = np.array(p["a_floor_polygon"], dtype=np.float32)
            b = np.array(p["b_floor_polygon"], dtype=np.float32)
            ret, inter = cv2.intersectConvexConvex(a, b)
            if ret > 0 and inter is not None:
                cv2.fillPoly(canvas, [inter.astype(np.int32)], (255, 255, 255))
        cv2.imwrite(str(out_dir / "overlap_vis.jpg"), canvas)
    return summary
