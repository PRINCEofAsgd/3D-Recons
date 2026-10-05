"""Alignment validation: floor-plan tables ↔ camera-image projections.

The req asks: *the position of tables in the floor plan and in the camera
images must agree, with small error.* This module produces a quantitative
report and a visual to verify exactly that.

For every detected ``MapObject25D`` (i.e. each accepted table):

    * Locate the **primary** camera that observed it (highest score).
    * Locate **all observing** cameras.
    * For each observing camera:
        - Image polygon (rotated rect from white-table detector)
        - Project that image polygon → floor-plan pixels via the camera's H
        - Compare to the canonical floor polygon stored on the object:
            * IoU (overlap)
            * Centroid distance in floor pixels and in cm
            * Hausdorff distance (max corner deviation)

Outputs:
    * ``alignment_report.json``  — structured numbers
    * ``alignment_overlay.jpg``  — floor plan with:
        - Camera footprints (faint coloured)
        - All projected image polygons (per camera, dashed)
        - Final canonical table (solid blue)
        - Centroid markers + per-table IoU label
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from store_vision.calibration.homography import image_polygon_to_floor_px
from store_vision.config import StoreConfig
from store_vision.data.models import MapObject25D, StoreDataset
from store_vision.geometry.coords import floor_px_to_cm

logger = logging.getLogger(__name__)


@dataclass
class TableAlignment:
    table_id: str
    primary_camera: str
    observed_by: list[str]
    canonical_centroid_cm: tuple[float, float]
    per_camera: list[dict]  # one entry per observing camera
    mean_centroid_err_cm: float
    max_centroid_err_cm: float
    mean_iou: float


def _polygon_iou(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape[0] < 3 or b.shape[0] < 3:
        return 0.0
    ret, inter = cv2.intersectConvexConvex(a.astype(np.float32), b.astype(np.float32))
    if ret <= 0 or inter is None or len(inter) < 3:
        return 0.0
    inter_a = float(cv2.contourArea(inter))
    union_a = (
        float(cv2.contourArea(a.astype(np.float32)))
        + float(cv2.contourArea(b.astype(np.float32)))
        - inter_a
    )
    if union_a <= 1e-3:
        return 0.0
    return inter_a / union_a


def _centroid(p: np.ndarray) -> np.ndarray:
    return p.mean(axis=0)


def _hausdorff(a: np.ndarray, b: np.ndarray) -> float:
    """Bidirectional max-min point distance."""
    if a.size == 0 or b.size == 0:
        return float("inf")
    d_ab = max(np.min(np.linalg.norm(b - p, axis=1)) for p in a)
    d_ba = max(np.min(np.linalg.norm(a - p, axis=1)) for p in b)
    return float(max(d_ab, d_ba))


def _cm_per_floor_px(dataset: StoreDataset, cfg: StoreConfig) -> tuple[float, float]:
    fw, fh = dataset.floor_plan_size or (1, 1)
    return cfg.floor_plan_width_cm / max(fw, 1), cfg.floor_plan_height_cm / max(fh, 1)


def compute_alignment_report(
    dataset: StoreDataset,
    objects: list[MapObject25D],
    cfg: StoreConfig,
    out_dir: Path | None = None,
) -> list[TableAlignment]:
    """Build the alignment report. Returns one TableAlignment per object."""
    if not dataset.floor_plan_size:
        return []
    fw, fh = dataset.floor_plan_size
    sx_cm, sy_cm = _cm_per_floor_px(dataset, cfg)

    cameras = {c.device_serial: c for c in dataset.camera_list()}

    reports: list[TableAlignment] = []
    for obj in objects:
        observations = obj.meta.get("observations") or []
        # Fallback to single-observation list when older meta is present.
        if not observations and obj.meta.get("polygon_floor_px"):
            observations = [{
                "camera": obj.source_camera,
                "polygon_floor_px": obj.meta["polygon_floor_px"],
                "polygon_cm": obj.polygon_cm,
            }]
        if not observations:
            continue

        # Canonical = element-wise mean of all observing cameras' floor
        # polygons in cm (4 ordered corners — minAreaRect ordering is stable
        # enough for averaging when corners are sorted by angle around the
        # centroid).
        polys_cm = []
        for obs in observations:
            p = np.asarray(obs.get("polygon_cm") or [], dtype=np.float64)
            if p.shape[0] < 3:
                continue
            cx, cy = p[:, 0].mean(), p[:, 1].mean()
            ang = np.arctan2(p[:, 1] - cy, p[:, 0] - cx)
            order = np.argsort(ang)
            polys_cm.append(p[order])
        if not polys_cm:
            continue
        # Pad/truncate all to 4 corners to allow elementwise mean.
        polys_cm = [p[:4] if len(p) >= 4 else np.vstack([p, p[:4 - len(p)]]) for p in polys_cm]
        canonical_cm = np.mean(np.stack(polys_cm), axis=0)
        canonical_centroid_cm = _centroid(canonical_cm)

        per_cam: list[dict] = []
        ious: list[float] = []
        cdists: list[float] = []
        for obs in observations:
            serial = obs.get("camera", "")
            if serial not in cameras:
                continue
            obs_px = np.asarray(obs.get("polygon_floor_px") or [], dtype=np.float64)
            obs_cm = np.asarray(obs.get("polygon_cm") or [], dtype=np.float64)
            if obs_px.shape[0] < 3 or obs_cm.shape[0] < 3:
                continue
            cx, cy = obs_cm[:, 0].mean(), obs_cm[:, 1].mean()
            ang = np.arctan2(obs_cm[:, 1] - cy, obs_cm[:, 0] - cx)
            obs_cm_sorted = obs_cm[np.argsort(ang)]
            iou = _polygon_iou(obs_cm_sorted, canonical_cm)
            cdist = float(np.linalg.norm(_centroid(obs_cm_sorted) - canonical_centroid_cm))
            haus_cm = _hausdorff(obs_cm_sorted, canonical_cm)
            per_cam.append({
                "camera": serial,
                "iou_with_canonical": iou,
                "centroid_err_cm": cdist,
                "hausdorff_cm": haus_cm,
                "projected_floor_px": obs_px.tolist(),
                "polygon_cm": obs_cm.tolist(),
            })
            ious.append(iou)
            cdists.append(cdist)

        reports.append(TableAlignment(
            table_id=obj.id,
            primary_camera=obj.source_camera or "",
            observed_by=[obs.get("camera", "") for obs in observations],
            canonical_centroid_cm=tuple(canonical_centroid_cm.tolist()),
            per_camera=per_cam,
            mean_centroid_err_cm=float(np.mean(cdists)) if cdists else 0.0,
            max_centroid_err_cm=float(np.max(cdists)) if cdists else 0.0,
            mean_iou=float(np.mean(ious)) if ious else 0.0,
        ))

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        floor = (
            cv2.imread(dataset.floor_plan_path)
            if dataset.floor_plan_path else np.full((fh, fw, 3), 240, dtype=np.uint8)
        )
        canvas = floor.copy()
        # Per-camera footprint outlines (faint).
        cam_colors = [
            (220, 120, 120), (120, 200, 120), (120, 140, 230),
            (210, 200, 80), (200, 120, 200), (120, 200, 200),
            (160, 200, 100), (180, 100, 180), (100, 180, 200),
        ]
        for i, calib in enumerate(dataset.camera_list()):
            if calib.homography is None:
                continue
            pts = np.array(
                [[p.x / 100.0 * fw, p.y / 100.0 * fh] for p in calib.map_points],
                dtype=np.int32,
            )
            cv2.polylines(canvas, [pts], True, cam_colors[i % len(cam_colors)], 2)
        # Each table.
        for r in reports:
            for pc in r.per_camera:
                proj = np.asarray(pc["projected_floor_px"], dtype=np.int32)
                cv2.polylines(canvas, [proj], True, (0, 200, 200), 2, cv2.LINE_AA)
        for obj, r in zip(objects, reports):
            poly_px = np.asarray(obj.meta.get("polygon_floor_px") or [], dtype=np.int32)
            if poly_px.shape[0] < 3:
                continue
            cv2.polylines(canvas, [poly_px], True, (255, 60, 30), 3, cv2.LINE_AA)
            c = poly_px.mean(axis=0)
            label = f"{r.table_id} IoU={r.mean_iou:.2f} dC={r.mean_centroid_err_cm:.0f}cm"
            cv2.putText(
                canvas, label, (int(c[0]) - 80, int(c[1])),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 60, 30), 2, cv2.LINE_AA,
            )
        cv2.imwrite(str(out_dir / "alignment_overlay.jpg"), canvas)
        with open(out_dir / "alignment_report.json", "w", encoding="utf-8") as f:
            summary = {
                "n_tables": len(reports),
                "mean_iou": float(np.mean([r.mean_iou for r in reports]))
                            if reports else 0.0,
                "mean_centroid_err_cm": float(np.mean(
                    [r.mean_centroid_err_cm for r in reports]
                )) if reports else 0.0,
                "max_centroid_err_cm": float(np.max(
                    [r.max_centroid_err_cm for r in reports]
                )) if reports else 0.0,
                "tables": [asdict(r) for r in reports],
            }
            json.dump(summary, f, indent=2)
        logger.info(
            "Alignment report: %d tables, mean IoU=%.2f, mean centroid err=%.0fcm",
            len(reports),
            summary["mean_iou"],
            summary["mean_centroid_err_cm"],
        )
    return reports
