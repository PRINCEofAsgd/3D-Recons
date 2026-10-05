"""V0.3.0 COLMAP Geometry Diagnostics 的只读编排与报告输出。"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from store_vision.calibration.geometry_visualization import (
    pair_directory_name,
    write_pair_visualizations,
)
from store_vision.calibration.intrinsics_config import load_and_validate_intrinsics
from store_vision.calibration.model_geometry import (
    analyze_current_model,
    parse_model_observations,
    parse_model_points,
)
from store_vision.calibration.pair_geometry import (
    ColmapPairDatabase,
    GeometryDiagnosticsConfig,
    diagnose_pair,
    rank_initial_pairs,
)


DEFAULT_DIAGNOSTIC_PAIRS = (
    ("CAMERA-04-01.jpg", "CAMERA-07-01.jpg"),
    ("CAMERA-05-01.jpg", "CAMERA-07-01.jpg"),
    ("CAMERA-07-01.jpg", "CAMERA-06-01.jpg"),
    ("CAMERA-02-01.jpg", "CAMERA-03-01.jpg"),
)


@dataclass(frozen=True)
class GeometryDiagnosticsRequest:
    database_path: Path
    image_path: Path
    model_path: Path
    output_path: Path
    config: GeometryDiagnosticsConfig = field(default_factory=GeometryDiagnosticsConfig)
    extra_pairs: tuple[tuple[str, str], ...] = ()
    all_pairs: bool = False
    dry_run: bool = False
    overwrite: bool = False
    intrinsics_config: Path | None = None


def _json_write(path: Path, payload: Any) -> Path:
    """以稳定缩进写入 UTF-8 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _canonical_pair(image_a: str, image_b: str) -> tuple[str, str]:
    """将无序图像对规范为名称升序。"""
    return tuple(sorted((image_a, image_b)))  # type: ignore[return-value]


def _pair_key(image_a: str, image_b: str) -> tuple[str, str]:
    """返回用于跨报告关联的稳定图像对键。"""
    return _canonical_pair(image_a, image_b)


def _select_pairs(
    all_diagnostics: list[dict[str, Any]],
    extra_pairs: Iterable[tuple[str, str]],
    all_pairs: bool,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """合并默认、弱匹配、最佳覆盖、最强匹配和显式图像对并去重。"""
    available = {_pair_key(item["image_a"], item["image_b"]): item for item in all_diagnostics}
    explicit_pairs = list(extra_pairs)
    # 内置默认对来自当前 Apple 样例；其他门店自动跳过不存在的默认对。
    # 用户通过 --pair 明确指定的图像对仍严格校验，避免静默忽略输入错误。
    missing = [pair for pair in explicit_pairs if _pair_key(*pair) not in available]
    if missing:
        readable = ", ".join(f"{a} <-> {b}" for a, b in missing)
        raise KeyError(f"requested diagnostic image pair is unavailable: {readable}")
    reasons: dict[tuple[str, str], set[str]] = {}

    def add(pair: tuple[str, str], reason: str) -> None:
        """累积图像对选择原因，同时完成自动去重。"""
        reasons.setdefault(_pair_key(*pair), set()).add(reason)

    if all_pairs:
        for pair in sorted(available):
            add(pair, "all_pairs")
    else:
        for pair in DEFAULT_DIAGNOSTIC_PAIRS:
            if _pair_key(*pair) in available:
                add(pair, "default_pair")
        for pair in explicit_pairs:
            add(pair, "explicit_pair")
        positive = [item for item in all_diagnostics if item["verified_matches"] > 0]
        if positive:
            weak = min(positive, key=lambda item: (item["verified_matches"], item["image_a"], item["image_b"]))
            add((weak["image_a"], weak["image_b"]), "weak_positive_pair")
            coverage = max(
                positive,
                key=lambda item: (
                    item["spatial_grid"]["joint"]["min_coverage_ratio"],
                    item["verified_matches"],
                    item["image_a"],
                    item["image_b"],
                ),
            )
            add((coverage["image_a"], coverage["image_b"]), "best_spatial_coverage")
            strongest = max(
                positive,
                key=lambda item: (item["verified_matches"], item["image_a"], item["image_b"]),
            )
            add((strongest["image_a"], strongest["image_b"]), "most_verified_matches")
    pairs = sorted(reasons)
    return pairs, {f"{a} <-> {b}": sorted(reasons[(a, b)]) for a, b in pairs}


def _write_ranking(rows: list[dict[str, Any]], output_path: Path) -> dict[str, str]:
    """同时写出完整 JSON 与便于筛选的 CSV 排名。"""
    json_path = _json_write(
        output_path / "initial_pair_ranking.json",
        {
            "ranking_type": "initialization candidate diagnostic ranking",
            "official_colmap_score": False,
            "limitations": [
                "No trustworthy intrinsics are available, so triangulation angle is not part of this ranking.",
                "The ranking is a reproducible diagnostic heuristic, not ground truth.",
            ],
            "pairs": rows,
        },
    )
    csv_path = output_path / "initial_pair_ranking.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "rank", "image_a", "image_b", "raw_matches", "verified_matches", "verified_ratio",
        "coverage_a", "coverage_b", "min_coverage", "median_displacement_px",
        "homography_inlier_ratio", "fundamental_inlier_ratio", "concentration_warning",
        "final_score", "score_breakdown", "warnings",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: json.dumps(row[field], ensure_ascii=False, sort_keys=True)
                    if field in {"score_breakdown", "warnings"}
                    else row[field]
                    for field in fields
                }
            )
    return {"json": str(json_path), "csv": str(csv_path)}


