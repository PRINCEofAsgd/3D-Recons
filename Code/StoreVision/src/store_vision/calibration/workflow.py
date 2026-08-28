"""真实 COLMAP 稀疏标定实验的统一 calibration-demo 工作流。"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from store_vision.calibration.alignment import SimilarityTransform, align_point_sets
from store_vision.calibration.colmap_database import (
    MatchStatistics,
    draw_camera_match_graph,
    draw_top_verified_matches,
    read_match_statistics,
    write_match_statistics,
)
from store_vision.calibration.colmap_fisheye import (
    FittedFisheyeCalibration,
    apply_fitted_fisheye_to_database,
    discover_fitted_fisheye_calibration,
    load_fitted_fisheye_calibration,
)
from store_vision.calibration.colmap_parser import parse_text_model
from store_vision.calibration.colmap_runner import (
    ColmapNotFoundError,
    CommandResult,
    convert_model_to_text,
    detect_colmap,
    extract_features,
    match_features,
    query_version,
)
from store_vision.calibration.evaluation import evaluate_calibration
from store_vision.calibration.manual import find_manual_calibration, load_manual_calibration
from store_vision.calibration.models import CalibrationMetrics, CameraRecord, ReconstructionSummary
from store_vision.calibration.sfm_registration import run_multi_candidate_sfm
from store_vision.data.loader import load_store_inputs
from store_vision.resolution import read_image_size
from store_vision.reporting.calibration_report import console_summary, write_calibration_report

logger = logging.getLogger(__name__)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass
class SparseModelResult:
    """一个独立 COLMAP 模型及其文本解析结果。"""

    model_id: str
    binary_path: Path
    text_path: Path
    reconstruction: ReconstructionSummary


@dataclass
class CalibrationWorkflowResult:
    """calibration-demo 运行结果，同时作为控制台和 JSON 报告的数据来源。"""

    dataset: Path
    output: Path
    manual_camera_count: int
    colmap_status: str
    colmap_path: Path | None = None
    colmap_version: str | None = None
    total_images: int = 0
    image_names: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    commands: list[tuple[str, ...]] = field(default_factory=list)
    command_results: list[CommandResult] = field(default_factory=list)
    match_statistics: MatchStatistics | None = None
    sparse_models: list[SparseModelResult] = field(default_factory=list)
    reconstruction: ReconstructionSummary | None = None
    alignment: SimilarityTransform | None = None
    metrics: CalibrationMetrics | None = None
    known_intrinsics_experiment: dict[str, Any] = field(default_factory=dict)
    sfm_intrinsics: dict[str, Any] = field(default_factory=dict)
    sfm_registration: dict[str, Any] = field(default_factory=dict)
    report_path: Path | None = None


def _find_images(dataset: Path) -> Path:
    """优先定位 GUI screenshots 图像根目录，并保留旧布局兼容。"""

    candidates = (
        dataset / "screenshots",
        dataset / "cameras" / "images",
        dataset / "images",
        dataset,
    )
    for candidate in candidates:
        if candidate.is_dir() and any(
            path.suffix.lower() in IMAGE_SUFFIXES
            for path in candidate.rglob("*")
            if path.is_file()
        ):
            return candidate
    raise FileNotFoundError(f"No camera images found under {dataset}")


def _camera_images(image_root: Path, manual_records: list[CameraRecord]) -> list[Path]:
    """用人工标定的相机标识筛出真实相机图，排除平面图和 2.5D 示例图。"""

    candidates = sorted(
        path
        for path in image_root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    selected: list[Path] = []
    for record in manual_records:
        if record.image_path is not None:
            explicit = record.image_path
            if not explicit.is_absolute():
                explicit = image_root / explicit
            matches = [explicit] if explicit.is_file() else []
        else:
            matches = [
                path
                for path in candidates
                if path.name == record.name
                or path.stem == record.name
                or path.stem.startswith(record.name + "-")
            ]
        if len(matches) != 1:
            raise ValueError(
                f"Manual camera {record.name!r} matched {len(matches)} image files under {image_root}"
            )
        selected.append(matches[0].resolve())
    if len(set(selected)) != len(selected):
        raise ValueError("Multiple manual camera records resolved to the same image file")
    return sorted(selected, key=lambda path: path.name)


def _control_points(dataset: Path) -> tuple[np.ndarray, np.ndarray] | None:
    path = dataset / "calibration" / "control_points.json"
    if not path.is_file():
        path = dataset / "control_points.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = payload.get("colmap_points") or payload.get("source_points")
    target = payload.get("floor_points") or payload.get("target_points")
    if source is None or target is None:
        raise ValueError(f"control points must contain colmap_points and floor_points: {path}")
    return np.asarray(source, dtype=np.float64), np.asarray(target, dtype=np.float64)


def _append(result: CalibrationWorkflowResult, command_result: CommandResult) -> None:
    result.commands.append(command_result.command)
    result.command_results.append(command_result)


def _command_succeeded(command_result: CommandResult) -> bool:
    return command_result.dry_run or command_result.returncode == 0


def _version_text(command_result: CommandResult) -> str | None:
    for line in (command_result.stdout + "\n" + command_result.stderr).splitlines():
        if "COLMAP" in line:
            return line.strip()
    return None


def _recursive_values(payload: Any, names: set[str]) -> list[Any]:
    """按精确字段名搜索人工文件，避免把 modifyTime 等误判为 fy。"""

    values: list[Any] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in names:
                values.append(value)
            values.extend(_recursive_values(value, names))
    elif isinstance(payload, list):
        for value in payload:
            values.extend(_recursive_values(value, names))
    return values


def _known_intrinsics_plan(dataset: Path) -> dict[str, Any]:
    """检查人工文件，只生成下一轮已知内参实验配置，不在本轮执行。"""

    manual_path = find_manual_calibration(dataset)
    try:
        payload = json.loads(manual_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    field_names = {
        "fx": {"fx", "focal_x", "focal_length_x"},
        "fy": {"fy", "focal_y", "focal_length_y"},
        "cx": {"cx", "principal_x", "principal_point_x"},
        "cy": {"cy", "principal_y", "principal_point_y"},
        "distortion": {
            "distortion",
            "distortion_params",
            "k1",
            "k2",
            "k3",
            "k4",
        },
        "camera_model": {"camera_model", "model"},
        "camera_group": {"camera_group", "camera_group_id", "group", "group_id"},
    }
    found = {name: _recursive_values(payload, aliases) for name, aliases in field_names.items()}
    required_ready = all(
        found[name] for name in ("fx", "fy", "cx", "cy", "distortion")
    )
    return {
        "status": "ready" if required_ready else "missing_required_intrinsics",
        "source": str(manual_path),
        "available_fields": {name: bool(values) for name, values in found.items()},
        "observed_values": {name: values for name, values in found.items() if values},
        "proposed_second_round": {
            "execute_now": False,
            "camera_model": "OPENCV_FISHEYE",
            "required_params": ["fx", "fy", "cx", "cy", "k1", "k2", "k3", "k4"],
            "distortion_policy": "Use the shared fitted fisheye D instead of a D=0 pinhole fallback.",
            "grouping_policy": "All lenses use the same model and share K/D in a common reference pixel frame.",
            "matcher": "exhaustive_matcher",
            "reconstruction": "sparse_only",
        },
    }


def _write_known_intrinsics_plan(plan: dict[str, Any], reports_dir: Path) -> Path:
    path = reports_dir / "known_intrinsics_experiment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _sparse_directories(sparse: Path) -> list[Path]:
    if not sparse.is_dir():
        return []
    return sorted(
        (
            path
            for path in sparse.iterdir()
            if path.is_dir() and (path / "cameras.bin").is_file() and (path / "images.bin").is_file()
        ),
        key=lambda path: (not path.name.isdigit(), int(path.name) if path.name.isdigit() else path.name),
    )


def _convert_sparse_models(
    result: CalibrationWorkflowResult,
    sparse: Path,
    model_text: Path,
    logs_dir: Path,
) -> None:
    binary_models = _sparse_directories(sparse)
    for index, binary_model in enumerate(binary_models):
        text_path = model_text if len(binary_models) == 1 else model_text / "models" / binary_model.name
        converted = convert_model_to_text(
            binary_model,
            text_path,
            executable=result.colmap_path,
            log_path=logs_dir / f"{4 + index:02d}_model_converter_{binary_model.name}.log",
        )
        _append(result, converted)
        if not _command_succeeded(converted):
            result.warnings.append(
                f"model_converter failed for sparse model {binary_model.name}: {converted.stderr.strip()}"
            )
            continue
        try:
            reconstruction = parse_text_model(text_path)
        except (FileNotFoundError, ValueError) as exc:
            result.warnings.append(f"Sparse model {binary_model.name} could not be parsed: {exc}")
            continue
        result.sparse_models.append(
            SparseModelResult(binary_model.name, binary_model, text_path, reconstruction)
        )
    if not result.sparse_models:
        return
    primary = max(
        result.sparse_models,
        key=lambda item: (item.reconstruction.registered_images, item.reconstruction.points3d),
    )
    result.reconstruction = primary.reconstruction
    if len(binary_models) > 1:
        # 根 model_txt 始终暴露最大模型的三个标准文本文件，分模型文本仍完整保留。
        model_text.mkdir(parents=True, exist_ok=True)
        for filename in ("cameras.txt", "images.txt", "points3D.txt"):
            shutil.copy2(primary.text_path / filename, model_text / filename)


def _registered_names(result: CalibrationWorkflowResult) -> list[str]:
    return sorted(
        {
            record.name
            for model in result.sparse_models
            for record in model.reconstruction.images.values()
        }
    )


def _aggregate_reconstruction(result: CalibrationWorkflowResult) -> ReconstructionSummary | None:
    if not result.sparse_models:
        return None
    registered = _registered_names(result)
    points = sum(model.reconstruction.points3d for model in result.sparse_models)
    weighted_error_sum = sum(
        (model.reconstruction.mean_reprojection_error or 0.0) * model.reconstruction.points3d
        for model in result.sparse_models
    )
    mean_error = weighted_error_sum / points if points else None
    return ReconstructionSummary(
        registered_images=len(registered),
        points3d=points,
        mean_reprojection_error=mean_error,
    )


def _match_failure_warnings(result: CalibrationWorkflowResult, mapper_result: CommandResult | None) -> None:
    statistics = result.match_statistics
    if statistics is None:
        result.warnings.append("Matching statistics are unavailable because no readable experiment database exists.")
        return
    if not statistics.effective_pairs:
        result.warnings.append("No image pair passed geometric verification; sparse initialization has no valid edge.")
    if not statistics.is_connected:
        result.warnings.append(
            f"Verified match graph is disconnected ({len(statistics.connected_components)} components); "
            f"isolated images: {statistics.isolated_images}."
        )
    if mapper_result is not None and not _command_succeeded(mapper_result):
        tail = " ".join(mapper_result.stderr.strip().splitlines()[-4:])
        result.warnings.append(f"Mapper exited with code {mapper_result.returncode}: {tail}")
    registered_count = len(_registered_names(result))
    if result.sparse_models and registered_count < result.total_images:
        result.warnings.append(
            f"Mapper registered only {registered_count}/{result.total_images} images even though the "
            f"verified match graph is {'connected' if statistics.is_connected else 'disconnected'}; "
            "graph connectivity alone does not guarantee a stable multi-view geometry."
        )
        mapper_output = mapper_result.stderr if mapper_result is not None else ""
        if "bad initial pair" in mapper_output or "No good initial image pair" in mapper_output:
            result.warnings.append(
                "Mapper explicitly rejected bad initial pairs and reported no good initial pair during "
                "several attempts before keeping the partial reconstruction."
            )
        result.warnings.append(
            "The partial registration is consistent with weak usable overlap, repeated retail texture, "
            "or a mostly planar scene; inspect the strongest verified-pair visualizations before changing "
            "initialization thresholds."
        )
    if not result.sparse_models:
        result.warnings.append(
            "Mapper produced no parseable sparse model. If verified edges exist, likely causes include an "
            "initial-pair failure, insufficient overlap, repeated texture, or planar-scene degeneracy."
        )
        result.warnings.append(
            "Next round should first inspect the strongest verified pairs, then try documented mapper "
            "initialization thresholds or measured intrinsics; do not start a broad parameter sweep."
        )


def _sparse_model_payload(model: SparseModelResult) -> dict[str, Any]:
    cameras: list[dict[str, Any]] = []
    for image_id, record in sorted(model.reconstruction.images.items(), key=lambda item: item[1].name):
        pose = record.pose
        intrinsics = record.intrinsics
        cameras.append(
            {
                "image_id": image_id,
                "image_name": record.name,
                "camera_id": record.camera_id,
                "camera_model": intrinsics.model if intrinsics else None,
                "camera_params": list(intrinsics.params) if intrinsics else None,
                "quaternion_wxyz": list(pose.quaternion_wxyz) if pose else None,
                "translation_xyz": list(pose.translation_xyz) if pose else None,
                "camera_center_xyz": list(pose.camera_center()) if pose else None,
            }
        )
    return {
        "model_id": model.model_id,
        "binary_path": str(model.binary_path),
        "text_path": str(model.text_path),
        "registered_images": [camera["image_name"] for camera in cameras],
        "registered_image_count": model.reconstruction.registered_images,
        "sparse_points": model.reconstruction.points3d,
        "mean_reprojection_error_px": model.reconstruction.mean_reprojection_error,
        "cameras": cameras,
    }


def _reconstruction_payload(result: CalibrationWorkflowResult) -> dict[str, Any]:
    registered_names = _registered_names(result)
    aggregate = _aggregate_reconstruction(result)
    return {
        "input_images": result.total_images,
        "registered_images": len(registered_names),
        "unregistered_images": sorted(set(result.image_names) - set(registered_names)),
        "sparse_points": aggregate.points3d if aggregate else 0,
        "mean_reprojection_error_px": aggregate.mean_reprojection_error if aggregate else None,
        "multiple_independent_models": len(result.sparse_models) > 1,
        "model_count": len(result.sparse_models),
        "models": [_sparse_model_payload(model) for model in result.sparse_models],
    }


def _calibration_report_payload(result: CalibrationWorkflowResult) -> dict[str, Any]:
    reconstruction = _reconstruction_payload(result)
    statistics = result.match_statistics
    return {
        # GUI 通过此字段选择兼容解析器；后续报告结构升级时递增。
        "schema_version": 2,
        "dataset": str(result.dataset),
        "output": str(result.output),
        "manual_camera_count": result.manual_camera_count,
        "colmap_status": result.colmap_status,
        "colmap_version": result.colmap_version,
        "colmap_path": str(result.colmap_path) if result.colmap_path else None,
        "total_images": result.total_images,
        "registered_images": reconstruction["registered_images"],
        "unregistered_images": reconstruction["unregistered_images"],
        "feature_count_by_image": statistics.feature_count_by_image if statistics else {},
        "verified_matches_by_pair": statistics.verified_matches_by_pair if statistics else {},
        "connected_components": len(statistics.connected_components) if statistics else 0,
        "sparse_models": reconstruction["models"],
        "sparse_points": reconstruction["sparse_points"],
        "mean_reprojection_error_px": reconstruction["mean_reprojection_error_px"],
        "warnings": result.warnings,
        "diagnostics": result.diagnostics,
        "commands": [
            {
                "argv": list(command.command),
                "returncode": command.returncode,
                "stdout_log": str(command.log_path) if command.log_path else None,
            }
            for command in result.command_results
        ],
        "known_intrinsics_experiment": result.known_intrinsics_experiment,
        "sfm_intrinsics": result.sfm_intrinsics,
        "sfm_registration": result.sfm_registration,
        # V0.3.0 由独立 geometry-diagnostics 命令填充；旧工作流先保留稳定字段。
        "geometry_diagnostics": {"status": "not_run"},
        "current_model_geometry": {"status": "not_run"},
        "intrinsics_configuration": {
            "status": result.known_intrinsics_experiment.get("status", "missing_required_intrinsics")
        },
        "controlled_experiments": {"status": "not_planned"},
        "unregistered_image_diagnostics": {"status": "not_run"},
        "alignment": asdict(result.alignment) if result.alignment else None,
        "metrics": asdict(result.metrics) if result.metrics else None,
    }


def _write_reports(result: CalibrationWorkflowResult, reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    _write_known_intrinsics_plan(result.known_intrinsics_experiment, reports_dir)
    if result.sfm_intrinsics:
        (reports_dir / "sfm_intrinsics.json").write_text(
            json.dumps(result.sfm_intrinsics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if result.sfm_registration:
        # 多候选模块已写入完整候选矩阵；这里额外保留稳定摘要供 GUI 快速读取。
        (reports_dir / "sfm_registration_summary.json").write_text(
            json.dumps(result.sfm_registration, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    reconstruction_path = reports_dir / "reconstruction_summary.json"
    reconstruction_path.write_text(
        json.dumps(_reconstruction_payload(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result.report_path = reports_dir / "calibration_report.json"
    write_calibration_report(_calibration_report_payload(result), result.report_path)


def run_calibration_workflow(
    dataset: str | Path,
    output: str | Path,
    *,
    dry_run: bool = False,
    skip_colmap: bool = False,
    fisheye_calibration: str | Path | None = None,
) -> CalibrationWorkflowResult:
    """运行鱼眼内参门禁、多候选 SfM、位姿辅助恢复和三维证据验收。"""

    dataset_path = Path(dataset).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if not dataset_path.is_dir():
        raise FileNotFoundError(f"Dataset directory not found: {dataset_path}")
    logger.info("Calibration demo: loading dataset %s", dataset_path)
    manual_path = find_manual_calibration(dataset_path)
    manual_records = load_manual_calibration(manual_path)
    image_root = _find_images(dataset_path)
    input_resolution_diagnostics: list[dict[str, Any]] = []
    if manual_path.name.lower() in {"cali.txt", "cali.json"}:
        # 同一设备存在多张快照时复用正式 loader 的快照号/尺寸联合配对，
        # 避免 _camera_images 因多匹配报错或按目录顺序选择错误图片。
        candidates = sorted(
            path
            for path in image_root.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        selected_inputs = load_store_inputs(manual_path, None, candidates)
        camera_images = sorted(
            (
                Path(camera.image_path)
                for camera in selected_inputs.camera_list()
                if camera.image_path is not None
            ),
            key=lambda path: path.name,
        )
        input_resolution_diagnostics = selected_inputs.input_diagnostics
    else:
        camera_images = _camera_images(image_root, manual_records)
    image_names = [str(path.relative_to(image_root)) for path in camera_images]
    image_resolutions = {
        size for path in camera_images if (size := read_image_size(path)) is not None
    }
    # COLMAP 的一个 camera 记录具有固定宽高。混合分辨率先逐图建记录，
    # 特征提取后再按分辨率合并，并由参考 K 按宽高比例生成各组参数。
    share_one_colmap_camera = len(image_resolutions) == 1
    result = CalibrationWorkflowResult(
        dataset=dataset_path,
        output=output_path,
        manual_camera_count=len(manual_records),
        colmap_status="skipped" if skip_colmap else "pending",
        total_images=len(camera_images),
        image_names=image_names,
        known_intrinsics_experiment=_known_intrinsics_plan(dataset_path),
    )
    result.diagnostics.append(
        (
            f"COLMAP 图片分辨率：{sorted(image_resolutions)}；"
            + (
                "尺寸一致，将使用单一共享鱼眼 camera 记录。"
                if share_one_colmap_camera
                else "检测到混合尺寸，将按分辨率建立共享鱼眼 camera 组。"
            )
        )
    )
    resolution_errors = [
        row
        for row in input_resolution_diagnostics
        if row.get("severity") == "error"
    ]
    if resolution_errors:
        result.warnings.append(
            f"输入预检发现 {len(resolution_errors)} 项标定坐标/分辨率错误；"
            "Sfm 特征处理仍可运行，但这些相机不能作为可信平面几何约束。"
        )
    reports_dir = output_path / "reports"
    logs_dir = output_path / "logs"
    database = output_path / "database.db"
    sparse = output_path / "sparse"
    model_text = output_path / "model_txt"
    image_list = output_path / "input_images.txt"

    if not dry_run and not skip_colmap and database.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing experiment database: {database}. Use a new run directory."
        )

    mapper_result: CommandResult | None = None
    fitted_intrinsics: FittedFisheyeCalibration | None = None
    if skip_colmap:
        result.diagnostics.append("COLMAP execution skipped by request; manual calibration remains available.")
    else:
        result.colmap_path = detect_colmap()
        if result.colmap_path is None and not dry_run:
            result.colmap_status = "unavailable"
            result.diagnostics.append(
                "COLMAP executable was not found. Install COLMAP and ensure `colmap` is on PATH, "
                "or use --dry-run/--skip-colmap."
            )
        else:
            if not dry_run:
                output_path.mkdir(parents=True, exist_ok=True)
                image_list.write_text("\n".join(image_names) + "\n", encoding="utf-8")
                try:
                    version_result = query_version(
                        executable=result.colmap_path,
                        log_path=logs_dir / "00_colmap_version.log",
                    )
                    _append(result, version_result)
                    result.colmap_version = _version_text(version_result)
                except ColmapNotFoundError as exc:
                    result.colmap_status = "unavailable"
                    result.diagnostics.append(str(exc))

            if result.colmap_status != "unavailable":
                fitted_source = (
                    Path(fisheye_calibration).expanduser().resolve()
                    if fisheye_calibration is not None
                    else discover_fitted_fisheye_calibration(dataset_path, output_path)
                )
                if fitted_source is None:
                    raise FileNotFoundError(
                        "Sfm 需要先运行同一数据集的鱼眼联合标定，或通过 "
                        "--fisheye-calibration 指定包含拟合 K/D 的运行目录"
                    )
                fitted_intrinsics = load_fitted_fisheye_calibration(
                    fitted_source,
                    expected_dataset=dataset_path,
                )
                result.sfm_intrinsics = fitted_intrinsics.report_payload()
                result.known_intrinsics_experiment = {
                    **result.sfm_intrinsics,
                    "proposed_second_round": {
                        "execute_now": True,
                        "camera_model": "OPENCV_FISHEYE",
                        "required_params": [
                            "fx",
                            "fy",
                            "cx",
                            "cy",
                            "k1",
                            "k2",
                            "k3",
                            "k4",
                        ],
                        "source": str(fitted_intrinsics.report_path),
                    },
                }
                if not fitted_intrinsics.fit_accepted or not fitted_intrinsics.ba_accepted:
                    result.warnings.append(
                        "拟合 K/D 的重投影阈值未完全通过；原始拟合参数不会被当作"
                        "可信标定结果。"
                    )
                if fitted_intrinsics.routing_source != "fitted":
                    result.warnings.append(
                        "拟合 K/D 被物理可信度门禁拒绝；本轮 SfM 使用初始 K 与"
                        f"缩放 {fitted_intrinsics.distortion_scale:.6g} 倍的非零 D "
                        "建立试算候选，不能标记为可信标定。"
                    )
                result.diagnostics.append(
                    "Sfm 已加载共享鱼眼参数："
                    f"{fitted_intrinsics.report_path}；路由={fitted_intrinsics.routing_source}；"
                    "所有候选均固定 K/D。"
                )
                common_camera_params = (
                    fitted_intrinsics.parameters_for_resolution(next(iter(image_resolutions)))
                    if share_one_colmap_camera and image_resolutions
                    else None
                )
                feature_result = extract_features(
                    database,
                    image_root,
                    dry_run=dry_run,
                    executable=result.colmap_path,
                    image_list_path=image_list,
                    # Sfm 仅作辅助诊断，但相机模型仍与正式同型号鱼眼路线一致。
                    camera_model="OPENCV_FISHEYE",
                    camera_params=common_camera_params,
                    single_camera=share_one_colmap_camera,
                    log_path=logs_dir / "01_feature_extractor.log",
                )
                _append(result, feature_result)
                if _command_succeeded(feature_result):
                    if not dry_run:
                        matching_source = (
                            "fitted"
                            if fitted_intrinsics.routing_source != "fitted"
                            else "active"
                        )
                        matching_intrinsics = apply_fitted_fisheye_to_database(
                            database,
                            fitted_intrinsics,
                            parameter_source=matching_source,
                        )
                        result.sfm_intrinsics = {
                            **matching_intrinsics,
                            "matching_intrinsics_source": matching_source,
                            "matching_intrinsics_policy": (
                                "被门禁拒绝的拟合 K/D 只用于生成二视图几何候选；"
                                "匹配完成后立即恢复物理可逆活动 K/D，所有重建和"
                                "最终 BA 均使用活动参数并接受三维门禁。"
                                if matching_source == "fitted"
                                else "匹配与重建均使用可信拟合 K/D。"
                            ),
                        }
                        result.known_intrinsics_experiment.update(result.sfm_intrinsics)
                    matcher_result = match_features(
                        database,
                        dry_run=dry_run,
                        executable=result.colmap_path,
                        log_path=logs_dir / "02_exhaustive_matcher.log",
                    )
                    _append(result, matcher_result)
                    if not dry_run:
                        # 原始拟合参数即使有助于提出更多二视图边，也不得进入
                        # Mapper/三角化/BA；在任何重建命令前恢复活动可逆 K/D。
                        active_intrinsics = apply_fitted_fisheye_to_database(
                            database,
                            fitted_intrinsics,
                            parameter_source="active",
                        )
                        result.sfm_intrinsics = {
                            **active_intrinsics,
                            "matching_intrinsics_source": (
                                "fitted"
                                if fitted_intrinsics.routing_source != "fitted"
                                else "active"
                            ),
                            "matching_intrinsics_policy": (
                                "原始拟合 K/D 仅用于二视图候选生成；重建已恢复"
                                "物理可逆活动 K/D。"
                                if fitted_intrinsics.routing_source != "fitted"
                                else "匹配与重建均使用可信拟合 K/D。"
                            ),
                            "mapping_intrinsics_source": "active",
                        }
                else:
                    matcher_result = None
                    result.warnings.append(
                        f"feature_extractor failed with code {feature_result.returncode}: "
                        f"{feature_result.stderr.strip()}"
                    )

                if not dry_run and database.is_file():
                    try:
                        result.match_statistics = read_match_statistics(database)
                        write_match_statistics(
                            result.match_statistics,
                            reports_dir / "match_statistics.json",
                            reports_dir / "match_statistics.csv",
                        )
                        draw_camera_match_graph(
                            result.match_statistics, reports_dir / "camera_match_graph.png"
                        )
                        draw_top_verified_matches(
                            database,
                            image_root,
                            result.match_statistics,
                            reports_dir / "matches",
                        )
                    except (FileNotFoundError, OSError, ValueError, sqlite3.DatabaseError) as exc:
                        result.warnings.append(f"Match diagnostics could not be generated: {exc}")

                if matcher_result is not None and _command_succeeded(matcher_result):
                    sfm_execution = run_multi_candidate_sfm(
                        database,
                        image_root,
                        output_path,
                        image_names,
                        fitted_intrinsics.report_path,
                        dry_run=dry_run,
                        executable=result.colmap_path,
                    )
                    for command_result in sfm_execution.command_results:
                        _append(result, command_result)
                    result.warnings.extend(sfm_execution.warnings)
                    mapper_result = next(
                        (
                            command
                            for command in sfm_execution.command_results
                            if len(command.command) > 1
                            and command.command[1] == "global_mapper"
                        ),
                        None,
                    )
                    if not dry_run:
                        selected = sfm_execution.selected
                        result.sfm_registration = {
                            "status": (
                                "all_camera_visual"
                                if selected is not None
                                and selected.full_visual_registration
                                else (
                                    "partial_visual"
                                    if selected is not None
                                    else "failed"
                                )
                            ),
                            "selected_candidate": (
                                selected.candidate if selected is not None else None
                            ),
                            "full_visual_registration": bool(
                                selected is not None
                                and selected.full_visual_registration
                            ),
                            "visually_registered_images": (
                                selected.visually_supported_images
                                if selected is not None
                                else []
                            ),
                            "weak_or_prior_only_images": (
                                selected.weak_or_prior_only_images
                                if selected is not None
                                else []
                            ),
                            "unregistered_images": (
                                selected.unregistered_images
                                if selected is not None
                                else image_names
                            ),
                            "sparse_points": (
                                selected.sparse_points if selected is not None else 0
                            ),
                            "total_point3d_observations": (
                                selected.total_point3d_observations
                                if selected is not None
                                else 0
                            ),
                            "per_image_point3d_observations": (
                                selected.per_image_point3d_observations
                                if selected is not None
                                else {}
                            ),
                            "mean_track_length": (
                                selected.mean_track_length
                                if selected is not None
                                else None
                            ),
                            "mean_reprojection_error_px": (
                                selected.mean_reprojection_error_px
                                if selected is not None
                                else None
                            ),
                            "candidate_count": len(sfm_execution.candidates),
                            "pose_seed": sfm_execution.pose_seed,
                            "comparison_report": str(
                                reports_dir / "sfm_registration.json"
                            ),
                        }
                        if selected is not None:
                            reconstruction = parse_text_model(model_text)
                            result.sparse_models = [
                                SparseModelResult(
                                    selected.candidate,
                                    sparse / "0",
                                    model_text,
                                    reconstruction,
                                )
                            ]
                elif matcher_result is not None:
                    result.warnings.append(
                        f"exhaustive_matcher failed with code {matcher_result.returncode}: "
                        f"{matcher_result.stderr.strip()}"
                    )

                if dry_run:
                    result.colmap_status = "dry-run"
                elif result.sparse_models:
                    result.colmap_status = (
                        "completed"
                        if result.sfm_registration.get("full_visual_registration")
                        else "partial"
                    )
                else:
                    result.colmap_status = "failed"

    points = _control_points(dataset_path)
    if points is not None:
        logger.info("Calibration demo: aligning control points")
        result.alignment = align_point_sets(*points)
        aligned_points = result.alignment.apply(points[0])
        if points[1].shape[1] == 2:
            result.metrics = evaluate_calibration(
                reference_projections=points[1], estimated_projections=aligned_points
            )
        else:
            result.metrics = evaluate_calibration(
                reference_positions=points[1], estimated_positions=aligned_points
            )
    else:
        result.diagnostics.append(
            "No control_points.json; alignment and comparative metrics were not computed."
        )
        result.metrics = evaluate_calibration()

    if dry_run:
        result.diagnostics.append("Dry-run only: no database, model or report was generated.")
    else:
        if not skip_colmap:
            _match_failure_warnings(result, mapper_result)
        _write_reports(result, reports_dir)
        logger.info("Calibration demo: report written to %s", result.report_path)
    print(console_summary(_calibration_report_payload(result)))
    for diagnostic in result.diagnostics:
        print(f"Diagnostic: {diagnostic}")
    return result
