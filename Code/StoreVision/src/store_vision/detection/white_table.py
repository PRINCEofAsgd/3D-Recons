"""White-tabletop detector — strict, calibration-aware.

Why not a hard image-space mask?
--------------------------------
The 4 calibration points cover only a small floor patch *between* the tables;
in fish-eye views the actual tables sit at the image periphery. So we cannot
mask away "outside the calibration quad" in image space without throwing the
tables out. Instead the gate is on the **floor side**:

Pipeline (per camera, after overlay-line removal):

    1. Build a "white" mask combining HSV (V high, S low) and LAB neutrality
       (L high, |a-128|, |b-128| small). Both must agree → robust against
       light-wood floors, glossy walls, magenta/cyan overlay lines, etc.
    2. Morphological close (large kernel ~21px) to merge tabletop fragments
       carved up by laptops/devices on top, then open to drop specks.
    3. Crop the mask to a soft image-fov ring so we don't waste cycles on the
       black borders of the fish-eye lens.
    4. For every connected component:
        a. Fit a rotated rectangle (``cv2.minAreaRect``).
        b. Compute *rectangularity* = component_area / rect_area.
        c. Undistort the corners, then use K/R/t to intersect the configured
           tabletop-height plane; existing H anchors world XY to floor pixels.
        d. Sanity gates:
            - Size in cm within ``cfg.table_size_cm_range`` (kills sky/floor)
            - Centroid lies inside the floor plan
            - Centroid lies within ``table_floor_gate_cm`` of the camera's
              calibrated floor patch (kills far-away projections)
        e. Aggregate sub-scores; accept if score >= threshold.
    5. The accepted polygon is the *rectangle*, not the noisy contour — much
       cleaner for downstream alignment / 2.5D extrusion.

Diagnostic outputs (written to ``out_dir``):

    * ``<serial>_stage1_white.jpg`` — raw white mask
    * ``<serial>_stage2_merged.jpg`` — closed/opened white mask
    * ``<serial>_candidates.jpg`` — final visualization
    * ``<serial>_candidates.json`` — full candidate list (accepted + rejected)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from store_vision.calibration.homography import image_polygon_to_height_plane_px
from store_vision.config import StoreConfig
from store_vision.data.models import CameraCalibration
from store_vision.resolution import scaled_pixel_value
from store_vision.geometry.coords import floor_px_to_cm
from store_vision.geometry.projection import polygon_centroid


@dataclass
class TableCandidate:
    accepted: bool
    score: float
    sub_scores: dict[str, float]
    polygon_img: list[tuple[float, float]]  # rotated-rectangle corners
    polygon_floor_px: list[tuple[float, float]]
    polygon_cm: list[tuple[float, float]]
    bbox_cm_size: tuple[float, float]
    reason: str = ""
    projection_method: str = "unknown"
    projection_height_cm: float = 0.0


# ---------------------------------------------------------------------- masks
def _fisheye_fov_mask(img_bgr: np.ndarray, cfg: StoreConfig) -> np.ndarray:
    """Coarse mask of the bright pixel area (drops the black fish-eye border)."""
    h, w = img_bgr.shape[:2]
    image_size = (w, h)
    open_size = scaled_pixel_value(
        9,
        image_size,
        reference_long_side=cfg.detection_reference_long_side_px,
        minimum=3,
        odd=True,
    )
    close_size = scaled_pixel_value(
        25,
        image_size,
        reference_long_side=cfg.detection_reference_long_side_px,
        minimum=5,
        odd=True,
    )
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    fov = (gray > 12).astype(np.uint8) * 255
    fov = cv2.morphologyEx(
        fov, cv2.MORPH_OPEN, np.ones((open_size, open_size), np.uint8)
    )
    fov = cv2.morphologyEx(
        fov, cv2.MORPH_CLOSE, np.ones((close_size, close_size), np.uint8)
    )
    return fov


def _camera_floor_gate(
    calib: CameraCalibration,
    fw: int,
    fh: int,
    expand_cm: float,
    cfg: StoreConfig,
) -> np.ndarray | None:
    """Convex polygon (in floor pixels) where this camera is *expected* to
    see things — the calibrated mapPoints, expanded by ``expand_cm``."""
    if not calib.map_points:
        return None
    pts = np.array(
        [[p.x / 100.0 * fw, p.y / 100.0 * fh] for p in calib.map_points],
        dtype=np.float64,
    )
    cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
    # Convert expand from cm → floor pixels (use mean px/cm).
    px_per_cm_x = fw / max(cfg.floor_plan_width_cm, 1)
    px_per_cm_y = fh / max(cfg.floor_plan_height_cm, 1)
    px_per_cm = float(np.hypot(px_per_cm_x, px_per_cm_y) / np.sqrt(2))
    pad = expand_cm * px_per_cm
    # Expand each vertex outward from centroid by `pad` pixels.
    vec = pts - [cx, cy]
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    norms = np.where(norms < 1e-3, 1.0, norms)
    expanded = pts + vec / norms * pad
    return expanded


def _point_in_convex(p: np.ndarray, poly: np.ndarray) -> bool:
    return cv2.pointPolygonTest(poly.astype(np.float32), (float(p[0]), float(p[1])), False) >= 0


def _white_mask(img_bgr: np.ndarray, cfg: StoreConfig) -> np.ndarray:
    """Combined HSV + LAB whiteness predicate."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    v = hsv[..., 2]
    s = hsv[..., 1]
    L = lab[..., 0]
    a = lab[..., 1].astype(np.int16)
    b = lab[..., 2].astype(np.int16)
    chroma_ok = (np.abs(a - 128) <= cfg.white_lab_chroma_max) & (
        np.abs(b - 128) <= cfg.white_lab_chroma_max
    )
    bright_ok = (v >= cfg.white_v_min) | (L >= cfg.white_lab_l_min)
    sat_ok = s <= cfg.white_s_max
    mask = (chroma_ok & bright_ok & sat_ok).astype(np.uint8) * 255
    return mask


