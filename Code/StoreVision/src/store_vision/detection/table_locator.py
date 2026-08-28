"""Glue: filter overlay lines, detect white tables, dedupe across cameras."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from store_vision.config import StoreConfig
from store_vision.data.models import MapObject25D, StoreDataset
from store_vision.detection.overlay_filter import remove_overlay_lines
from store_vision.detection.white_table import TableCandidate, detect_white_tables

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableDetectionResult:
    """自动输出与人工待复核池；后者包含被单相机阈值挡住的弱候选。"""

    objects: list[MapObject25D]
    review_objects: list[MapObject25D]


def _polygon_iou_cm(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Polygon IoU in floor-cm space (Shoelace + clipping)."""
    if len(a) < 3 or len(b) < 3:
        return 0.0
    pa = np.asarray(a, dtype=np.float32)
    pb = np.asarray(b, dtype=np.float32)
    ret, inter = cv2.intersectConvexConvex(pa, pb)
    if ret <= 0 or inter is None or len(inter) < 3:
        return 0.0
    inter_a = float(cv2.contourArea(inter))
    union_a = float(cv2.contourArea(pa)) + float(cv2.contourArea(pb)) - inter_a
    if union_a <= 1e-3:
        return 0.0
    return inter_a / union_a


def _polygon_centroid(poly: list[tuple[float, float]]) -> tuple[float, float]:
    arr = np.asarray(poly, dtype=np.float64)
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


def _dedupe_tables(
    raw: list[tuple[TableCandidate, str]],
    cfg: StoreConfig,
) -> list[tuple[TableCandidate, str, list[tuple[TableCandidate, str]]]]:
    """Cluster (candidate, camera_serial) tuples whose floor-plane footprints
    overlap significantly. Returns one entry per cluster:
    ``(best_candidate, primary_camera_serial, [all_members])``."""
    used = [False] * len(raw)
    out: list[tuple[TableCandidate, str, list[tuple[TableCandidate, str]]]] = []
    for i, (ci, si) in enumerate(raw):
        if used[i]:
            continue
        cluster_idx = [i]
        cxi, cyi = _polygon_centroid(ci.polygon_cm)
        for j in range(i + 1, len(raw)):
            if used[j]:
                continue
            cj, sj = raw[j]
            cxj, cyj = _polygon_centroid(cj.polygon_cm)
            d = float(np.hypot(cxi - cxj, cyi - cyj))
            iou = _polygon_iou_cm(ci.polygon_cm, cj.polygon_cm)
            if d <= cfg.table_dedup_distance_cm or iou >= 0.30:
                cluster_idx.append(j)
        for k in cluster_idx:
            used[k] = True
        cluster = [raw[k] for k in cluster_idx]
        cluster.sort(key=lambda x: x[0].score, reverse=True)
        best_cand, best_serial = cluster[0]
        out.append((best_cand, best_serial, cluster))
    return out


def _cluster_to_object(
    object_id: str,
    cand: TableCandidate,
    primary: str,
    members: list[tuple[TableCandidate, str]],
    cfg: StoreConfig,
    *,
    default_included: bool,
    automatic_gate: str,
) -> MapObject25D:
    """把跨相机候选簇转换为带人工复核元数据的 2.5D 对象。"""

    sources = sorted({serial for _, serial in members})
    observations = [
        {
            "camera": serial,
            "polygon_img": item.polygon_img,
            "polygon_floor_px": item.polygon_floor_px,
            "polygon_cm": item.polygon_cm,
            "score": item.score,
            "projection_method": item.projection_method,
            "projection_height_cm": item.projection_height_cm,
        }
        for item, serial in members
    ]
    return MapObject25D(
        id=object_id,
        label="table",
        polygon_cm=cand.polygon_cm,
        height_cm=cfg.table_height_cm,
        source_camera=primary,
        score=cand.score,
        meta={
            "image_polygon": cand.polygon_img,
            "polygon_floor_px": cand.polygon_floor_px,
            "bbox_cm_size": cand.bbox_cm_size,
            "sub_scores": cand.sub_scores,
            "observed_by": sources,
            "observations": observations,
            "projection_method": cand.projection_method,
            "projection_height_cm": cand.projection_height_cm,
            "review_default_included": default_included,
            "automatic_gate": automatic_gate,
        },
    )


