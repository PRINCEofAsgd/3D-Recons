"""Remove algorithm-drawn overlay lines (cyan/magenta/etc) before detection.

Strategy
--------
1. Strong-saturation HSV mask -> candidate overlay pixels.
2. Hough line detection on that mask -> long thin lines.
3. Also burn-in cali.txt-derived overlay polygons (recxysets / linexysets) - we
   know exactly where they were drawn.
4. cv2.inpaint(TELEA) restores realistic background.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from store_vision.config import StoreConfig
from store_vision.data.models import CameraCalibration
from store_vision.resolution import scaled_pixel_value


def _saturation_mask(img_bgr: np.ndarray, s_min: int) -> np.ndarray:
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1]
    v = hsv[..., 2]
    mask = ((s > s_min) & (v > 60)).astype(np.uint8) * 255
    return mask


def _hough_line_mask(
    sat_mask: np.ndarray,
    min_len: int,
    max_gap: int,
    line_thickness: int,
    vote_threshold: int,
) -> np.ndarray:
    edges = cv2.Canny(sat_mask, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=vote_threshold,
        minLineLength=min_len,
        maxLineGap=max_gap,
    )
    out = np.zeros_like(sat_mask)
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            cv2.line(
                out,
                (x1, y1),
                (x2, y2),
                255,
                thickness=line_thickness,
            )
    return out


def _polygon_mask(
    polys: list[list],
    shape: tuple[int, int],
    thickness: int = 6,
) -> np.ndarray:
    mask = np.zeros(shape[:2], dtype=np.uint8)
    for poly in polys:
        if len(poly) < 2:
            continue
        pts = np.array([[p.x, p.y] for p in poly], dtype=np.int32)
        cv2.polylines(mask, [pts], isClosed=True, color=255, thickness=thickness)
    return mask


def remove_overlay_lines(
    img_bgr: np.ndarray,
    calib: CameraCalibration,
    cfg: StoreConfig,
    out_dir: Path | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (clean_image, total_overlay_mask)."""
    image_size = (img_bgr.shape[1], img_bgr.shape[0])
    reference = cfg.detection_reference_long_side_px
    line_min = scaled_pixel_value(
        cfg.overlay_line_min_len_px,
        image_size,
        reference_long_side=reference,
        minimum=12,
    )
    polygon_thickness = scaled_pixel_value(
        6, image_size, reference_long_side=reference, minimum=2
    )
    hough_thickness = scaled_pixel_value(
        4, image_size, reference_long_side=reference, minimum=2
    )
    hough_gap = scaled_pixel_value(
        8, image_size, reference_long_side=reference, minimum=2
    )
    hough_votes = scaled_pixel_value(
        40, image_size, reference_long_side=reference, minimum=10
    )
    dilation = scaled_pixel_value(
        3,
        image_size,
        reference_long_side=reference,
        minimum=1,
        odd=True,
    )
    inpaint_radius = scaled_pixel_value(
        cfg.overlay_inpaint_radius,
        image_size,
        reference_long_side=reference,
        minimum=1,
    )
    sat = _saturation_mask(img_bgr, cfg.overlay_min_saturation)
    line_mask = _hough_line_mask(
        sat, line_min, hough_gap, hough_thickness, hough_votes
    )
    poly_mask = _polygon_mask(
        calib.overlay_polygons_img, img_bgr.shape, thickness=polygon_thickness
    )
    total = cv2.bitwise_or(line_mask, poly_mask)
    total = cv2.dilate(
        total, np.ones((dilation, dilation), np.uint8), iterations=1
    )

    clean = cv2.inpaint(
        img_bgr, total, inpaint_radius, cv2.INPAINT_TELEA
    )

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_dir / f"{calib.device_serial}_overlay_mask.jpg"), total)
        cv2.imwrite(str(out_dir / f"{calib.device_serial}_clean.jpg"), clean)
    return clean, total