def _merge_close(mask: np.ndarray, kernel_px: int, open_kernel_px: int) -> np.ndarray:
    """Close fragmented white blobs into single components, then drop specks.
    Kernel size scales with image resolution but never below 7px."""
    k = max(7, int(kernel_px) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    opened = cv2.morphologyEx(
        closed,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT, (open_kernel_px, open_kernel_px)
        ),
        iterations=1,
    )
    return opened


# --------------------------------------------------------------------- score
def _score_candidate(
    contour: np.ndarray,
    component_mask: np.ndarray,
    img_bgr: np.ndarray,
    calib: CameraCalibration,
    cfg: StoreConfig,
    floor_w: int,
    floor_h: int,
    floor_gate: np.ndarray | None,
) -> TableCandidate:
    sub: dict[str, float] = {}

    # ---- rotated-rectangle fit (this becomes the table polygon) -----------
    rect = cv2.minAreaRect(contour)
    box_pts = cv2.boxPoints(rect)
    box_pts = box_pts.astype(np.float64)

    contour_area = float(cv2.contourArea(contour))
    rect_area = float(cv2.contourArea(box_pts.astype(np.float32)))
    if rect_area < 1.0:
        return TableCandidate(False, 0.0, {}, [], [], [], (0, 0), "degenerate")
    sub["rectangularity"] = float(np.clip(contour_area / rect_area, 0.0, 1.0))

    # ---- whiteness inside the polygon (use original color image) ----------
    poly_mask = np.zeros(img_bgr.shape[:2], dtype=np.uint8)
    cv2.fillConvexPoly(poly_mask, box_pts.astype(np.int32), 255)
    inside = poly_mask & component_mask
    region = img_bgr[inside > 0]
    if region.size == 0:
        return TableCandidate(False, 0.0, sub, [], [], [], (0, 0), "empty")
    hsv = cv2.cvtColor(region.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    v_mean = float(hsv[:, 2].mean())
    s_mean = float(hsv[:, 1].mean())
    sub["whiteness"] = float(
        np.clip((v_mean - 180) / 60.0, 0, 1)
        * np.clip(1.0 - s_mean / 50.0, 0, 1)
    )

    # ---- 指定桌面高度上的尺寸和位置（K/D/R/t + 平面图 H 锚点）-----------
    floor_px, projection_method = image_polygon_to_height_plane_px(
        calib,
        box_pts,
        cfg.table_height_cm / 100.0,
    )
    if floor_px.size == 0:
        return TableCandidate(False, 0.0, sub, box_pts.tolist(), [], [], (0, 0), "no_H")
    cm = floor_px_to_cm(floor_px, floor_w, floor_h, cfg)
    edges = [float(np.linalg.norm(cm[(i + 1) % 4] - cm[i])) for i in range(4)]
    short = min(edges[0], edges[1])
    long_side = max(edges[0], edges[1])
    s_lo, s_hi = cfg.table_size_cm_range
    in_range = (s_lo <= short <= s_hi) and (s_lo <= long_side <= s_hi)
    sub["size"] = 1.0 if in_range else 0.0

    aspect = long_side / max(short, 1e-3)
    sub["shape"] = float(np.clip(1.0 - (aspect - 1.0) / cfg.table_aspect_max, 0, 1))

    hull = cv2.convexHull(contour)
    a_hull = float(cv2.contourArea(hull))
    cvx = contour_area / max(a_hull, 1e-3)
    sub["convexity"] = float(np.clip((cvx - cfg.table_convexity_min) / 0.15, 0, 1))

    # ---- floor-side gate: centroid must lie inside floor plan AND inside
    #      the camera's calibrated floor patch (expanded). ------------------
    centroid_px = floor_px.mean(axis=0)
    on_plan = (0 <= centroid_px[0] < floor_w) and (0 <= centroid_px[1] < floor_h)
    in_gate = floor_gate is None or _point_in_convex(centroid_px, floor_gate)
    sub["position"] = 1.0 if (on_plan and in_gate) else 0.0

    # ---- final score ------------------------------------------------------
    score = (
        0.30 * sub["rectangularity"]
        + 0.20 * sub["size"]
        + 0.20 * sub["whiteness"]
        + 0.15 * sub["shape"]
        + 0.10 * sub["position"]
        + 0.05 * sub["convexity"]
    )

    accept = (
        score >= cfg.table_score_threshold
        and sub["rectangularity"] >= cfg.table_rectangularity_min
        and in_range
        and on_plan
        and in_gate
    )
    if not accept:
        if not on_plan:
            reason = "off_plan"
        elif not in_gate:
            reason = "outside_camera_gate"
        elif not in_range:
            reason = f"size_out short={short:.0f}cm long={long_side:.0f}cm"
        elif sub["rectangularity"] < cfg.table_rectangularity_min:
            reason = f"not_rectangular {sub['rectangularity']:.2f}"
        else:
            reason = f"score_below {score:.2f}"
    else:
        reason = "ok"

    return TableCandidate(
        # OpenCV/NumPy 比较会产生 np.bool_；显式转为 Python bool，保证逐机
        # 候选报告可 JSON 序列化，避免单台相机写报告失败后整轮 2.5D 变为空。
        accepted=bool(accept),
        score=float(score),
        sub_scores={k: float(v) for k, v in sub.items()},
        polygon_img=[(float(p[0]), float(p[1])) for p in box_pts],
        polygon_floor_px=[(float(p[0]), float(p[1])) for p in floor_px],
        polygon_cm=[(float(p[0]), float(p[1])) for p in cm],
        bbox_cm_size=(float(short), float(long_side)),
        reason=reason,
        projection_method=projection_method,
        projection_height_cm=float(cfg.table_height_cm),
    )


# ------------------------------------------------------------------- driver
def detect_white_tables(
    img_bgr: np.ndarray,
    calib: CameraCalibration,
    cfg: StoreConfig,
    floor_w: int,
    floor_h: int,
    out_dir: Path | None = None,
) -> list[TableCandidate]:
    """Run table detection on a single inpainted (overlay-clean) camera image."""
    image_size = (img_bgr.shape[1], img_bgr.shape[0])
    fov = _fisheye_fov_mask(img_bgr, cfg)
    raw_white = _white_mask(img_bgr, cfg)
    raw_white = cv2.bitwise_and(raw_white, fov)
    close_kernel = scaled_pixel_value(
        cfg.table_close_kernel_px,
        image_size,
        reference_long_side=cfg.detection_reference_long_side_px,
        minimum=7,
        odd=True,
    )
    open_kernel = scaled_pixel_value(
        5,
        image_size,
        reference_long_side=cfg.detection_reference_long_side_px,
        minimum=3,
        odd=True,
    )
    merged = _merge_close(raw_white, close_kernel, open_kernel)
    floor_gate = _camera_floor_gate(
        calib, floor_w, floor_h, cfg.table_floor_gate_cm, cfg
    )

    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    candidates: list[TableCandidate] = []
    min_area_px = max(800, int(0.0008 * img_bgr.shape[0] * img_bgr.shape[1]))
    for lab_idx in range(1, n_lab):
        area = int(stats[lab_idx, cv2.CC_STAT_AREA])
        if area < min_area_px:
            continue
        comp = (labels == lab_idx).astype(np.uint8) * 255
        contours, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        cand = _score_candidate(
            cnt, comp, img_bgr, calib, cfg, floor_w, floor_h, floor_gate
        )
        candidates.append(cand)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_dir / f"{calib.device_serial}_stage1_white.jpg"), raw_white)
        cv2.imwrite(str(out_dir / f"{calib.device_serial}_stage2_merged.jpg"), merged)
        vis = img_bgr.copy()
        cv2.polylines(
            vis, [calib.camera_points_array().astype(np.int32)], True, (0, 200, 200), 2
        )
        for c in candidates:
            color = (0, 255, 0) if c.accepted else (0, 0, 255)
            poly = np.array(c.polygon_img, dtype=np.int32)
            cv2.polylines(vis, [poly], True, color, 3)
            cx, cy = polygon_centroid(c.polygon_img)
            label = f"{c.score:.2f} {c.bbox_cm_size[0]:.0f}x{c.bbox_cm_size[1]:.0f}cm"
            cv2.putText(
                vis, label, (int(cx) - 60, int(cy)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
            )
        cv2.imwrite(str(out_dir / f"{calib.device_serial}_candidates.jpg"), vis)
        with open(out_dir / f"{calib.device_serial}_candidates.json", "w", encoding="utf-8") as f:
            json.dump([asdict(c) for c in candidates], f, indent=2)

    return candidates
