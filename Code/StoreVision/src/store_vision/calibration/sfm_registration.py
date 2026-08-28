"""多候选 SfM 注册、位姿辅助恢复、质量验收与最终发布。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from store_vision.calibration.colmap_runner import (
    CommandResult,
    convert_model_to_text,
    run_bundle_adjuster,
    run_global_mapper,
    run_image_registrator,
    run_mapper,
    run_point_triangulator,
)
from store_vision.calibration.pose_seed import write_pose_seed_model
from store_vision.calibration.sfm_quality import (
    SfmCandidateQuality,
    evaluate_sfm_candidate,
    publish_selected_candidate,
    select_best_sfm_candidate,
    write_candidate_comparison,
)


@dataclass
class SfmRegistrationResult:
    """多候选阶段的执行结果。"""

    command_results: list[CommandResult] = field(default_factory=list)
    candidates: list[SfmCandidateQuality] = field(default_factory=list)
    selected: SfmCandidateQuality | None = None
    warnings: list[str] = field(default_factory=list)
    pose_seed: dict[str, object] = field(default_factory=dict)


def _command_succeeded(result: CommandResult) -> bool:
    return result.dry_run or result.returncode == 0


def _binary_models(root: Path) -> list[Path]:
    """兼容 global_mapper 直接输出和 incremental mapper 的编号目录。"""

    if (root / "cameras.bin").is_file() and (root / "images.bin").is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and (path / "cameras.bin").is_file()
        and (path / "images.bin").is_file()
    )


def _clear_previous_generated_models(output: Path) -> None:
    """清除同一输出目录中的旧候选，防止失败重跑误选上轮模型。"""

    generated_paths = (
        output / "sparse_candidates",
        output / "model_candidates",
        output / "pose_seed_model",
        output / "sparse",
        output / "model_txt",
        output / "reports" / "sfm_registration.json",
    )
    for path in generated_paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _convert_and_evaluate(
    execution: SfmRegistrationResult,
    *,
    label: str,
    root: Path,
    model_text_root: Path,
    image_names: Iterable[str],
    executable: str | Path | None,
    command_succeeded: bool,
    logs_dir: Path,
) -> None:
    models = _binary_models(root)
    if not models:
        execution.candidates.append(
            evaluate_sfm_candidate(
                label,
                root,
                model_text_root / label,
                image_names,
                command_succeeded=command_succeeded,
            )
        )
        return
    for index, model in enumerate(models):
        suffix = "" if len(models) == 1 else f"_{model.name}"
        candidate_label = f"{label}{suffix}"
        text_path = model_text_root / candidate_label
        converted = convert_model_to_text(
            model,
            text_path,
            executable=executable,
            log_path=logs_dir / f"model_converter_{candidate_label}.log",
        )
        execution.command_results.append(converted)
        execution.candidates.append(
            evaluate_sfm_candidate(
                candidate_label,
                model,
                text_path,
                image_names,
                command_succeeded=command_succeeded
                and _command_succeeded(converted),
            )
        )


def run_multi_candidate_sfm(
    database_path: str | Path,
    image_path: str | Path,
    output_path: str | Path,
    image_names: Iterable[str],
    calibration_report_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
) -> SfmRegistrationResult:
    """运行全局/增量/拟合位姿候选，并只发布通过三维证据门禁的模型。"""

    database = Path(database_path)
    images = Path(image_path)
    output = Path(output_path)
    candidates_root = output / "sparse_candidates"
    texts_root = output / "model_candidates"
    logs_dir = output / "logs"
    result = SfmRegistrationResult()
    names = list(image_names)
    if not dry_run:
        # 候选只能来自当前数据库和当前匹配结果，禁止复用同目录旧模型。
        _clear_previous_generated_models(output)

    global_root = candidates_root / "global"
    global_result = run_global_mapper(
        database,
        images,
        global_root,
        dry_run=dry_run,
        executable=executable,
        log_path=logs_dir / "03_global_mapper.log",
    )
    result.command_results.append(global_result)

    incremental_root = candidates_root / "incremental"
    incremental_result = run_mapper(
        database,
        images,
        incremental_root,
        dry_run=dry_run,
        executable=executable,
        freeze_intrinsics=True,
        log_path=logs_dir / "04_incremental_mapper.log",
    )
    result.command_results.append(incremental_result)
    if dry_run:
        # dry-run 只输出完整命令计划，不伪造候选模型或验收结果。
        return result

    _convert_and_evaluate(
        result,
        label="global",
        root=global_root,
        model_text_root=texts_root,
        image_names=names,
        executable=executable,
        command_succeeded=_command_succeeded(global_result),
        logs_dir=logs_dir,
    )
    _convert_and_evaluate(
        result,
        label="incremental",
        root=incremental_root,
        model_text_root=texts_root,
        image_names=names,
        executable=executable,
        command_succeeded=_command_succeeded(incremental_result),
        logs_dir=logs_dir,
    )

    temporary = select_best_sfm_candidate(result.candidates)
    if temporary is not None and not temporary.full_visual_registration:
        expanded_root = candidates_root / "registered_expansion"
        expanded = run_image_registrator(
            database,
            temporary.binary_path,
            expanded_root,
            executable=executable,
            log_path=logs_dir / "05_image_registrator.log",
        )
        result.command_results.append(expanded)
        _convert_and_evaluate(
            result,
            label="registered_expansion",
            root=expanded_root,
            model_text_root=texts_root,
            image_names=names,
            executable=executable,
            command_succeeded=_command_succeeded(expanded),
            logs_dir=logs_dir,
        )

    if not any(candidate.full_visual_registration for candidate in result.candidates):
        seed_text = output / "pose_seed_model"
        try:
            result.pose_seed = write_pose_seed_model(
                database,
                calibration_report_path,
                seed_text,
            )
            triangulated_root = candidates_root / "pose_seed_triangulated"
            triangulated = run_point_triangulator(
                database,
                images,
                seed_text,
                triangulated_root,
                executable=executable,
                log_path=logs_dir / "06_pose_seed_triangulator.log",
            )
            result.command_results.append(triangulated)
            _convert_and_evaluate(
                result,
                label="pose_seed_triangulated",
                root=triangulated_root,
                model_text_root=texts_root,
                image_names=names,
                executable=executable,
                command_succeeded=_command_succeeded(triangulated),
                logs_dir=logs_dir,
            )
            seed_candidate = next(
                (
                    candidate
                    for candidate in reversed(result.candidates)
                    if candidate.candidate.startswith("pose_seed_triangulated")
                    and candidate.accepted
                ),
                None,
            )
            if seed_candidate is not None:
                optimized_root = candidates_root / "pose_seed_ba"
                optimized = run_bundle_adjuster(
                    seed_candidate.binary_path,
                    optimized_root,
                    executable=executable,
                    log_path=logs_dir / "07_pose_seed_bundle_adjuster.log",
                )
                result.command_results.append(optimized)
                _convert_and_evaluate(
                    result,
                    label="pose_seed_ba",
                    root=optimized_root,
                    model_text_root=texts_root,
                    image_names=names,
                    executable=executable,
                    command_succeeded=_command_succeeded(optimized),
                    logs_dir=logs_dir,
                )
        except (OSError, ValueError) as exc:
            result.pose_seed = {"status": "unavailable", "reason": str(exc)}
            result.warnings.append(f"拟合位姿种子候选不可用：{exc}")
    else:
        result.pose_seed = {
            "status": "not_needed",
            "reason": "视觉 SfM 已通过全相机三维观测门禁",
        }

    result.selected = select_best_sfm_candidate(result.candidates)
    if result.selected is not None:
        publish_selected_candidate(
            result.selected,
            output / "sparse",
            output / "model_txt",
        )
        if not result.selected.full_visual_registration:
            result.warnings.append(
                "最高分候选仅达到部分视觉注册："
                + "；".join(result.selected.rejection_reasons)
            )
    else:
        result.warnings.append("没有 SfM 候选通过真实三维观测门禁")
    write_candidate_comparison(
        result.candidates,
        result.selected,
        output / "reports" / "sfm_registration.json",
    )
    return result
