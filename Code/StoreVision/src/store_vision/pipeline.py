"""End-to-end pipeline."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from store_vision.calibration import (
    calibrate_shared_fisheye,
    calibrate_homographies,
)
from store_vision.calibration.colmap_refine import run_colmap_if_available
from store_vision.calibration.scale_metadata import load_scale_metadata
from store_vision.config import StoreConfig
from store_vision.data import load_store_folder
from store_vision.data.models import MapObject25D, StoreDataset
from store_vision.detection import detect_tables_all
from store_vision.mapping import (
    build_map25d,
    compute_alignment_report,
    compute_overlaps,
    stitch_cameras_to_floor,
)

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    dataset: StoreDataset
    map25d: dict
    objects: list[MapObject25D] = field(default_factory=list)
    overlaps: dict | None = None
    floor_image: np.ndarray | None = None
    stitch_image: np.ndarray | None = None
    output_dir: Path | None = None
    alignment: list = field(default_factory=list)
    alignment_summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def run_pipeline(
    source: str | Path | StoreDataset,
    cfg: StoreConfig | None = None,
    skip_detection: bool = False,
    output_dir: str | Path | None = None,
) -> PipelineResult:
    """运行完整流水线，并可将结果直接写入用户指定的输出目录。"""
    cfg = cfg or StoreConfig()
    if isinstance(source, StoreDataset):
        dataset = source
        source_name = Path(dataset.root).name
    else:
        folder = Path(source)
        dataset = load_store_folder(folder)
        source_name = folder.name

    # 2.5D 平面尺寸优先采用 scale 文件，而不是跨门店共用固定 20×12m。
    runtime_cfg = replace(cfg)
    scale_summary: dict = {
        "status": "default_dimensions",
        "plan_width_cm": runtime_cfg.floor_plan_width_cm,
        "plan_height_cm": runtime_cfg.floor_plan_height_cm,
        "rmse_cm": None,
    }
    if dataset.scale_path:
        try:
            scale, _ = load_scale_metadata(dataset.scale_path)
            runtime_cfg.floor_plan_width_cm = (
                abs(scale.x_metres_per_percent) * 100.0 * 100.0
            )
            runtime_cfg.floor_plan_height_cm = (
                abs(scale.y_metres_per_percent) * 100.0 * 100.0
            )
            scale_summary = {
                "status": "scale_metadata_trial",
                "source": str(dataset.scale_path),
                "plan_width_cm": runtime_cfg.floor_plan_width_cm,
                "plan_height_cm": runtime_cfg.floor_plan_height_cm,
                "rmse_cm": scale.rmse_metres * 100.0,
                "max_error_cm": scale.max_error_metres * 100.0,
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            scale_summary["warning"] = str(exc)

    # 未指定时保留命令行旧行为；桌面界面会传入用户选择的目录。
    out_dir = (
        Path(output_dir).expanduser()
        if output_dir
        else cfg.output_root / source_name / "current_pipeline"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Pipeline start: %s", dataset.root)
    cal_dir = out_dir / "calibration"
    cal_dir.mkdir(parents=True, exist_ok=True)
    # 在任何几何计算前保存配对与坐标变换证据，便于定位错误数据而不修改原图。
    (cal_dir / "input_resolution.json").write_text(
        json.dumps(
            {
                "status": "blocked" if dataset.resolution_errors() else "ready",
                "strict": cfg.strict_input_resolution,
                "diagnostics": dataset.input_diagnostics,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if cfg.strict_input_resolution and dataset.resolution_errors():
        summary = "；".join(
            f"{row.get('physical_camera_id', 'unknown')}: {row.get('message', row.get('code'))}"
            for row in dataset.resolution_errors()[:5]
        )
        raise ValueError(
            "输入图片与标定坐标不一致，已停止几何处理；"
            f"详见 {cal_dir / 'input_resolution.json'}。{summary}"
        )
    # 正式路线：全部镜头按同一型号鱼眼相机处理，共享 K/D，先形成拟合
    # 初始化基线，再复用这些控制观测联合优化逐机位姿。
    shared_calibration = calibrate_shared_fisheye(dataset, cal_dir)
    calibrate_homographies(dataset, cal_dir)

    if cfg.use_colmap:
        run_colmap_if_available(dataset, cal_dir)

    overlaps = None
    if dataset.floor_plan_path:
        try:
            overlaps = compute_overlaps(dataset, out_dir / "overlap")
        except Exception as e:
            logger.warning("Overlap computation failed: %s", e)

    objects: list[MapObject25D] = []
    warnings: list[str] = []
    detection_status = "skipped" if skip_detection else "complete"
    if not skip_detection:
        try:
            objects = detect_tables_all(
                dataset,
                runtime_cfg,
                out_dir / "detection",
            )
        except Exception as e:
            logger.warning("Table detection failed: %s", e)
            detection_status = "failed"
            warnings.append(f"桌子检测失败：{e}")
            detection_dir = out_dir / "detection"
            detection_dir.mkdir(parents=True, exist_ok=True)
            (detection_dir / "table_summary.json").write_text(
                json.dumps(
                    {"status": "failed", "error": str(e)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    map25d = build_map25d(
        objects,
        out_dir / "map25d.geojson",
        metadata={
            "trust_level": (
                "observation_not_independently_validated"
                if shared_calibration.get("credible_calibration")
                else "provisional_planar_trial"
            ),
            "position_source": (
                "K/D/R/t tabletop-plane projection anchored by cali floor homography"
            ),
            "height_source": (
                f"fixed {runtime_cfg.table_height_cm:.1f} cm business value used "
                "for XY projection and Z extrusion"
            ),
            "sfm_status": "not_used_in_base_pipeline",
            "intrinsics_route": shared_calibration.get(
                "mapping_intrinsics_route"
            ),
            "credible_calibration": shared_calibration.get(
                "credible_calibration", False
            ),
            "detection_status": detection_status,
            "plan_width_cm": runtime_cfg.floor_plan_width_cm,
            "plan_height_cm": runtime_cfg.floor_plan_height_cm,
            "scale": scale_summary,
            "limitations": [
                "XY 来自四点平面映射，不是 SfM 三维坐标。",
                "Z 为固定业务高度，不是图像测量结果。",
                "桌子检测结果是候选，仍需人工核对。",
            ],
        },
    )

    alignment = []
    alignment_summary: dict = {}
    if objects and dataset.floor_plan_size:
        try:
            alignment = compute_alignment_report(
                dataset, objects, runtime_cfg, out_dir / "alignment"
            )
            if alignment:
                alignment_summary = {
                    "n_tables": len(alignment),
                    "mean_iou": float(
                        np.mean([a.mean_iou for a in alignment])
                    ),
                    "mean_centroid_err_cm": float(
                        np.mean([a.mean_centroid_err_cm for a in alignment])
                    ),
                    "max_centroid_err_cm": float(
                        np.max([a.max_centroid_err_cm for a in alignment])
                    ),
                }
                logger.info(
                    "Alignment: tables=%d mean_IoU=%.2f mean_dC=%.0fcm max_dC=%.0fcm",
                    alignment_summary["n_tables"],
                    alignment_summary["mean_iou"],
                    alignment_summary["mean_centroid_err_cm"],
                    alignment_summary["max_centroid_err_cm"],
                )
        except Exception as e:
            logger.warning("Alignment report failed: %s", e)

    floor_img = stitch_img = None
    if dataset.floor_plan_path:
        try:
            floor_img, stitch_img = stitch_cameras_to_floor(
                dataset, runtime_cfg, out_dir / "stitch"
            )
        except Exception as e:
            logger.warning("Stitch failed: %s", e)

    logger.info("Pipeline done: %s", out_dir)
    return PipelineResult(
        dataset=dataset,
        map25d=map25d,
        objects=objects,
        overlaps=overlaps,
        floor_image=floor_img,
        stitch_image=stitch_img,
        output_dir=out_dir,
        alignment=alignment,
        alignment_summary=alignment_summary,
        warnings=warnings,
    )
