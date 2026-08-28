"""只消费统一中间层的 2.5D 建图业务流水线。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from store_vision.data.models import MapObject25D, StoreDataset
from store_vision.data.workspace import load_intermediate_runtime, load_sfm_evidence
from store_vision.config import StoreConfig
from store_vision.detection import detect_tables_with_review
from store_vision.mapping.alignment_report import compute_alignment_report
from store_vision.mapping.map25d import build_map25d
from store_vision.mapping.map25d_preview import render_map25d_preview
from store_vision.mapping.overlap import compute_overlaps


@dataclass(frozen=True)
class Map25DWorkflowResult:
    """一次独立 2.5D 建图运行的界面交付结果。"""

    dataset: StoreDataset
    map25d: dict[str, Any]
    review_map25d: dict[str, Any]
    objects: tuple[MapObject25D, ...]
    output_dir: Path
    preview_path: Path | None
    alignment_summary: dict[str, Any]
    candidate_name: str
    intermediate_path: Path
    mode: str


MAP25D_MODES = {
    "baseline",
    "sfm_assisted",
    "sfm_registered_only",
}


def _apply_sfm_support(
    objects: list[MapObject25D],
    accepted_camera_ids: set[str],
) -> None:
    """把相机级三维观测转成对象支撑权重，不冒充桌面三维点。"""

    for item in objects:
        observed = set(item.meta.get("observed_by", []))
        supported = sorted(observed & accepted_camera_ids)
        ratio = len(supported) / max(len(observed), 1)
        item.meta["baseline_detector_score"] = item.score
        item.meta["sfm_camera_support"] = {
            "supported_cameras": supported,
            "observation_camera_ratio": ratio,
            "interpretation": "相机级共同三维观测，不是桌面表面三维点。",
        }
        # 辅助模式保留全部相机，只把无视觉支撑结果降权；基础模式不受影响。
        item.score = float(item.score * (0.75 + 0.25 * ratio))


def _write_json(path: Path, payload: Any) -> None:
    """写入稳定、可追踪的业务输出 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_map25d_from_intermediate(
    intermediate_path: str | Path,
    output_dir: str | Path,
    *,
    candidate_name: str | None = None,
    mode: str = "baseline",
    cfg: StoreConfig | None = None,
) -> Map25DWorkflowResult:
    """从中间层的 K/D/H、米制平面与高度策略生成独立 2.5D 输出。"""

    if mode not in MAP25D_MODES:
        raise ValueError(f"不支持的 2.5D 模式：{mode}")
    runtime = load_intermediate_runtime(
        intermediate_path,
        candidate_name=candidate_name,
        cfg=cfg,
    )
    if not runtime.capabilities.map25d.ready:
        raise ValueError(
            "2.5D 建图所需中间数据不完整："
            + "、".join(runtime.capabilities.map25d.missing)
        )
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    evidence = load_sfm_evidence(runtime.package_path)
    accepted_camera_ids = set(
        evidence.get("accepted_camera_ids", []) if evidence else []
    )
    if mode != "baseline" and not runtime.capabilities.sfm_assisted.ready:
        raise ValueError("当前中间层没有与候选匹配的有效 SfM 三维观测证据")
    allowed_camera_ids = (
        accepted_camera_ids if mode == "sfm_registered_only" else None
    )

    overlaps = compute_overlaps(runtime.dataset, output / "overlap")
    detection = detect_tables_with_review(
        runtime.dataset,
        runtime.config,
        output / "detection",
        allowed_camera_ids=allowed_camera_ids,
    )
    objects = detection.objects
    review_objects = detection.review_objects
    if mode == "sfm_assisted":
        # 自动对象与候选池共享实例，只处理候选池即可避免重复降权。
        _apply_sfm_support(review_objects, accepted_camera_ids)
    calibration_item = runtime.manifest["calibration_candidates"][
        runtime.candidate_name
    ]
    projection_item = runtime.manifest.get(
        "planar_projection_candidates", {}
    ).get(runtime.candidate_name, {})
    metadata = {
        "workflow": "map25d_from_intermediate",
        "map25d_mode": mode,
        "trust_level": calibration_item.get(
            "quality_status", "unclassified"
        ),
        "calibration_candidate": runtime.candidate_name,
        "calibration_source": calibration_item.get("source"),
        "position_source": (
            "height_aware_rt_plan_anchor: K/D undistorted ray + R/t "
            f"intersection at Z={runtime.config.table_height_cm:.1f} cm; "
            f"plan anchor={projection_item.get('source')}"
        ),
        "height_source": (
            f"fixed {runtime.config.table_height_cm:.1f} cm business value "
            "used by both XY plane intersection and Z extrusion"
        ),
        "sfm_status": (
            "available_as_optional_evidence"
            if evidence
            else "not_required"
        ),
        "plan_width_cm": runtime.config.floor_plan_width_cm,
        "plan_height_cm": runtime.config.floor_plan_height_cm,
        "intermediate_package": str(runtime.package_path),
        "limitations": [
            "XY 由 K/D/R/t 在指定桌高平面反投影，并由已发布 H 锚定到平面图。",
            "桌高为固定业务值，不是图像或点云测量结果。",
            "R/t 若为 provisional，其误差会继续影响桌面 XY。",
            "自动对象仍需人工复核。",
        ],
    }
    collection = build_map25d(
        objects,
        output / "map25d.geojson",
        metadata=metadata,
    )
    review_metadata = {
        **metadata,
        "review_status": "candidate_pool_unreviewed",
        "review_instructions": (
            "review_default_included=false 的弱单相机候选可由界面人工纳入。"
        ),
    }
    review_collection = build_map25d(
        review_objects,
        output / "map25d_candidates.geojson",
        metadata=review_metadata,
    )

    alignment = compute_alignment_report(
        runtime.dataset,
        objects,
        runtime.config,
        output / "alignment",
    )
    alignment_summary: dict[str, Any] = {}
    if alignment:
        alignment_summary = {
            "table_count": len(alignment),
            "mean_iou": float(np.mean([item.mean_iou for item in alignment])),
            "mean_centroid_error_cm": float(
                np.mean([item.mean_centroid_err_cm for item in alignment])
            ),
            "max_centroid_error_cm": float(
                np.max([item.max_centroid_err_cm for item in alignment])
            ),
        }

    preview_path: Path | None = None
    if runtime.dataset.floor_plan_path:
        preview_path = render_map25d_preview(
            collection,
            runtime.dataset.floor_plan_path,
            output / "map25d_preview.png",
        )
    output_manifest = {
        "schema_version": 1,
        "package_type": "store_vision_map25d_output",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "dataset_id": runtime.manifest.get("dataset_id"),
        "source_intermediate": str(runtime.package_path),
        "calibration_candidate": runtime.candidate_name,
        "map25d_mode": mode,
        "sfm_evidence": evidence,
        "capabilities": runtime.capabilities.as_dict(),
        "summary": {
            "object_count": len(objects),
            "review_candidate_count": len(review_objects),
            "weak_review_candidate_count": len(review_objects) - len(objects),
            "overlap_pair_count": len(overlaps.get("pairs", []))
            if isinstance(overlaps, dict)
            else 0,
            "alignment": alignment_summary,
        },
        "outputs": {
            "geojson": "map25d.geojson",
            "review_candidates": "map25d_candidates.geojson",
            "reviewed_geojson": "map25d_reviewed.geojson",
            "reviewed_preview": "map25d_reviewed_preview.png",
            "review_outputs_generated_on_selection": True,
            "preview": preview_path.name if preview_path else None,
            "detection": "detection",
            "overlap": "overlap",
            "alignment": "alignment",
        },
        "raw_inputs_modified": False,
    }
    _write_json(output / "manifest.json", output_manifest)
    return Map25DWorkflowResult(
        dataset=runtime.dataset,
        map25d=collection,
        review_map25d=review_collection,
        objects=tuple(objects),
        output_dir=output,
        preview_path=preview_path,
        alignment_summary=alignment_summary,
        candidate_name=runtime.candidate_name,
        intermediate_path=runtime.package_path,
        mode=mode,
    )


__all__ = [
    "MAP25D_MODES",
    "Map25DWorkflowResult",
    "run_map25d_from_intermediate",
]