def detect_tables_with_review(
    dataset: StoreDataset,
    cfg: StoreConfig,
    out_dir: Path | None = None,
    *,
    allowed_camera_ids: set[str] | None = None,
) -> TableDetectionResult:
    """检测并合并桌子，同时保留可由人工纳入的弱单相机候选。"""

    if not dataset.floor_plan_size:
        return TableDetectionResult([], [])
    fw, fh = dataset.floor_plan_size

    raw: list[tuple[TableCandidate, str]] = []
    diag: list[dict] = []
    inpaint_dir = (out_dir / "inpaint") if out_dir else None
    detect_dir = (out_dir / "candidates") if out_dir else None

    for calib in dataset.camera_list():
        if (
            allowed_camera_ids is not None
            and calib.device_serial not in allowed_camera_ids
        ):
            continue
        if not calib.image_path:
            continue
        raw_img = cv2.imread(calib.image_path)
        if raw_img is None:
            continue

        clean, _ = remove_overlay_lines(raw_img, calib, cfg, inpaint_dir)
        cands = detect_white_tables(clean, calib, cfg, fw, fh, detect_dir)
        accepted = [c for c in cands if c.accepted]
        for c in accepted:
            raw.append((c, calib.device_serial))
        diag.append({
            "camera": calib.device_serial,
            "name": calib.name,
            "n_candidates": len(cands),
            "n_accepted": len(accepted),
            "scores": [c.score for c in cands],
            "reasons": [c.reason for c in cands],
        })

    clustered = _dedupe_tables(raw, cfg)
    # False-positive guard: single-camera detections must clear the stricter
    # threshold; multi-camera ones are always trusted. Empirically this
    # eliminates wall-mounted display panels that fool a single fish-eye view.
    filtered: list[tuple[TableCandidate, str, list[tuple[TableCandidate, str]]]] = []
    for cand, primary, members in clustered:
        unique_cams = {s for _, s in members}
        if len(unique_cams) >= 2 or cand.score >= cfg.table_score_threshold_single_cam:
            filtered.append((cand, primary, members))
    rejected = len(clustered) - len(filtered)

    accepted_keys = {id(item[0]) for item in filtered}
    objects: list[MapObject25D] = []
    review_objects: list[MapObject25D] = []
    automatic_index = 0
    weak_index = 0
    for cand, primary, members in clustered:
        default_included = id(cand) in accepted_keys
        if default_included:
            object_id = f"table_{automatic_index:03d}"
            automatic_index += 1
            gate = "multi_camera_or_single_score_pass"
        else:
            object_id = f"review_weak_{weak_index:03d}"
            weak_index += 1
            gate = (
                "single_camera_score_below_"
                f"{cfg.table_score_threshold_single_cam:.2f}"
            )
        item = _cluster_to_object(
            object_id,
            cand,
            primary,
            members,
            cfg,
            default_included=default_included,
            automatic_gate=gate,
        )
        review_objects.append(item)
        if default_included:
            objects.append(item)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "table_summary.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "n_raw_accepted": len(raw),
                    "n_after_dedup": len(clustered),
                    "n_after_single_cam_filter": len(filtered),
                    "n_rejected_low_confidence_single_cam": rejected,
                    "n_review_pool": len(review_objects),
                    "per_camera": diag,
                    "tables": [
                        {
                            "id": o.id,
                            "score": o.score,
                            "centroid_cm": _polygon_centroid(o.polygon_cm),
                            "size_cm": o.meta.get("bbox_cm_size"),
                            "primary": o.source_camera,
                            "observed_by": o.meta.get("observed_by"),
                        }
                        for o in objects
                    ],
                    "review_candidates": [
                        {
                            "id": o.id,
                            "score": o.score,
                            "default_included": o.meta.get(
                                "review_default_included", True
                            ),
                            "automatic_gate": o.meta.get("automatic_gate"),
                            "projection_method": o.meta.get(
                                "projection_method"
                            ),
                        }
                        for o in review_objects
                    ],
                },
                f,
                indent=2,
            )

    logger.info(
        "Tables: %d raw → %d after dedup → %d after single-cam filter "
        "(across %d cameras; rejected %d weak singles)",
        len(raw), len(clustered), len(filtered), len(dataset.cameras), rejected,
    )
    return TableDetectionResult(objects, review_objects)


def detect_tables_all(
    dataset: StoreDataset,
    cfg: StoreConfig,
    out_dir: Path | None = None,
    *,
    allowed_camera_ids: set[str] | None = None,
) -> list[MapObject25D]:
    """兼容既有调用，只返回通过自动门禁的桌子对象。"""

    return detect_tables_with_review(
        dataset,
        cfg,
        out_dir,
        allowed_camera_ids=allowed_camera_ids,
    ).objects