def _controlled_experiment_plan(
    request: GeometryDiagnosticsRequest,
    intrinsics: dict[str, Any],
    ranking: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成 E0–E4 单变量、默认不执行的受控实验计划。"""
    ready = bool(intrinsics.get("ready_for_known_intrinsics"))
    binary = "colmap"
    top_pairs = [(row["image_a"], row["image_b"]) for row in ranking[:2]]
    experiments: list[dict[str, Any]] = [
        {
            "id": "E0",
            "status": "reused_existing_result" if request.model_path.is_dir() else "available",
            "changed_variables": [],
            "description": "Current free-estimated-intrinsics, automatic-initialization baseline.",
            "commands": [],
        }
    ]
    definitions = [
        ("E1", "Measured per-camera or documented group intrinsics; automatic initialization.", None, None),
        ("E2", "Measured intrinsics with the first ranked diagnostic pair.", top_pairs[0] if top_pairs else None, None),
        ("E3", "Measured intrinsics with the second ranked diagnostic pair.", top_pairs[1] if len(top_pairs) > 1 else None, None),
        ("E4", "Measured intrinsics and only a mild init_min_tri_angle adjustment.", None, "14.4"),
    ]
    for experiment_id, description, pair, tri_angle in definitions:
        experiment_root = f"<NEW_OUTPUT>/{experiment_id}"
        experiment_database = f"{experiment_root}/database.db"
        # 内参缺失时保留占位符而不填猜测值；校验通过后由准备步骤写入逐图 camera/image 记录。
        feature = [
            binary, "feature_extractor", "--database_path", experiment_database,
            "--image_path", str(request.image_path), "--default_random_seed", str(request.config.seed),
            "--ImageReader.camera_model", "<FROM_VALIDATED_INTRINSICS_CONFIG>",
            "--ImageReader.single_camera", "0", "--FeatureExtraction.use_gpu", "0",
            "--FeatureExtraction.num_threads", "1",
        ]
        matcher = [
            binary, "exhaustive_matcher", "--database_path", experiment_database,
            "--default_random_seed", str(request.config.seed), "--FeatureMatching.use_gpu", "0",
            "--FeatureMatching.num_threads", "1",
        ]
        mapper = [binary, "mapper", "--database_path", experiment_database, "--image_path", str(request.image_path), "--output_path", f"{experiment_root}/sparse", "--default_random_seed", str(request.config.seed), "--Mapper.random_seed", str(request.config.seed), "--Mapper.num_threads", "1"]
        changed = ["measured_intrinsics"]
        if pair is not None:
            # COLMAP mapper accepts database image ids for explicit initialization; names remain in plan for review.
            mapper.extend(["--Mapper.init_image_id1", f"<ID:{pair[0]}>", "--Mapper.init_image_id2", f"<ID:{pair[1]}>"])
            changed.append("initial_image_pair")
        if tri_angle is not None:
            mapper.extend(["--Mapper.init_min_tri_angle", tri_angle])
            changed.append("init_min_tri_angle")
        experiments.append(
            {
                "id": experiment_id,
                "status": "dry_run_ready" if ready else "blocked",
                "blocked_reason": None if ready else "measured intrinsics configuration is missing or invalid",
                "changed_variables": changed,
                "baseline_difference": changed,
                "change_details": {
                    "initial_pair": list(pair) if pair else None,
                    "init_min_tri_angle_degrees": {
                        "E0_baseline": 16.0,
                        "planned": float(tri_angle),
                        "relative_change": "-10%",
                    } if tri_angle is not None else None,
                },
                "description": description,
                "selected_pair": list(pair) if pair else None,
                "preparation_steps": [
                    "Create a new experiment directory and database; never reuse run_001/database.db.",
                    "After feature extraction, replace/create camera and image assignments from the validated intrinsics groups before matching; do not use EXIF guesses.",
                ],
                "commands": [
                    feature,
                    matcher,
                    mapper,
                    [binary, "model_converter", "--input_path", f"{experiment_root}/sparse/0", "--output_path", f"{experiment_root}/model_txt", "--output_type", "TXT"],
                ],
                "execute_now": False,
            }
        )
    return {
        "status": "ready" if ready else "blocked",
        "dry_run_only": True,
        "rules": [
            "Each experiment changes only the listed variables relative to E0.",
            "No focal length, principal point or distortion value is guessed.",
            "E1-E4 must write a new database/output and may not modify run_001.",
        ],
        "experiments": experiments,
    }


def _unregistered_diagnostics(
    database: ColmapPairDatabase,
    model_path: Path,
    pair_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """从当前轨迹和验证匹配计算未注册图的静态事实与规则提示。"""
    images_path = model_path / "images.txt"
    points_path = model_path / "points3D.txt"
    if not images_path.is_file() or not points_path.is_file():
        return {"status": "unavailable", "reason": "current text model observations are unavailable", "images": []}
    registered = parse_model_observations(images_path)
    model_points = parse_model_points(points_path)
    track_lengths = {point_id: len(point.track) for point_id, point in model_points.items()}
    results: list[dict[str, Any]] = []
    for image_name in sorted(set(database.images) - set(registered)):
        pair_rows: list[dict[str, Any]] = []
        potential_points: set[int] = set()
        potential_associations = 0
        concentrated = False
        homography_cue = False
        for registered_name, observations in sorted(registered.items()):
            pair = database.pair(image_name, registered_name)
            verified = pair.verified_indices
            matched_point_ids: list[int] = []
            for _, registered_feature_index in verified:
                point_id = observations.point3d_by_feature_index.get(int(registered_feature_index))
                if point_id is not None:
                    matched_point_ids.append(point_id)
            potential_associations += len(matched_point_ids)
            potential_points.update(matched_point_ids)
            diagnostic = pair_by_key[_pair_key(image_name, registered_name)]
            joint = diagnostic["spatial_grid"]["joint"]
            concentrated = concentrated or bool(joint["concentration_warning"])
            homography_cue = homography_cue or bool(diagnostic["geometry_models"]["diagnosis"]["homography_dominant"])
            pair_rows.append(
                {
                    "registered_image": registered_name,
                    "raw_matches": len(pair.raw_indices),
                    "verified_matches": len(verified),
                    "potential_2d3d_associations": len(matched_point_ids),
                    "distinct_3d_points": len(set(matched_point_ids)),
                    "concentration_warning": joint["concentration_warning"],
                    "homography_dominant_cue": diagnostic["geometry_models"]["diagnosis"]["homography_dominant"],
                }
            )
        reasons: list[str] = []
        if sum(row["verified_matches"] for row in pair_rows) < 15:
            reasons.append("insufficient_matches_to_registered_images")
        if len(potential_points) < 6:
            reasons.append("insufficient_potential_2d3d_correspondences")
        if concentrated:
            reasons.append("concentrated_correspondences")
        if homography_cue:
            reasons.append("likely_planar_or_rotation_degeneracy")
        if not potential_points or all(track_lengths.get(point_id, 0) <= 2 for point_id in potential_points):
            reasons.append("no_long_tracks")
        reasons.extend(["unknown_mapper_internal_failure", "requires_verbose_mapper_instrumentation"])
        results.append(
            {
                "image": image_name,
                "facts": {
                    "raw_matches_to_registered_images": sum(row["raw_matches"] for row in pair_rows),
                    "verified_matches_to_registered_images": sum(row["verified_matches"] for row in pair_rows),
                    "potential_2d3d_associations": potential_associations,
                    "distinct_potential_3d_points": len(potential_points),
                    "common_pnp_minimum_scale_reached": len(potential_points) >= 6,
                    "pair_details": pair_rows,
                },
                "rule_based_inferences": {
                    "concentrated_correspondences": concentrated,
                    "likely_planar_or_rotation_degeneracy": homography_cue,
                    "reason_codes": reasons,
                },
                "unknowns": [
                    "Actual mapper candidate 2D-3D correspondence count",
                    "Actual PnP inlier count and ratio",
                    "The exact internal mapper rejection branch without verbose instrumentation",
                ],
            }
        )
    return {
        "status": "complete",
        "registered_images": sorted(registered),
        "unregistered_image_count": len(results),
        "images": results,
        "method_limitations": "Potential associations are reconstructed from verified database matches to currently observed 3D points; they are not mapper PnP logs.",
    }


def _global_findings(all_pairs: list[dict[str, Any]], current_model: dict[str, Any]) -> tuple[list[str], list[str]]:
    """汇总全量图像对中可确定的统计事实和方法限制。"""
    pair_count = len(all_pairs)
    concentrated = sum(bool(item["spatial_grid"]["joint"]["concentration_warning"]) for item in all_pairs)
    h_dominant = sum(bool(item["geometry_models"]["diagnosis"]["homography_dominant"]) for item in all_pairs)
    low_displacement = sum(
        float(item["image_space_displacement"]["median"] or 0.0) <= 10.0 for item in all_pairs
    )
    findings = [
        f"All {pair_count} database image pairs were evaluated with real verified correspondences.",
        f"Spatial concentration warnings were raised for {concentrated}/{pair_count} pairs under configured grid thresholds.",
        f"Homography-dominance cues were raised for {h_dominant}/{pair_count} pairs; these cues are not proof of planarity or pure rotation.",
        f"Low median image-space displacement (at most 10 px) occurred in {low_displacement}/{pair_count} pairs; this is a proxy cue, not a triangulation-angle measurement.",
    ]
    if current_model.get("registered_image_count") == 2:
        findings.append("The current sparse model is only a two-view component, so graph connectivity has not become a stable nine-camera geometry.")
    limitations = [
        "No trustworthy measured intrinsics are available.",
        "Image-space displacement is a proxy and is not reported as triangulation angle.",
        "H/F competition can suggest but cannot prove planar degeneracy or pure rotation.",
        "Default mapper output does not expose complete per-image PnP rejection details.",
    ]
    return findings, limitations


def _markdown_report(payload: dict[str, Any]) -> str:
    """将结构化诊断结果转换为保守表述的可读 Markdown。"""
    geometry = payload["geometry_diagnostics"]
    model = payload["current_model_geometry"]
    ranking = geometry["top_initial_pair_candidates"]
    lines = [
        "# COLMAP Geometry Diagnostics Report",
        "",
        "## 1. 执行环境",
        "",
        f"- 数据库：`{geometry['database_path']}`",
        f"- 图像目录：`{geometry['image_path']}`",
        f"- 固定随机种子：`{geometry['config']['seed']}`；CPU 只读诊断，不执行 Mapper。",
        "",
        "## 2. 数据集摘要",
        "",
        f"- 图像数：{geometry['image_count']}；全量图像对：{geometry['all_pair_count']}；详细可视化图像对：{geometry['diagnosed_pair_count']}。",
        "",
        "## 3. 当前重建结果",
        "",
        f"- 当前注册图像：{model.get('registered_image_count', 0)}；稀疏点：{model.get('sparse_point_count', 0)}。",
        "- 当前异常内参下的模型深度与三角化角只用于模型内部诊断。",
        "",
        "## 4. 匹配图连通性",
        "",
        "- 匹配图连通只说明存在两两验证边，不保证能形成稳定、长轨迹的多视几何。",
        "",
        "## 5–8. 图像对、空间覆盖、H/F 与位移代理",
        "",
    ]
    lines.extend(f"- {finding}" for finding in geometry["global_findings"])
    lines.extend(["", "## 9. 初始化候选排名", ""])
    for row in ranking:
        lines.append(
            f"{row['rank']}. `{row['image_a']}` ↔ `{row['image_b']}`：score={row['final_score']:.4f}，"
            f"verified={row['verified_matches']}，coverage={row['min_coverage']:.3f}。"
        )
    lines.extend(
        [
            "",
            "## 10. 当前二视图模型分析",
            "",
            f"- 基线（模型单位）：{model.get('baseline_model_units')}。",
            f"- 轨迹长度分布：`{json.dumps(model.get('track_lengths', {}).get('distribution', {}), ensure_ascii=False)}`。",
            "- 仅两张注册图和几乎全为二视图轨迹，不能支持全局 9 相机对齐。",
            "",
            "## 11. 未注册图片静态诊断",
            "",
            f"- 已生成 {payload['unregistered_image_diagnostics'].get('unregistered_image_count', 0)} 张未注册图的真实匹配与潜在 2D-3D 关联统计。",
            "- 未伪造 mapper 的 PnP 内点数或内部失败原因。",
            "",
            "## 12. 内参缺失情况",
            "",
            f"- 状态：`{payload['intrinsics_configuration'].get('status')}`；缺失时 E1–E4 保持 blocked。",
            "",
            "## 13. 结论",
            "",
            "- 当前失败不是匹配图不连通，而是验证边尚未形成可信内参支持下的稳定多视几何与长轨迹。",
            "- 是否由低视差、纯旋转或近平面场景单独主导，只能作为规则提示，不能据此下确定性结论。",
            "",
            "## 14. 下一步建议",
            "",
            "1. 获取逐物理相机或有明确依据的相机组内参、畸变和分辨率记录。",
            "2. 内参校验通过后依次执行 E1，并仅对排名前两对做 E2/E3 单变量初始化实验。",
            "3. 注册覆盖显著改善且出现跨三张以上图像的长轨迹后，再评估人工位姿对齐。",
            "",
            "## 15. 方法限制",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in geometry["limitations"])
    return "\n".join(lines) + "\n"


def _dry_run_payload(request: GeometryDiagnosticsRequest) -> dict[str, Any]:
    """返回零写入的输入与计划产物清单。"""
    return {
        "status": "dry-run",
        "writes_performed": False,
        "database_mode": "read-only",
        "inputs": {
            "database_path": str(request.database_path),
            "image_path": str(request.image_path),
            "model_path": str(request.model_path),
        },
        "planned_outputs": [
            str(request.output_path / name)
            for name in (
                "pair_diagnostics/", "initial_pair_ranking.json", "initial_pair_ranking.csv",
                "current_model_geometry.json", "unregistered_image_diagnostics.json",
                "controlled_experiment_plan.json", "geometry_diagnostics_report.md",
            )
        ],
        "config": asdict(request.config),
    }


def run_geometry_diagnostics(request: GeometryDiagnosticsRequest) -> dict[str, Any]:
    """执行只读真实诊断；仅写指定 reports 目录和已有总报告的新增字段。"""

    if request.dry_run:
        return _dry_run_payload(request)
    for path, label in ((request.database_path, "database"), (request.image_path, "image directory")):
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
    output = request.output_path
    sentinel = output / "geometry_diagnostics_report.md"
    if sentinel.exists() and not request.overwrite:
        raise FileExistsError(f"diagnostic outputs already exist: {sentinel}; pass --overwrite to regenerate")
    with ColmapPairDatabase(request.database_path) as database:
        all_pairs: list[dict[str, Any]] = []
        pair_objects: dict[tuple[str, str], Any] = {}
        for pair in database.all_pairs():
            key = _pair_key(pair.image_a.name, pair.image_b.name)
            pair_objects[key] = pair
            all_pairs.append(diagnose_pair(pair, request.config))
        ranking = rank_initial_pairs(all_pairs, request.config)
        selected_pairs, selection_reasons = _select_pairs(all_pairs, request.extra_pairs, request.all_pairs)
        pair_root = output / "pair_diagnostics"
        diagnosed: list[dict[str, Any]] = []
        pair_by_key = {_pair_key(item["image_a"], item["image_b"]): item for item in all_pairs}
        for key in selected_pairs:
            pair = pair_objects[key]
            metrics = pair_by_key[key]
            pair_dir = pair_root / pair_directory_name(*key)
            artifacts = write_pair_visualizations(pair, request.image_path, pair_dir, metrics, request.config)
            _json_write(pair_dir / "spatial_grid.json", metrics["spatial_grid"])
            _json_write(pair_dir / "geometry_models.json", metrics["geometry_models"])
            _json_write(pair_dir / "pair_summary.json", metrics)
            diagnosed.append(
                {
                    **metrics,
                    "selection_reasons": selection_reasons[f"{key[0]} <-> {key[1]}"],
                    "artifacts": artifacts,
                }
            )
        ranking_artifacts = _write_ranking(ranking, output)
        current_model = analyze_current_model(request.model_path)
        _json_write(output / "current_model_geometry.json", current_model)
        available_images = {
            name: (int(image.width or 0), int(image.height or 0))
            for name, image in database.images.items()
        }
        # 示例配置作为包数据随安装分发，诊断报告可始终指向可用模板。
        template = Path(__file__).resolve().parents[1] / "examples" / "camera_intrinsics.example.json"
        intrinsics = load_and_validate_intrinsics(request.intrinsics_config, available_images, template_path=template)
        controlled = _controlled_experiment_plan(request, intrinsics, ranking)
        _json_write(output / "controlled_experiment_plan.json", controlled)
        unregistered = _unregistered_diagnostics(database, request.model_path, pair_by_key)
        _json_write(output / "unregistered_image_diagnostics.json", unregistered)
        findings, limitations = _global_findings(all_pairs, current_model)
        geometry = {
            "status": "complete",
            "database_path": str(request.database_path),
            "image_path": str(request.image_path),
            "model_path": str(request.model_path),
            "image_count": len(database.images),
            "all_pair_count": len(all_pairs),
            "diagnosed_pair_count": len(diagnosed),
            "default_pairs": [list(pair) for pair in DEFAULT_DIAGNOSTIC_PAIRS],
            "selected_pairs": diagnosed,
            "top_initial_pair_candidates": ranking[:5],
            "global_findings": findings,
            "limitations": limitations,
            "config": asdict(request.config),
            "artifacts": {
                "pair_diagnostics": str(pair_root),
                "initial_pair_ranking": ranking_artifacts,
                "current_model_geometry": str(output / "current_model_geometry.json"),
                "unregistered_image_diagnostics": str(output / "unregistered_image_diagnostics.json"),
                "controlled_experiment_plan": str(output / "controlled_experiment_plan.json"),
                "markdown_report": str(output / "geometry_diagnostics_report.md"),
            },
        }
    payload = {
        "geometry_schema_version": 1,
        "geometry_diagnostics": geometry,
        "current_model_geometry": current_model,
        "intrinsics_configuration": intrinsics,
        "controlled_experiments": controlled,
        "unregistered_image_diagnostics": unregistered,
    }
    report_path = output / "calibration_report.json"
    if report_path.is_file():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        existing = {}
    existing.setdefault("schema_version", 1)
    existing.update(payload)
    _json_write(report_path, existing)
    (output / "geometry_diagnostics_report.md").write_text(_markdown_report(payload), encoding="utf-8")
    return payload
