"""把鱼眼联合标定结果安全接入 COLMAP 相机数据库。"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from store_vision.calibration.intrinsics_validation import (
    provisional_fisheye_intrinsics,
    validate_fisheye_intrinsics,
)


# COLMAP 官方相机模型枚举中的 OPENCV_FISHEYE 固定编号。
OPENCV_FISHEYE_MODEL_ID = 5


@dataclass(frozen=True)
class FittedFisheyeCalibration:
    """经可信度路由后、可按输出尺寸换算的共享鱼眼 K/D。

    ``K/D`` 是实际写入 COLMAP 的参数；``fitted_K/fitted_D`` 始终保留
    原始拟合结果，便于报告区分“正式拟合”和“物理可逆试算”。
    """

    report_path: Path
    dataset_path: Path
    canonical_resolution: tuple[int, int]
    K: tuple[tuple[float, float, float], ...]
    D: tuple[float, float, float, float]
    fitted_K: tuple[tuple[float, float, float], ...]
    fitted_D: tuple[float, float, float, float]
    initial_K: tuple[tuple[float, float, float], ...]
    fit_accepted: bool
    fit_rmse_px: float | None
    ba_accepted: bool
    ba_rmse_px: float | None
    fitted_validation: dict[str, Any]
    active_validation: dict[str, Any]
    routing_source: str
    distortion_scale: float

    def parameters_for_resolution(
        self,
        resolution: tuple[int, int],
        *,
        source: str = "active",
    ) -> tuple[float, ...]:
        """按线性关系换算 K；可明确选择活动参数或原始拟合参数。"""

        width, height = resolution
        canonical_width, canonical_height = self.canonical_resolution
        scale_x = width / canonical_width
        scale_y = height / canonical_height
        if source == "active":
            K, D = self.K, self.D
        elif source == "fitted":
            K, D = self.fitted_K, self.fitted_D
        else:
            raise ValueError(f"未知鱼眼参数来源：{source}")
        return (
            K[0][0] * scale_x,
            K[1][1] * scale_y,
            K[0][2] * scale_x,
            K[1][2] * scale_y,
            *D,
        )

    def report_payload(self) -> dict[str, Any]:
        """生成写入 Sfm 总报告的可审计参数摘要。"""

        return {
            "status": (
                "applied_fitted_fisheye"
                if self.routing_source == "fitted"
                else "applied_provisional_fisheye"
            ),
            "source": str(self.report_path),
            "dataset_path": str(self.dataset_path),
            "camera_model": "OPENCV_FISHEYE",
            "canonical_resolution": list(self.canonical_resolution),
            "shared_K": [list(row) for row in self.K],
            "shared_D": list(self.D),
            "fitted_K": [list(row) for row in self.fitted_K],
            "fitted_D": list(self.fitted_D),
            "initial_K": [list(row) for row in self.initial_K],
            "routing_source": self.routing_source,
            "distortion_scale": self.distortion_scale,
            "intrinsics_validation": self.fitted_validation,
            "active_intrinsics_validation": self.active_validation,
            "credible_calibration": (
                self.routing_source == "fitted"
                and bool(self.fitted_validation.get("usable_for_sfm"))
            ),
            "fit_accepted_by_reprojection_threshold": self.fit_accepted,
            "fit_reprojection_rmse_px": self.fit_rmse_px,
            "ba_accepted_by_reprojection_threshold": self.ba_accepted,
            "ba_reprojection_rmse_px": self.ba_rmse_px,
            "execute_now": True,
            "intrinsics_refinement": "frozen_during_all_sfm_candidates",
            "grouping_policy": (
                "同分辨率图片共享一个 COLMAP camera；异分辨率按宽高比例换算 K，"
                "所有分辨率保持同一无量纲 D。"
            ),
            "routing_policy": (
                "可信拟合直接进入 Sfm；物理门禁拒绝时仅使用初始 K 与缩放后非零 D "
                "建立试算候选，最终必须由真实三维观测验收。"
            ),
        }


def _read_report(path: Path) -> tuple[Path, dict[str, Any]]:
    """读取显式文件或标定运行目录中的主 JSON 报告。"""

    candidates = (
        (path,)
        if path.is_file()
        else (
            path / "fisheye_calibration_report.json",
            path / "shared_calibration_report.json",
        )
    )
    report_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if report_path is None:
        raise FileNotFoundError(f"鱼眼标定目录缺少主报告：{path}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"鱼眼标定报告不是 JSON 对象：{report_path}")
    return report_path.resolve(), payload


def _metric(payload: dict[str, Any], stage: str, key: str) -> Any:
    """兼容读取顶层非线性摘要和模型内嵌摘要中的拟合/BA指标。"""

    nonlinear = payload.get("nonlinear_optimization", {})
    if stage == "fit":
        primary = nonlinear.get("initialization", {}).get("fit", {})
        fallback = (
            payload.get("intrinsics_models", {})
            .get("FISHEYE", {})
            .get("initialization", {})
            .get("fit", {})
        )
    else:
        primary = nonlinear.get("bundle_adjustment", {})
        fallback = (
            payload.get("intrinsics_models", {})
            .get("FISHEYE", {})
            .get("bundle_adjustment", {})
        )
    return primary.get(key, fallback.get(key))


def _stage_summary(payload: dict[str, Any], stage: str) -> dict[str, Any]:
    """兼容取得完整拟合阶段摘要，供内参可信度门禁判断。"""

    nonlinear = payload.get("nonlinear_optimization", {})
    if stage == "fit":
        primary = nonlinear.get("initialization", {}).get("fit", {})
        fallback = (
            payload.get("intrinsics_models", {})
            .get("FISHEYE", {})
            .get("initialization", {})
            .get("fit", {})
        )
    else:
        primary = nonlinear.get("bundle_adjustment", {})
        fallback = (
            payload.get("intrinsics_models", {})
            .get("FISHEYE", {})
            .get("bundle_adjustment", {})
        )
    return dict(primary or fallback or {})


def _matrix_or_fallback(value: Any, fallback: list[list[float]]) -> list[list[float]]:
    """读取报告矩阵；旧报告缺少初始 K 时回退到拟合 K。"""

    if (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in value)
    ):
        return value
    return fallback


def load_fitted_fisheye_calibration(
    path: str | Path,
    *,
    expected_dataset: str | Path,
) -> FittedFisheyeCalibration:
    """校验报告身份、模型和数值后，返回可供 Sfm 使用的拟合 K/D。"""

    report_path, payload = _read_report(Path(path).expanduser().resolve())
    dataset_value = payload.get("input", {}).get("dataset_path")
    if not dataset_value:
        raise ValueError(f"鱼眼标定报告缺少 input.dataset_path：{report_path}")
    dataset_path = Path(dataset_value).expanduser().resolve()
    expected = Path(expected_dataset).expanduser().resolve()
    if dataset_path != expected:
        raise ValueError(
            f"鱼眼标定报告属于其他数据集：{dataset_path}；当前数据集：{expected}"
        )

    selection = payload.get("selection", {})
    if selection.get("selected_model") != "FISHEYE":
        raise ValueError(f"鱼眼标定报告未选择 FISHEYE 模型：{report_path}")
    K = selection.get("selected_K")
    D = selection.get("selected_D") or selection.get("distortion_D")
    groups = payload.get("intrinsics_groups") or []
    canonical = groups[0].get("canonical_resolution") if groups else None
    canonical = canonical or payload.get("input", {}).get("image_resolution")
    if (
        not isinstance(K, list)
        or len(K) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in K)
        or not isinstance(D, list)
        or len(D) != 4
        or not isinstance(canonical, list)
        or len(canonical) != 2
    ):
        raise ValueError(f"鱼眼标定报告中的 K/D/参考分辨率结构不完整：{report_path}")

    initialization = payload.get("nonlinear_optimization", {}).get("initialization", {})
    initial_K = _matrix_or_fallback(initialization.get("initial_K"), K)
    values = (
        [float(value) for row in K for value in row]
        + [float(value) for value in D]
        + [float(value) for row in initial_K for value in row]
    )
    width, height = int(canonical[0]), int(canonical[1])
    if not all(math.isfinite(value) for value in values) or width <= 0 or height <= 0:
        raise ValueError(f"鱼眼标定报告包含非有限参数或非法参考分辨率：{report_path}")
    if float(K[0][0]) <= 0 or float(K[1][1]) <= 0:
        raise ValueError(f"鱼眼标定报告焦距必须为正数：{report_path}")

    fitted_matrix = np.asarray(K, dtype=np.float64)
    fitted_distortion = np.asarray(D, dtype=np.float64)
    initial_matrix = np.asarray(initial_K, dtype=np.float64)
    fit_summary = _stage_summary(payload, "fit")
    ba_summary = _stage_summary(payload, "ba")
    fitted_validation = validate_fisheye_intrinsics(
        fitted_matrix,
        fitted_distortion,
        (width, height),
        fit_summary=fit_summary,
        ba_summary=ba_summary,
    )
    routing_source = "fitted"
    distortion_scale = 1.0
    active_matrix = fitted_matrix
    active_distortion = fitted_distortion
    active_validation = fitted_validation
    if not fitted_validation["usable_for_sfm"]:
        (
            active_matrix,
            active_distortion,
            distortion_scale,
            active_validation,
        ) = provisional_fisheye_intrinsics(
            initial_matrix,
            fitted_distortion,
            (width, height),
        )
        routing_source = "provisional_initial_k_scaled_nonzero_d"
    if not active_validation["usable_for_sfm"]:
        raise ValueError(
            "鱼眼拟合 K/D 被物理门禁拒绝，且无法构造可逆的非零 D 试算参数："
            f"{report_path}"
        )

    return FittedFisheyeCalibration(
        report_path=report_path,
        dataset_path=dataset_path,
        canonical_resolution=(width, height),
        K=tuple(tuple(float(value) for value in row) for row in active_matrix),
        D=tuple(float(value) for value in active_distortion),
        fitted_K=tuple(tuple(float(value) for value in row) for row in fitted_matrix),
        fitted_D=tuple(float(value) for value in fitted_distortion),
        initial_K=tuple(tuple(float(value) for value in row) for row in initial_matrix),
        fit_accepted=bool(_metric(payload, "fit", "accepted_by_reprojection_threshold")),
        fit_rmse_px=_optional_float(_metric(payload, "fit", "reprojection_rmse_px")),
        ba_accepted=bool(_metric(payload, "ba", "accepted_by_reprojection_threshold")),
        ba_rmse_px=_optional_float(_metric(payload, "ba", "reprojection_rmse_px")),
        fitted_validation=fitted_validation,
        active_validation=active_validation,
        routing_source=routing_source,
        distortion_scale=distortion_scale,
    )


def _optional_float(value: Any) -> float | None:
    """把报告中的可选指标转为有限浮点数。"""

    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def discover_fitted_fisheye_calibration(
    dataset: str | Path,
    sfm_output: str | Path,
) -> Path | None:
    """从本轮输出根和标准数据集输出目录发现最新同门店标定结果。"""

    dataset_path = Path(dataset).expanduser().resolve()
    output_path = Path(sfm_output).expanduser().resolve()
    roots: list[Path] = []
    for parent in output_path.parents:
        candidate = parent / "fisheye_calibration"
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    for parent in dataset_path.parents:
        candidate = parent / "outputs" / dataset_path.name / "fisheye_calibration"
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)

    candidates = sorted(
        (
            run
            for root in roots
            for run in root.iterdir()
            if run.is_dir()
        ),
        key=lambda run: run.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            load_fitted_fisheye_calibration(candidate, expected_dataset=dataset_path)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            continue
        return candidate
    return None


def apply_fitted_fisheye_to_database(
    database_path: str | Path,
    calibration: FittedFisheyeCalibration,
    *,
    parameter_source: str = "active",
) -> dict[str, Any]:
    """按分辨率合并 camera，并写入指定来源的共享鱼眼 K/D。"""

    database = Path(database_path).expanduser().resolve()
    groups: list[dict[str, Any]] = []
    with sqlite3.connect(database) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        has_rig_schema = {
            "rigs",
            "rig_sensors",
            "frames",
            "frame_data",
        } <= table_names
        rows = connection.execute(
            """
            SELECT images.image_id, images.name, images.camera_id, cameras.width, cameras.height
            FROM images
            JOIN cameras ON cameras.camera_id = images.camera_id
            ORDER BY images.image_id
            """
        ).fetchall()
        if not rows:
            raise ValueError(f"COLMAP 数据库中没有图片记录：{database}")

        by_resolution: dict[tuple[int, int], list[tuple[int, str, int]]] = {}
        for image_id, name, camera_id, width, height in rows:
            by_resolution.setdefault((int(width), int(height)), []).append(
                (int(image_id), str(name), int(camera_id))
            )

        retained_camera_ids: list[int] = []
        retained_rig_ids: list[int] = []
        for resolution, images in sorted(by_resolution.items()):
            camera_id = min(row[2] for row in images)
            retained_camera_ids.append(camera_id)
            params = calibration.parameters_for_resolution(
                resolution,
                source=parameter_source,
            )
            connection.execute(
                """
                UPDATE cameras
                SET model = ?, width = ?, height = ?, params = ?, prior_focal_length = 1
                WHERE camera_id = ?
                """,
                (
                    OPENCV_FISHEYE_MODEL_ID,
                    resolution[0],
                    resolution[1],
                    sqlite3.Binary(struct.pack("<8d", *params)),
                    camera_id,
                ),
            )
            image_ids = [row[0] for row in images]
            placeholders = ",".join("?" for _ in image_ids)
            connection.execute(
                f"UPDATE images SET camera_id = ? WHERE image_id IN ({placeholders})",
                (camera_id, *image_ids),
            )
            rig_id: int | None = None
            if has_rig_schema:
                rig_row = connection.execute(
                    """
                    SELECT frames.rig_id
                    FROM frame_data
                    JOIN frames ON frames.frame_id = frame_data.frame_id
                    WHERE frame_data.data_id = ? AND frame_data.sensor_type = 0
                    """,
                    (image_ids[0],),
                ).fetchone()
                if rig_row is None:
                    raise ValueError(
                        f"COLMAP 图片缺少 camera frame/rig 关系：{images[0][1]}"
                    )
                rig_id = int(rig_row[0])
                retained_rig_ids.append(rig_id)
                # 同一内参组作为一个 camera sensor 跨多个 frame 复用；
                # data_id 仍保持逐图不变，图像位姿继续由各 frame 独立承载。
                connection.execute(
                    f"""
                    UPDATE frame_data
                    SET sensor_id = ?
                    WHERE sensor_type = 0 AND data_id IN ({placeholders})
                    """,
                    (camera_id, *image_ids),
                )
                connection.execute(
                    f"""
                    UPDATE frames
                    SET rig_id = ?
                    WHERE frame_id IN (
                        SELECT frame_id FROM frame_data
                        WHERE sensor_type = 0 AND data_id IN ({placeholders})
                    )
                    """,
                    (rig_id, *image_ids),
                )
            groups.append(
                {
                    "camera_id": camera_id,
                    "rig_id": rig_id,
                    "resolution": list(resolution),
                    "image_count": len(images),
                    "image_names": [row[1] for row in images],
                    "camera_model": "OPENCV_FISHEYE",
                    "camera_params": list(params),
                }
            )

        if has_rig_schema:
            # 单相机 rig 的参考传感器只记录在 rigs.ref_sensor_id 中；
            # rig_sensors 仅保存非参考传感器，重复写入会导致 Mapper 启动失败。
            connection.execute("DELETE FROM rig_sensors WHERE sensor_type = 0")
            for camera_id, rig_id in zip(retained_camera_ids, retained_rig_ids):
                connection.execute(
                    """
                    UPDATE rigs
                    SET ref_sensor_id = ?, ref_sensor_type = 0
                    WHERE rig_id = ?
                    """,
                    (camera_id, rig_id),
                )
            rig_placeholders = ",".join("?" for _ in retained_rig_ids)
            connection.execute(
                f"DELETE FROM rigs WHERE rig_id NOT IN ({rig_placeholders})",
                retained_rig_ids,
            )

        placeholders = ",".join("?" for _ in retained_camera_ids)
        connection.execute(
            f"DELETE FROM cameras WHERE camera_id NOT IN ({placeholders})",
            retained_camera_ids,
        )
        connection.commit()

    return {
        **calibration.report_payload(),
        "database_parameter_source": parameter_source,
        "database_path": str(database),
        "camera_group_count": len(groups),
        "camera_groups": groups,
    }
