"""统一相机标定 GUI 的结果发现、报告归一化和运行目录管理。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class WorkspaceMetric:
    """概览页展示的一项真实指标。"""

    label: str
    value: str
    state: str = "neutral"
    detail: str = ""


@dataclass(frozen=True)
class WorkspaceStage:
    """一个标定阶段及其 ready/blocked/failed 等状态。"""

    key: str
    title: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class PairDiagnosticView:
    """一组图像对诊断的指标和可视化文件。"""

    name: str
    directory: Path
    summary: dict[str, Any]
    images: dict[str, Path]


@dataclass(frozen=True)
class SharedCalibrationView:
    """共享鱼眼 K/D、逐机位姿和 BA 报告的稳定只读视图。"""

    result_path: Path
    report: dict[str, Any]
    models: dict[str, dict[str, Any]]
    selected_model: str | None
    selected_K: list[list[float]] | None
    selected_D: list[float] | None
    poses: list[dict[str, Any]]
    track_validation: dict[str, Any]
    visualizations: dict[str, Path]


@dataclass
class CalibrationWorkspaceSnapshot:
    """将共享标定与既有 COLMAP 分散产物归一化后的只读 GUI 数据。"""

    dataset_path: Path | None
    experiment_path: Path | None
    report_roots: tuple[Path, ...]
    shared_result_path: Path | None = None
    shared: SharedCalibrationView | None = None
    sfm_intrinsics: dict[str, Any] = field(default_factory=dict)
    sfm_registration: dict[str, Any] = field(default_factory=dict)
    metrics: list[WorkspaceMetric] = field(default_factory=list)
    stages: list[WorkspaceStage] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    cameras: list[dict[str, Any]] = field(default_factory=list)
    pairs: list[PairDiagnosticView] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_project_root(path: str | Path | None) -> Path | None:
    """从数据或实验目录向上定位包含正式 Python 项目的仓库根目录。"""

    if path is None:
        return None
    current = Path(path).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        # 以新的单层 code/ 打包配置识别仓库，避免误认普通数据目录。
        if (candidate / "code" / "pyproject.toml").is_file():
            return candidate
    return None


def create_unique_run_directory(output_root: str | Path, prefix: str = "run") -> Path:
    """生成不覆盖旧数据库的时间戳运行目录；这里只分配路径，不创建文件。"""

    root = Path(output_root).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"{prefix}_{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = root / f"{prefix}_{stamp}_{suffix:02d}"
        suffix += 1
    return candidate


def create_unique_analysis_directory(experiment_path: str | Path) -> Path:
    """为一次 GUI 安全分析分配独立目录，避免改写既有报告。"""

    return create_unique_run_directory(Path(experiment_path) / "analysis", "analysis")


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    """读取一个 JSON 对象并把局部失败收集为界面告警。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"无法读取 {path}: {exc}")
        return None
    if not isinstance(payload, dict):
        warnings.append(f"报告不是 JSON 对象：{path}")
        return None
    return payload


def _unique_existing(paths: list[Path]) -> tuple[Path, ...]:
    """保留按优先级排列且真实存在的不重复目录。"""

    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return tuple(result)


def _repository_reports_match_dataset(report_root: Path, dataset: Path | None) -> bool:
    """仅为同一数据集自动兼容旧仓库级 V0.4 报告，防止跨门店串用。"""

    if dataset is None:
        return False
    inventory_path = report_root / "calibration_data_inventory.json"
    if not inventory_path.is_file():
        return False
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    audited = payload.get("audited_dataset_files", []) if isinstance(payload, dict) else []
    for value in audited if isinstance(audited, list) else []:
        try:
            if Path(value).expanduser().resolve().is_relative_to(dataset):
                return True
        except (OSError, TypeError, ValueError):
            continue
    return False


