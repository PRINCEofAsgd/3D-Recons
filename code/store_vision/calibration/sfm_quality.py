"""依据真实三维观测验收、比较并选择 COLMAP 稀疏模型候选。"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from store_vision.calibration.colmap_parser import parse_text_model


@dataclass(frozen=True)
class SfmCandidateQuality:
    """单个 SfM 候选的可审计质量摘要。"""

    candidate: str
    binary_path: str
    text_path: str
    command_succeeded: bool
    status: str
    accepted: bool
    full_visual_registration: bool
    registered_images: list[str]
    visually_supported_images: list[str]
    weak_or_prior_only_images: list[str]
    unregistered_images: list[str]
    per_image_point3d_observations: dict[str, int]
    sparse_points: int
    total_point3d_observations: int
    mean_track_length: float | None
    mean_reprojection_error_px: float | None
    rejection_reasons: list[str]
    score: tuple[float, ...]


def evaluate_sfm_candidate(
    candidate: str,
    binary_path: str | Path,
    text_path: str | Path,
    all_image_names: Iterable[str],
    *,
    command_succeeded: bool = True,
    minimum_image_observations: int = 3,
    maximum_reprojection_error_px: float = 4.0,
) -> SfmCandidateQuality:
    """验收候选；“入模”必须同时具有关联到 3D 点的二维观测。"""

    binary = Path(binary_path)
    text = Path(text_path)
    all_names = sorted(set(all_image_names))
    reasons: list[str] = []
    if not command_succeeded:
        reasons.append("候选命令执行失败")
    try:
        reconstruction = parse_text_model(text)
    except (FileNotFoundError, OSError, ValueError) as exc:
        reasons.append(f"模型不可解析：{exc}")
        return SfmCandidateQuality(
            candidate=candidate,
            binary_path=str(binary),
            text_path=str(text),
            command_succeeded=command_succeeded,
            status="unparseable",
            accepted=False,
            full_visual_registration=False,
            registered_images=[],
            visually_supported_images=[],
            weak_or_prior_only_images=[],
            unregistered_images=all_names,
            per_image_point3d_observations={},
            sparse_points=0,
            total_point3d_observations=0,
            mean_track_length=None,
            mean_reprojection_error_px=None,
            rejection_reasons=reasons,
            score=(0.0,) * 7,
        )

    per_image = {
        record.name: int(record.metadata.get("point3d_observation_count", 0))
        for record in reconstruction.images.values()
    }
    registered = sorted(per_image)
    supported = sorted(
        name
        for name, count in per_image.items()
        if count >= minimum_image_observations
    )
    weak = sorted(set(registered) - set(supported))
    unregistered = sorted(set(all_names) - set(registered))
    total_observations = sum(per_image.values())
    mean_track = (
        total_observations / reconstruction.points3d
        if reconstruction.points3d
        else None
    )
    minimum_points = max(8, len(all_names))
    minimum_total_observations = max(24, len(all_names) * minimum_image_observations)
    enough_global_support = (
        reconstruction.points3d >= minimum_points
        and total_observations >= minimum_total_observations
        and mean_track is not None
        and mean_track >= 2.0
    )
    error_ok = (
        reconstruction.mean_reprojection_error is None
        or (
            math.isfinite(reconstruction.mean_reprojection_error)
            and reconstruction.mean_reprojection_error <= maximum_reprojection_error_px
        )
    )
    full_visual = (
        len(supported) == len(all_names)
        and not unregistered
        and enough_global_support
        and error_ok
    )
    partial_visual = (
        len(supported) >= 2
        and reconstruction.points3d >= 8
        and total_observations >= 24
        and error_ok
    )
    if reconstruction.points3d == 0:
        reasons.append("模型没有任何三维点")
    if registered and not supported:
        reasons.append("图像虽写入模型，但没有达到最小三维观测支持")
    if not enough_global_support:
        reasons.append(
            f"三维支撑不足：{reconstruction.points3d} 点、"
            f"{total_observations} 次观测"
        )
    if not error_ok:
        reasons.append("平均重投影误差超过验收上限")
    if weak:
        reasons.append(f"{len(weak)} 张已入模图像缺少足够三维观测")
    if unregistered:
        reasons.append(f"{len(unregistered)} 张输入图像未入模")

    if full_visual:
        status = "all_camera_visual"
    elif partial_visual:
        status = "partial_visual"
    elif registered:
        status = "registered_without_geometry"
    else:
        status = "failed"
    accepted = full_visual or partial_visual
    score = (
        2.0 if full_visual else (1.0 if partial_visual else 0.0),
        float(len(supported)),
        float(len(registered)),
        float(reconstruction.points3d),
        float(total_observations),
        float(mean_track or 0.0),
        -float(reconstruction.mean_reprojection_error or 1.0e6),
    )
    return SfmCandidateQuality(
        candidate=candidate,
        binary_path=str(binary),
        text_path=str(text),
        command_succeeded=command_succeeded,
        status=status,
        accepted=accepted,
        full_visual_registration=full_visual,
        registered_images=registered,
        visually_supported_images=supported,
        weak_or_prior_only_images=weak,
        unregistered_images=unregistered,
        per_image_point3d_observations=per_image,
        sparse_points=reconstruction.points3d,
        total_point3d_observations=total_observations,
        mean_track_length=mean_track,
        mean_reprojection_error_px=reconstruction.mean_reprojection_error,
        rejection_reasons=reasons,
        score=score,
    )


def select_best_sfm_candidate(
    candidates: Iterable[SfmCandidateQuality],
) -> SfmCandidateQuality | None:
    """只从具有真实视觉几何的候选中选择最高分模型。"""

    accepted = [candidate for candidate in candidates if candidate.accepted]
    return max(accepted, key=lambda candidate: candidate.score) if accepted else None


def publish_selected_candidate(
    candidate: SfmCandidateQuality,
    sparse_output: str | Path,
    text_output: str | Path,
) -> None:
    """把已验收候选发布到稳定输出位置，保留原候选目录供追溯。"""

    binary_destination = Path(sparse_output) / "0"
    text_destination = Path(text_output)
    binary_destination.parent.mkdir(parents=True, exist_ok=True)
    text_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(candidate.binary_path, binary_destination, dirs_exist_ok=True)
    shutil.copytree(candidate.text_path, text_destination, dirs_exist_ok=True)


def write_candidate_comparison(
    candidates: Iterable[SfmCandidateQuality],
    selected: SfmCandidateQuality | None,
    path: str | Path,
) -> Path:
    """输出完整候选矩阵，使 UI 不再把命令成功误报为 SfM 成功。"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    items = list(candidates)
    payload = {
        "schema_version": 1,
        "status": (
            "all_camera_visual"
            if selected is not None and selected.full_visual_registration
            else ("partial_visual" if selected is not None else "failed")
        ),
        "selected_candidate": selected.candidate if selected is not None else None,
        "full_visual_registration": bool(
            selected is not None and selected.full_visual_registration
        ),
        "acceptance_rule": (
            "每张相机至少 3 个已关联三维点观测，且全局点数、观测数、"
            "平均轨迹长度和重投影误差同时通过门禁。"
        ),
        "candidates": [asdict(candidate) for candidate in items],
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output
