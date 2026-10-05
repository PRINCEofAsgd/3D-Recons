"""Geometry helpers: H apply, polygon containment, nearest-camera search."""

from __future__ import annotations

import cv2
import numpy as np

from store_vision.data.models import CameraCalibration, StoreDataset


def apply_homography(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply 3x3 H to Nx2 points."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
    return out


def polygon_centroid(poly: list[tuple[float, float]] | np.ndarray) -> tuple[float, float]:
    arr = np.asarray(poly, dtype=np.float64)
    if arr.size == 0:
        return (0.0, 0.0)
    return (float(arr[:, 0].mean()), float(arr[:, 1].mean()))


def point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > py) != (y2 > py)) and (
            px < (x2 - x1) * (py - y1) / max(y2 - y1, 1e-9) + x1
        ):
            inside = not inside
    return inside


def nearest_camera_to_floor_pt(
    dataset: StoreDataset,
    fx: float,
    fy: float,
) -> CameraCalibration | None:
    """Return camera whose mapPoints centroid is closest to (fx, fy) in floor pixel."""
    if not dataset.floor_plan_size or not dataset.cameras:
        return list(dataset.cameras.values())[0] if dataset.cameras else None
    fw, fh = dataset.floor_plan_size
    best: CameraCalibration | None = None
    best_d = float("inf")
    for c in dataset.camera_list():
        pts = [(p.x / 100.0 * fw, p.y / 100.0 * fh) for p in c.map_points]
        if point_in_polygon(fx, fy, pts):
            return c
        cx, cy = polygon_centroid(pts)
        d = (cx - fx) ** 2 + (cy - fy) ** 2
        if d < best_d:
            best_d, best = d, c
    return best


def best_camera_for_floor_polygon(
    dataset: StoreDataset,
    floor_polygon: list[tuple[float, float]],
    image_sizes: dict[str, tuple[int, int]] | None = None,
) -> tuple[CameraCalibration, float] | None:
    """Pick the camera whose back-projected image polygon centroid is closest to
    the camera's image center.

    Multiple cameras can cover the same floor area; the best view is the one
    where the area appears closest to the image center (least lens distortion,
    best resolution, smallest perspective foreshortening).

    Args:
        dataset: the loaded store dataset (with calibrated homographies).
        floor_polygon: polygon in floor-plan pixels.
        image_sizes: optional ``{device_serial: (W, H)}`` cache. If a camera's
            size is missing the function reads its image lazily.

    Returns:
        ``(camera, score)`` where ``score`` is the normalized distance from the
        image-polygon centroid to the image center (0 = center, ~0.5 = corner).
        Returns ``None`` if no camera has a valid back-projection.
    """
    from store_vision.calibration.homography import floor_polygon_to_camera_polygon

    if not floor_polygon or len(floor_polygon) < 3:
        return None
    image_sizes = dict(image_sizes or {})
    candidates: list[tuple[float, CameraCalibration]] = []
    fallback: list[tuple[float, CameraCalibration]] = []
    for calib in dataset.camera_list():
        if calib.homography_inv is None:
            continue
        cam_poly = floor_polygon_to_camera_polygon(calib, floor_polygon)
        if cam_poly is None or len(cam_poly) < 3:
            continue
        size = image_sizes.get(calib.device_serial)
        if size is None and calib.image_path:
            img = cv2.imread(calib.image_path)
            if img is not None:
                size = (img.shape[1], img.shape[0])
                image_sizes[calib.device_serial] = size
        if size is None:
            continue
        W, H = size
        cx = float(np.mean(cam_poly[:, 0]))
        cy = float(np.mean(cam_poly[:, 1]))
        ix, iy = W / 2.0, H / 2.0
        diag = float(np.hypot(W, H))
        score = float(np.hypot(cx - ix, cy - iy)) / max(diag, 1.0)
        # In-frame cameras (centroid lies inside the actual image) are strongly
        # preferred over those that only happen to project nearby.
        in_frame = 0.0 <= cx <= W and 0.0 <= cy <= H
        if in_frame:
            candidates.append((score, calib))
        else:
            fallback.append((score, calib))
    pool = candidates or fallback
    if not pool:
        return None
    pool.sort(key=lambda x: x[0])
    score, cam = pool[0]
    return cam, score
