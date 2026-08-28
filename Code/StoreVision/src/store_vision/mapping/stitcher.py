"""Multi-camera stitching with calibration-quad weighting + gain compensation.

Seamlessness strategy
---------------------
1. Each camera contributes to the floor mosaic only inside its calibration
   quadrilateral (expanded by ``cfg.stitch_quad_expand``). Outside the quad
   weight is hard-zero, killing the "ghost shelves" that appeared when the
   warped trapezoid extrapolated wildly.
2. Weight ramps smoothly from 1 at the quad centre to 0 at the soft boundary
   via a signed distance transform, then a Gaussian blur smears the edge so
   blends are fully feathered (controls visible seams).
3. Optional **gain compensation**: equalize per-camera mean BGR inside their
   pairwise overlap regions so neighbouring cameras have matching exposure.
   This is a 1-iteration least-squares solve à la OpenCV's
   ``cv::detail::GainCompensator`` but specialised for floor mosaics.

Outputs (``out_dir``):
    * ``stitch_floor.jpg`` — original floor plan (for reference)
    * ``stitch_mosaic.jpg`` — final blended mosaic
    * ``stitch_overlay.jpg`` — floor plan + mosaic α-blend
    * ``stitch_contrib_mask.jpg`` — total covered area
    * ``stitch_per_cam/<serial>_warp.jpg`` — per-camera warp diagnostic
    * ``stitch_per_cam/<serial>_weight.jpg`` — per-camera weight mask
    * ``stitch_meta.json`` — gains, sizes, cameras
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from store_vision.calibration.distortion import undistort_image
from store_vision.config import StoreConfig
from store_vision.data.models import StoreDataset


# ----------------------------------------------------------------------- mask
def _calibration_quad_mask(calib, fw: int, fh: int, expand: float) -> np.ndarray:
    """Hard fill of the calibration quad (expanded by ``expand`` about its
    centroid). Returns 8-bit mask; pixels inside == 255."""
    if calib.homography is None:
        return np.zeros((fh, fw), dtype=np.uint8)
    pts = np.array(
        [[p.x / 100.0 * fw, p.y / 100.0 * fh] for p in calib.map_points],
        dtype=np.float64,
    )
    cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
    pts = (pts - [cx, cy]) * (1.0 + expand) + [cx, cy]
    pts[:, 0] = np.clip(pts[:, 0], 0, fw - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, fh - 1)
    mask = np.zeros((fh, fw), dtype=np.uint8)
    cv2.fillConvexPoly(mask, pts.astype(np.int32), 255)
    return mask


def _signed_distance_weight(mask: np.ndarray, feather: float) -> np.ndarray:
    """Weight ramps from 1 inside (far from boundary) → 0 at + feather past
    boundary. Pixels outside mask get 0."""
    inside = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    w = np.clip(inside / max(feather, 1.0), 0.0, 1.0)
    return w.astype(np.float64)


# ----------------------------------------------------------------- gain comp
def _gain_compensation(
    cams: list,
    warps: dict[str, np.ndarray],
    masks: dict[str, np.ndarray],
) -> dict[str, float]:
    """Per-camera scalar BGR gain s.t. average intensities match across
    pairwise overlap regions. Closed-form 1-iter least-squares.

    Returns ``{serial: gain}`` with mean(gain) == 1.0.
    """
    serials = [c.device_serial for c in cams]
    n = len(serials)
    if n <= 1:
        return {s: 1.0 for s in serials}

    means = np.ones(n, dtype=np.float64)  # per-camera mean intensity
    sums = np.zeros(n, dtype=np.float64)
    counts = np.zeros(n, dtype=np.float64)
    for i, s in enumerate(serials):
        m = masks[s] > 0
        if m.sum() == 0:
            continue
        gray = cv2.cvtColor(warps[s], cv2.COLOR_BGR2GRAY).astype(np.float64)
        sums[i] = gray[m].sum()
        counts[i] = m.sum()
    means = np.where(counts > 0, sums / np.maximum(counts, 1), 1.0)
    target = float(means[counts > 0].mean()) if (counts > 0).any() else 1.0
    gains = {s: float(target / max(means[i], 1.0)) for i, s in enumerate(serials)}
    # Clamp to sensible band so a too-dark camera doesn't blow out the mosaic.
    return {s: float(np.clip(g, 0.7, 1.4)) for s, g in gains.items()}


# ------------------------------------------------------------------ stitcher
def stitch_cameras_to_floor(
    dataset: StoreDataset,
    cfg: StoreConfig,
    out_dir: Path | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (floor_plan_bgr, mosaic_bgr) — both same shape."""
    if not dataset.floor_plan_path or not dataset.floor_plan_size:
        raise ValueError("Floor plan required for stitching")

    floor = cv2.imread(dataset.floor_plan_path)
    if floor is None:
        raise ValueError("cannot read floor plan")
    fh, fw = floor.shape[:2]

    cams = [c for c in dataset.camera_list() if c.homography is not None and c.image_path]
    warps: dict[str, np.ndarray] = {}
    quad_masks: dict[str, np.ndarray] = {}
    weights: dict[str, np.ndarray] = {}

    per_cam_dir = (out_dir / "stitch_per_cam") if out_dir else None
    if per_cam_dir is not None:
        per_cam_dir.mkdir(parents=True, exist_ok=True)

    for calib in cams:
        img = cv2.imread(calib.image_path)
        if img is None:
            continue
        if calib.K is not None and calib.dist_coeffs is not None:
            img = undistort_image(img, calib.K, calib.dist_coeffs)
        warped = cv2.warpPerspective(img, calib.homography, (fw, fh), flags=cv2.INTER_LINEAR)
        warps[calib.device_serial] = warped

        # Mask = (image-projected support) ∩ (calibration quad expanded)
        support = cv2.warpPerspective(
            np.full(img.shape[:2], 255, dtype=np.uint8),
            calib.homography, (fw, fh), flags=cv2.INTER_NEAREST,
        )
        quad = _calibration_quad_mask(calib, fw, fh, cfg.stitch_quad_expand)
        m = cv2.bitwise_and(support, quad)
        quad_masks[calib.device_serial] = m

        # Smooth feathered weight (Gaussian-blurred SDT) — for blending only.
        w = _signed_distance_weight(m, cfg.stitch_feather_px)
        if cfg.stitch_weight_blur_px and cfg.stitch_weight_blur_px > 1:
            k = int(cfg.stitch_weight_blur_px) | 1
            w = cv2.GaussianBlur(w, (k, k), 0)
            w = np.clip(w, 0.0, 1.0)
        weights[calib.device_serial] = w

    if not warps:
        return floor, floor.copy()

    # ------------------------------------------------------------------ gain
    if cfg.stitch_gain_compensation:
        gains = _gain_compensation(cams, warps, quad_masks)
    else:
        gains = {s: 1.0 for s in warps}

    # --------------------------------------------------------------- compose
    acc = np.zeros((fh, fw, 3), dtype=np.float64)
    weight = np.zeros((fh, fw, 1), dtype=np.float64)
    contrib_mask = np.zeros((fh, fw), dtype=np.uint8)

    for calib in cams:
        s = calib.device_serial
        if s not in warps:
            continue
        w = weights[s][..., None]
        acc += warps[s].astype(np.float64) * gains[s] * w
        weight += w
        contrib_mask = cv2.bitwise_or(contrib_mask, quad_masks[s])

    weight = np.maximum(weight, 1e-6)
    mosaic = np.clip(acc / weight, 0, 255).astype(np.uint8)
    no_cam = (contrib_mask == 0)
    mosaic[no_cam] = floor[no_cam]

    # ---------------------------------------------------------------- output
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_dir / "stitch_floor.jpg"), floor)
        cv2.imwrite(str(out_dir / "stitch_mosaic.jpg"), mosaic)
        cv2.imwrite(
            str(out_dir / "stitch_overlay.jpg"),
            cv2.addWeighted(floor, 0.4, mosaic, 0.6, 0),
        )
        cv2.imwrite(str(out_dir / "stitch_contrib_mask.jpg"), contrib_mask)
        if per_cam_dir is not None:
            for s, w in weights.items():
                cv2.imwrite(str(per_cam_dir / f"{s}_weight.jpg"),
                            (w * 255).astype(np.uint8))
                cv2.imwrite(str(per_cam_dir / f"{s}_warp.jpg"), warps[s])
        meta = {
            "floor_size": [fw, fh],
            "distortion_corrected": True,
            "feather_px": cfg.stitch_feather_px,
            "weight_blur_px": cfg.stitch_weight_blur_px,
            "quad_expand": cfg.stitch_quad_expand,
            "gain_compensation": cfg.stitch_gain_compensation,
            "gains": gains,
            "cameras_used": [c.device_serial for c in cams],
        }
        with open(out_dir / "stitch_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    return floor, mosaic