def discover_report_roots(
    dataset_path: str | Path | None,
    experiment_path: str | Path | None,
    additional_report_path: str | Path | None = None,
    *,
    include_legacy_reports: bool = True,
) -> tuple[Path, ...]:
    """发现实验报告与最新 GUI 分析，并按需兼容仓库级旧报告。"""

    candidates: list[Path] = []
    experiment = Path(experiment_path).expanduser().resolve() if experiment_path else None
    if experiment is not None:
        candidates.append(experiment / "reports")
        analysis_root = experiment / "analysis"
        if analysis_root.is_dir():
            # 最新分析优先，使重新运行后界面立即展示新结果。
            candidates.extend(
                sorted(
                    (path for path in analysis_root.iterdir() if path.is_dir()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
    if additional_report_path:
        candidates.insert(0, Path(additional_report_path))
    dataset = Path(dataset_path).expanduser().resolve() if dataset_path else None
    project_root = find_project_root(dataset_path or experiment_path)
    if include_legacy_reports and project_root is not None:
        legacy_reports = project_root / "reports"
        if _repository_reports_match_dataset(legacy_reports, dataset):
            candidates.append(legacy_reports)
    return _unique_existing(candidates)


def _shared_report_matches_dataset(
    result_path: Path,
    dataset: Path | None,
    warnings: list[str],
) -> bool:
    """核对共享标定报告的输入目录，拒绝把其他门店结果混入当前页面。"""

    report_path = result_path / "shared_calibration_report.json"
    if not report_path.is_file():
        return False
    payload = _read_json(report_path, warnings)
    if payload is None:
        return False
    value = payload.get("input", {}).get("dataset_path")
    if dataset is None or not value:
        warnings.append(f"共享标定报告缺少可核对的数据集路径：{report_path}")
        return False
    try:
        matches = Path(value).expanduser().resolve() == dataset
    except (OSError, TypeError, ValueError):
        matches = False
    if not matches:
        warnings.append(
            f"已忽略其他门店的共享标定结果：{result_path}（报告数据集：{value}）"
        )
    return matches


def discover_shared_result(
    dataset_path: str | Path | None,
    explicit_result_path: str | Path | None = None,
    *,
    warnings: list[str] | None = None,
) -> Path | None:
    """优先使用显式结果，否则发现当前门店最新且身份匹配的共享标定运行。"""

    collected = warnings if warnings is not None else []
    dataset = Path(dataset_path).expanduser().resolve() if dataset_path else None
    candidates: list[Path] = []
    if explicit_result_path:
        explicit = Path(explicit_result_path).expanduser().resolve()
        if not (explicit / "shared_calibration_report.json").is_file():
            collected.append(f"共享标定目录缺少主报告：{explicit}")
            return None
        candidates.append(explicit)
    elif dataset is not None:
        project_root = find_project_root(dataset)
        if project_root is not None:
            for shared_root in (
                project_root / "outputs" / dataset.name / "fisheye_calibration",
                project_root / "outputs" / dataset.name / "shared_intrinsics",
            ):
                if shared_root.is_dir():
                    candidates.extend(
                        sorted(
                            (path for path in shared_root.iterdir() if path.is_dir()),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                    )
    for candidate in candidates:
        if _shared_report_matches_dataset(candidate, dataset, collected):
            return candidate
    return None


def _load_shared_view(
    result_path: Path | None,
    warnings: list[str],
) -> SharedCalibrationView | None:
    """将共享标定主报告归一化为页面所需的模型、位姿和可视化集合。"""

    if result_path is None:
        return None
    report = _read_json(result_path / "shared_calibration_report.json", warnings)
    if report is None:
        return None
    models = report.get("intrinsics_models", {})
    poses = report.get("poses", [])
    selection = report.get("selection", {})
    track_validation = report.get("feature_track_plane_validation", {})
    visualizations = {
        key: path
        for key, filename in (
            ("homography", "homography_mapping.png"),
            ("camera_pose", "camera_pose_map.png"),
            ("height", "height_distribution.png"),
        )
        if (path := result_path / filename).is_file()
    }
    return SharedCalibrationView(
        result_path=result_path,
        report=report,
        models=models if isinstance(models, dict) else {},
        selected_model=selection.get("selected_model") if isinstance(selection, dict) else None,
        selected_K=selection.get("selected_K") if isinstance(selection, dict) else None,
        selected_D=(
            selection.get("selected_D", selection.get("distortion_D"))
            if isinstance(selection, dict)
            else None
        ),
        poses=[row for row in poses if isinstance(row, dict)] if isinstance(poses, list) else [],
        track_validation=track_validation if isinstance(track_validation, dict) else {},
        visualizations=visualizations,
    )


def _newest_payload(
    roots: tuple[Path, ...],
    filename: str,
    warnings: list[str],
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """从多个报告根目录返回满足条件的最新同名 JSON。"""

    paths = sorted(
        (root / filename for root in roots if (root / filename).is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        payload = _read_json(path, warnings)
        if payload is not None and (predicate is None or predicate(payload)):
            return payload
    return {}


def _experiment_report(experiment: Path | None, warnings: list[str]) -> dict[str, Any]:
    """读取 V0.2 实验自己的主报告，不被后续分析报告遮蔽。"""

    if experiment is None:
        return {}
    path = experiment / "reports" / "calibration_report.json"
    return _read_json(path, warnings) or {} if path.is_file() else {}


def _value_count(value: Any) -> int | None:
    """把列表、字典或整数统一转换为可展示的数量。"""

    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    if isinstance(value, int):
        return value
    return None


def _metric_state(value: int | None, expected: int | None = None) -> str:
    """按是否达到期望数量决定概览指标颜色。"""

    if value is None:
        return "neutral"
    if expected is not None and value < expected:
        return "warning"
    return "good"


def _camera_rows(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """过滤盘点报告中的非法相机行。"""

    rows = inventory.get("cameras", [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _find_pairs(roots: tuple[Path, ...], warnings: list[str]) -> list[PairDiagnosticView]:
    """发现并去重图像对指标及四类可视化。"""

    pairs: list[PairDiagnosticView] = []
    seen_names: set[str] = set()
    for root in roots:
        pair_root = root / "pair_diagnostics"
        if not pair_root.is_dir():
            continue
        for directory in sorted(path for path in pair_root.iterdir() if path.is_dir()):
            if directory.name in seen_names:
                continue
            summary = _read_json(directory / "pair_summary.json", warnings) or {}
            images = {
                key: path
                for key, filename in (
                    ("raw", "raw_matches.png"),
                    ("verified", "verified_matches.png"),
                    ("grid", "verified_matches_grid.png"),
                    ("displacement", "displacement_vectors.png"),
                )
                if (path := directory / filename).is_file()
            }
            if images or summary:
                seen_names.add(directory.name)
                pairs.append(PairDiagnosticView(directory.name, directory, summary, images))
    return pairs


def _find_artifacts(experiment: Path | None, roots: tuple[Path, ...]) -> list[Path]:
    """收集可在 GUI 预览的报告、图片、模型文本和日志。"""

    suffixes = {".json", ".csv", ".md", ".txt", ".log", ".png", ".jpg", ".jpeg"}
    search_roots = list(roots)
    if experiment is not None:
        search_roots.extend([experiment / "logs", experiment / "model_txt"])
    artifacts: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            resolved = path.resolve()
            if path.is_file() and path.suffix.lower() in suffixes and resolved not in seen:
                seen.add(resolved)
                artifacts.append(resolved)
    return artifacts


def load_calibration_workspace(
    dataset_path: str | Path | None,
    experiment_path: str | Path | None,
    additional_report_path: str | Path | None = None,
    shared_result_path: str | Path | None = None,
    *,
    auto_discover_shared: bool = True,
    include_legacy_reports: bool = True,
) -> CalibrationWorkspaceSnapshot:
    """加载指定的共享标定和实验产物；局部损坏只形成告警。"""

    dataset = Path(dataset_path).expanduser().resolve() if dataset_path else None
    experiment = Path(experiment_path).expanduser().resolve() if experiment_path else None
    warnings: list[str] = []
    shared_path = (
        discover_shared_result(dataset, shared_result_path, warnings=warnings)
        if auto_discover_shared or shared_result_path
        else None
    )
    shared = _load_shared_view(shared_path, warnings)
    roots = discover_report_roots(
        dataset,
        experiment,
        additional_report_path,
        include_legacy_reports=include_legacy_reports,
    )
    if shared_path is not None:
        roots = _unique_existing([shared_path, *roots])
    experiment_report = _experiment_report(experiment, warnings)
    geometry_report = _newest_payload(
        roots,
        "calibration_report.json",
        warnings,
        lambda payload: isinstance(payload.get("geometry_diagnostics"), dict),
    )
    geometry = geometry_report.get("geometry_diagnostics", {})
    inventory = _newest_payload(roots, "calibration_data_inventory.json", warnings)
    tracks = _newest_payload(roots, "feature_track_summary.json", warnings)
    baseline = _newest_payload(roots, "manual_calibration_baseline.json", warnings)
    match_stats = _newest_payload(roots, "match_statistics.json", warnings)

    total_images = _value_count(experiment_report.get("total_images")) or experiment_report.get("total_images")
    registered = _value_count(experiment_report.get("registered_images"))
    sparse_points = experiment_report.get("sparse_points")
    pairs = match_stats.get("pairs", []) if isinstance(match_stats.get("pairs"), list) else []
    verified_pairs = sum(
        1 for pair in pairs if isinstance(pair, dict) and int(pair.get("verified_matches", 0) or 0) > 0
    )
    physical_cameras = inventory.get("physical_camera_count")
    mapping_count = len(inventory.get("cameras", [])) if inventory.get("mapping_confirmed") else 0
    clean_tracks = tracks.get("cleaned_track_count")
    long_tracks = tracks.get("length_at_least_3")
    baseline_status = str(baseline.get("status", "未生成"))

    shared_summary = shared.report.get("summary", {}) if shared else {}
    shared_scale = shared.report.get("scale", {}) if shared else {}
    shared_identity = shared.report.get("identity_audit", {}) if shared else {}
    shared_tracks = shared.track_validation if shared else {}
    selected_model = shared.selected_model if shared else None
    selected_k = shared.selected_K if shared else None
    selected_focal = (
        f"fx={selected_k[0][0]:.2f}, fy={selected_k[1][1]:.2f}"
        if selected_k
        else "—"
    )
    pose_pass_rate = shared_summary.get("physical_plausibility_pass_rate")
    sfm_intrinsics = (
        experiment_report.get("sfm_intrinsics", {})
        if isinstance(experiment_report.get("sfm_intrinsics"), dict)
        else {}
    )
    sfm_registration = (
        experiment_report.get("sfm_registration", {})
        if isinstance(experiment_report.get("sfm_registration"), dict)
        else {}
    )
    shared_intrinsics_validation = (
        shared.report.get("selection", {}).get("intrinsics_validation", {})
        if shared
        else {}
    )
    intrinsics_validation = (
        shared_intrinsics_validation
        or sfm_intrinsics.get("intrinsics_validation", {})
    )
    intrinsics_usable = bool(
        intrinsics_validation.get(
            "usable_for_sfm",
            shared_summary.get("intrinsics_usable_for_sfm", False),
        )
    )
    routing_source = sfm_intrinsics.get("routing_source")
    visually_registered = _value_count(
        sfm_registration.get("visually_registered_images")
    )
    weak_registered = _value_count(
        sfm_registration.get("weak_or_prior_only_images")
    )
    visual_metric_count = (
        visually_registered if visually_registered is not None else registered
    )
    visual_metric_is_legacy = visually_registered is None and registered is not None
    metrics = [
        WorkspaceMetric(
            "相机模型",
            selected_model or sfm_intrinsics.get("camera_model", "—"),
            "good" if selected_model or sfm_intrinsics.get("camera_model") else "neutral",
        ),
        WorkspaceMetric(
            "共享焦距",
            selected_focal,
            "good" if selected_k and intrinsics_usable else (
                "warning" if selected_k else "neutral"
            ),
        ),
        WorkspaceMetric(
            "共享鱼眼 D",
            (
                ", ".join(f"{float(value):.4g}" for value in shared.selected_D)
                if shared and shared.selected_D
                else "—"
            ),
            (
                "good"
                if shared and shared.selected_D is not None and intrinsics_usable
                else ("warning" if shared and shared.selected_D is not None else "neutral")
            ),
        ),
        WorkspaceMetric(
            "内参可信度门禁",
            str(intrinsics_validation.get("status", "—")),
            (
                "good"
                if intrinsics_validation.get("status") == "credible"
                else ("warning" if intrinsics_validation else "neutral")
            ),
            "联合检查主点、焦距、视场覆盖、鱼眼映射可逆性和拟合/BA 阈值。",
        ),
        WorkspaceMetric(
            "SfM 内参路由",
            str(routing_source or "—"),
            (
                "good"
                if routing_source == "fitted"
                else ("warning" if routing_source else "neutral")
            ),
            "provisional 路由只用于候选试算，不代表拟合 K/D 可信。",
        ),
        WorkspaceMetric(
            "拟合初始化",
            (
                "完成"
                if shared_summary.get("fitted_initialization_available")
                else "—"
            ),
            "good"
            if shared_summary.get("fitted_initialization_available")
            else "neutral",
            "由共享 K/D 与逐机位姿联合拟合形成，作为 BA 起点。",
        ),
        WorkspaceMetric(
            "联合 BA",
            (
                "完成"
                if shared_summary.get("bundle_adjustment_success")
                else (
                    "已执行"
                    if shared_summary.get("bundle_adjustment_performed")
                    else "—"
                )
            ),
            "good"
            if shared_summary.get("bundle_adjustment_success")
            else (
                "warning"
                if shared_summary.get("bundle_adjustment_performed")
                else "neutral"
            ),
            "允许复用形成拟合初始化基线的人工控制观测。",
        ),
        WorkspaceMetric(
            "尺度拟合 RMSE",
            f"{shared_scale.get('rmse_metres'):.4f} m" if shared_scale.get("rmse_metres") is not None else "—",
            "good" if shared_scale else "neutral",
        ),
        WorkspaceMetric(
            "设备三方一致",
            (
                f"{len(shared_identity.get('intersection_all_three', []))}/"
                f"{len(shared_identity.get('rows', []))}"
                if shared_identity.get("rows")
                else "—"
            ),
            (
                "good"
                if shared_identity.get("rows")
                and len(shared_identity.get("intersection_all_three", []))
                == len(shared_identity.get("rows", []))
                else ("warning" if shared_identity else "neutral")
            ),
            "scale.txt、cali.txt 与相机图片的设备 ID 交集。",
        ),
        WorkspaceMetric(
            "位姿物理检查",
            f"{pose_pass_rate:.0%}" if isinstance(pose_pass_rate, (int, float)) else "—",
            "good" if pose_pass_rate == 1 else ("warning" if pose_pass_rate is not None else "neutral"),
        ),
        WorkspaceMetric(
            "地面相容候选轨迹",
            str(
                shared_tracks.get("candidate_counts_by_spread_threshold_metres", {}).get("0.25", "—")
            ),
            "warning" if shared_tracks else "neutral",
            "没有地面语义标签，只能作为候选，不能视为独立验证。",
        ),
        WorkspaceMetric("验证匹配图像对", f"{verified_pairs}/36" if pairs else "—", _metric_state(verified_pairs if pairs else None, 36)),
        WorkspaceMetric(
            "视觉 SfM 注册",
            (
                f"{visual_metric_count}/{total_images}"
                if visual_metric_count is not None and total_images
                else "—"
            ),
            (
                "warning"
                if visual_metric_is_legacy
                else _metric_state(
                    visual_metric_count,
                    total_images if isinstance(total_images, int) else None,
                )
            ),
            (
                "旧报告没有逐相机三维观测，数量仅作兼容展示，不能视为视觉验收。"
                if visual_metric_is_legacy
                else "只统计达到最小三维点观测数的图片，不统计空壳位姿。"
            ),
        ),
        WorkspaceMetric(
            "弱观测/先验位姿",
            str(weak_registered if weak_registered is not None else "—"),
            (
                "warning"
                if weak_registered
                else ("good" if weak_registered == 0 else "neutral")
            ),
            "这些图片不会计入全相机视觉注册。",
        ),
        WorkspaceMetric("稀疏点", str(sparse_points if sparse_points is not None else "—"), _metric_state(sparse_points)),
        WorkspaceMetric("清洗后轨迹", str(clean_tracks if clean_tracks is not None else "—"), _metric_state(clean_tracks)),
        WorkspaceMetric("长度 ≥3 轨迹", str(long_tracks if long_tracks is not None else "—"), _metric_state(long_tracks)),
    ]

    if shared is None:
        stages = [
            WorkspaceStage(key, title, "未生成", detail)
            for key, title, detail in (
                ("input_scale", "① 输入与尺度", "解析 scale.txt、cali.txt、图片和设备身份"),
                ("fit_pose", "② K/D 与位姿拟合", "共享鱼眼 K/D 并联合拟合逐机位姿"),
                ("intrinsics_gate", "③ 内参可信度门禁", "拒绝越界、折返或未过阈值的拟合参数"),
                ("sfm_candidates", "④ 多候选 SfM", "比较全局、增量和拟合位姿辅助候选"),
                ("visual_acceptance", "⑤ 三维证据验收", "按逐相机三维观测而非入模位姿验收"),
                ("output", "⑥ 全相机输出", "发布通过候选并区分弱观测与未注册相机"),
            )
        ]
    else:
        homography_count = len(shared.report.get("homographies", []))
        fitted_ready = bool(shared_summary.get("fitted_initialization_available"))
        identity_mismatches = [
            *shared_identity.get("cali_and_image_not_scale", []),
            *shared_identity.get("scale_not_cali_or_image", []),
            *shared_identity.get("cali_without_image", []),
            *shared_identity.get("image_without_cali", []),
        ]
        fit_status = "complete" if fitted_ready and shared.poses else "blocked"
        gate_status = (
            "complete"
            if intrinsics_validation.get("status") == "credible"
            else ("warning" if intrinsics_validation else "blocked")
        )
        candidate_count = int(sfm_registration.get("candidate_count", 0) or 0)
        sfm_status = str(sfm_registration.get("status", ""))
        candidate_status = (
            "complete"
            if candidate_count and sfm_registration.get("selected_candidate")
            else ("failed" if sfm_status == "failed" else "blocked")
        )
        visual_status = (
            "complete"
            if sfm_registration.get("full_visual_registration")
            else (
                "warning"
                if sfm_status == "partial_visual"
                else ("failed" if sfm_status == "failed" else "blocked")
            )
        )
        stages = [
            WorkspaceStage(
                "input_scale",
                "① 输入与尺度",
                "warning" if identity_mismatches else "complete",
                (
                    "已解析尺度；设备身份存在差异：" + "、".join(map(str, identity_mismatches))
                    if identity_mismatches
                    else "已解析 scale.txt、cali.txt、图片与设备身份"
                ),
            ),
            WorkspaceStage(
                "fit_pose",
                "② K/D 与位姿拟合",
                fit_status,
                f"纳入 {homography_count} 台相机；共享 K/D 并联合形成逐机位姿",
            ),
            WorkspaceStage(
                "intrinsics_gate",
                "③ 内参可信度门禁",
                gate_status,
                "；".join(
                    map(str, intrinsics_validation.get("hard_failures", []))
                )
                or f"状态：{intrinsics_validation.get('status', '未生成')}",
            ),
            WorkspaceStage(
                "sfm_candidates",
                "④ 多候选 SfM",
                candidate_status,
                (
                    f"已比较 {candidate_count} 个候选，选择 "
                    f"{sfm_registration.get('selected_candidate')}"
                    if candidate_count
                    else "等待运行 Sfm"
                ),
            ),
            WorkspaceStage(
                "visual_acceptance",
                "⑤ 三维证据验收",
                visual_status,
                (
                    f"视觉支撑 {visually_registered or 0}/{total_images or 0}；"
                    f"弱观测/先验 {weak_registered or 0}"
                    if sfm_registration
                    else "等待逐相机三维观测验收"
                ),
            ),
            WorkspaceStage(
                "output",
                "⑥ 全相机输出",
                "ready" if sfm_registration.get("full_visual_registration") else (
                    "warning" if sfm_registration.get("selected_candidate") else "blocked"
                ),
                (
                    "全相机均有真实三维观测，已发布最终模型"
                    if sfm_registration.get("full_visual_registration")
                    else "未达到全相机视觉注册，保留候选与失败原因"
                ),
            ),
        ]

    findings: list[str] = []
    for key in ("warnings", "diagnostics"):
        values = experiment_report.get(key, [])
        if isinstance(values, list):
            findings.extend(str(value) for value in values)
    global_findings = geometry.get("global_findings", [])
    if isinstance(global_findings, list):
        findings.extend(str(value) for value in global_findings)
    if shared:
        if shared.report.get("conclusion"):
            findings.insert(0, str(shared.report["conclusion"]))
        selection_reason = shared.report.get("selection", {}).get("reason")
        if selection_reason:
            findings.append(str(selection_reason))
        findings.append(str(shared.report.get("selection", {}).get("distortion_observability", "")))
        if shared_summary.get("post_pose_suspect_camera_ids"):
            findings.append(
                "位姿疑似异常相机："
                + "、".join(map(str, shared_summary["post_pose_suspect_camera_ids"]))
            )

    return CalibrationWorkspaceSnapshot(
        dataset_path=dataset,
        experiment_path=experiment,
        report_roots=roots,
        shared_result_path=shared_path,
        shared=shared,
        sfm_intrinsics=sfm_intrinsics,
        sfm_registration=sfm_registration,
        metrics=metrics,
        stages=stages,
        findings=list(dict.fromkeys(findings)),
        cameras=_camera_rows(inventory),
        pairs=_find_pairs(roots, warnings),
        artifacts=_find_artifacts(experiment, roots),
        warnings=warnings,
    )
