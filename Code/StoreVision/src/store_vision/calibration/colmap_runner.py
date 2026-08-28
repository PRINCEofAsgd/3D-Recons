"""Safe subprocess wrapper for the sparse COLMAP stages used by the demo."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class ColmapNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    dry_run: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    log_path: Path | None = None


def detect_colmap() -> Path | None:
    executable = shutil.which("colmap")
    return Path(executable) if executable else None


def _command(executable: str | Path | None, dry_run: bool, args: Sequence[str]) -> list[str]:
    if executable is not None:
        binary = str(executable)
    else:
        detected = detect_colmap()
        if detected is None and not dry_run:
            raise ColmapNotFoundError(
                "COLMAP executable was not found. Install COLMAP and ensure `colmap` is on PATH, "
                "or use --dry-run/--skip-colmap."
            )
        binary = str(detected or "colmap")
    return [binary, *map(str, args)]


def _write_command_log(result: CommandResult) -> None:
    """将命令、标准输出、标准错误和退出码完整保存到单阶段日志。"""

    if result.log_path is None:
        return
    result.log_path.parent.mkdir(parents=True, exist_ok=True)
    result.log_path.write_text(
        "command: " + " ".join(result.command) + "\n"
        + f"returncode: {result.returncode}\n"
        + "\n[stdout]\n"
        + result.stdout
        + "\n[stderr]\n"
        + result.stderr,
        encoding="utf-8",
    )


def _run(command: list[str], dry_run: bool, log_path: str | Path | None = None) -> CommandResult:
    print("COLMAP plan:", " ".join(command))
    if dry_run:
        return CommandResult(tuple(command), dry_run=True)
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        result = CommandResult(
            tuple(command),
            dry_run=False,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            log_path=Path(log_path) if log_path is not None else None,
        )
    except OSError as exc:
        # 启动失败也返回结构化结果，让 workflow 能继续生成诊断报告。
        result = CommandResult(
            tuple(command),
            dry_run=False,
            returncode=None,
            stderr=str(exc),
            log_path=Path(log_path) if log_path is not None else None,
        )
    _write_command_log(result)
    return result


def query_version(
    *,
    executable: str | Path | None = None,
    log_path: str | Path | None = None,
) -> CommandResult:
    """执行 COLMAP 自带的 version 子命令并保留环境证据。"""

    return _run(_command(executable, False, ["version"]), False, log_path)


def extract_features(
    database_path: str | Path,
    image_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    image_list_path: str | Path | None = None,
    camera_model: str = "SIMPLE_RADIAL",
    camera_params: Sequence[float] | None = None,
    single_camera: bool = True,
    use_gpu: bool = False,
    log_path: str | Path | None = None,
) -> CommandResult:
    args: list[str | Path] = [
        "feature_extractor",
        "--database_path",
        database_path,
        "--image_path",
        image_path,
        "--default_random_seed",
        "0",
        "--ImageReader.camera_model",
        camera_model,
        "--ImageReader.single_camera",
        "1" if single_camera else "0",
        "--FeatureExtraction.use_gpu",
        "1" if use_gpu else "0",
        "--FeatureExtraction.num_threads",
        "1",
    ]
    if image_list_path is not None:
        args.extend(["--image_list_path", image_list_path])
    if camera_params is not None:
        # COLMAP 接受逗号分隔的模型参数；保持 argv 传参，不通过 shell 拼接。
        args.extend(
            [
                "--ImageReader.camera_params",
                ",".join(f"{float(value):.17g}" for value in camera_params),
            ]
        )
    command = _command(
        executable,
        dry_run,
        args,
    )
    return _run(command, dry_run, log_path)


def match_features(
    database_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    use_gpu: bool = False,
    log_path: str | Path | None = None,
) -> CommandResult:
    args: list[str | Path] = [
        "exhaustive_matcher",
        "--database_path",
        database_path,
        "--default_random_seed",
        "0",
        "--FeatureMatching.use_gpu",
        "1" if use_gpu else "0",
        "--FeatureMatching.num_threads",
        "1",
    ]
    command = _command(
        executable,
        dry_run,
        args,
    )
    return _run(command, dry_run, log_path)


def run_mapper(
    database_path: str | Path,
    image_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    freeze_intrinsics: bool = False,
    max_extra_param: float | None = None,
    input_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> CommandResult:
    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    args: list[str | Path] = [
            "mapper",
            "--database_path",
            database_path,
            "--image_path",
            image_path,
            "--output_path",
            output_path,
            "--default_random_seed",
            "0",
            "--Mapper.random_seed",
            "0",
            "--Mapper.num_threads",
            "1",
        ]
    if input_path is not None:
        args.extend(["--input_path", input_path])
    if freeze_intrinsics:
        # Sfm 使用拟合 K/D 作为固定几何基线，防止二视图阶段把高阶鱼眼参数拟合到极端值。
        args.extend(
            [
                "--Mapper.ba_refine_focal_length",
                "0",
                "--Mapper.ba_refine_principal_point",
                "0",
                "--Mapper.ba_refine_extra_params",
                "0",
            ]
        )
    if max_extra_param is not None:
        args.extend(["--Mapper.max_extra_param", f"{float(max_extra_param):.17g}"])
    command = _command(executable, dry_run, args)
    return _run(command, dry_run, log_path)


def run_global_mapper(
    database_path: str | Path,
    image_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    freeze_intrinsics: bool = True,
    log_path: str | Path | None = None,
) -> CommandResult:
    """运行全局 SfM 候选；该分支不依赖增量 Mapper 的初始二视图选择。"""

    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    args: list[str | Path] = [
        "global_mapper",
        "--database_path",
        database_path,
        "--image_path",
        image_path,
        "--output_path",
        output_path,
        "--default_random_seed",
        "0",
        # 明确关闭图优化和 BA 的 GPU 路径，保证 macOS/CI 的确定性。
        "--GlobalMapper.gp_use_gpu",
        "0",
        "--GlobalMapper.ba_ceres_use_gpu",
        "0",
        # 监控画面重叠常只形成二视图轨迹；保留它们才能给边缘相机建立
        # 可核验的三维观测，最终仍由候选质量门禁过滤弱模型。
        "--GlobalMapper.track_min_num_views_per_track",
        "2",
        "--GlobalMapper.ba_min_track_length",
        "2",
    ]
    if freeze_intrinsics:
        args.extend(
            [
                "--GlobalMapper.ba_refine_focal_length",
                "0",
                "--GlobalMapper.ba_refine_principal_point",
                "0",
                "--GlobalMapper.ba_refine_extra_params",
                "0",
                "--GlobalMapper.refine_sensor_from_rig",
                "0",
            ]
        )
    return _run(_command(executable, dry_run, args), dry_run, log_path)


def run_image_registrator(
    database_path: str | Path,
    input_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    log_path: str | Path | None = None,
) -> CommandResult:
    """把尚未入模图片注册到已有几何模型，内参保持数据库中的共享值。"""

    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    args: list[str | Path] = [
        "image_registrator",
        "--database_path",
        database_path,
        "--input_path",
        input_path,
        "--output_path",
        output_path,
        "--Mapper.ba_refine_focal_length",
        "0",
        "--Mapper.ba_refine_principal_point",
        "0",
        "--Mapper.ba_refine_extra_params",
        "0",
    ]
    return _run(_command(executable, dry_run, args), dry_run, log_path)


def run_point_triangulator(
    database_path: str | Path,
    image_path: str | Path,
    input_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    clear_points: bool = True,
    log_path: str | Path | None = None,
) -> CommandResult:
    """在给定位姿种子上重新三角化，生成可验证的视觉三维证据。"""

    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    args: list[str | Path] = [
        "point_triangulator",
        "--database_path",
        database_path,
        "--image_path",
        image_path,
        "--input_path",
        input_path,
        "--output_path",
        output_path,
        "--clear_points",
        "1" if clear_points else "0",
        "--refine_intrinsics",
        "0",
    ]
    return _run(_command(executable, dry_run, args), dry_run, log_path)


def run_bundle_adjuster(
    input_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    log_path: str | Path | None = None,
) -> CommandResult:
    """对已具有真实三维点的候选做位姿 BA，并冻结共享 K/D。"""

    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    args: list[str | Path] = [
        "bundle_adjuster",
        "--input_path",
        input_path,
        "--output_path",
        output_path,
        "--BundleAdjustment.refine_focal_length",
        "0",
        "--BundleAdjustment.refine_principal_point",
        "0",
        "--BundleAdjustment.refine_extra_params",
        "0",
    ]
    return _run(_command(executable, dry_run, args), dry_run, log_path)


def convert_model_to_text(
    input_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    executable: str | Path | None = None,
    log_path: str | Path | None = None,
) -> CommandResult:
    if not dry_run:
        Path(output_path).mkdir(parents=True, exist_ok=True)
    command = _command(
        executable,
        dry_run,
        [
            "model_converter",
            "--input_path",
            input_path,
            "--output_path",
            output_path,
            "--output_type",
            "TXT",
        ],
    )
    return _run(command, dry_run, log_path)
