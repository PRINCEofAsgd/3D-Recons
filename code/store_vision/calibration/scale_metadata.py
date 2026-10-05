"""`scale.txt` 的业务字段解析、尺度拟合和设备身份审计。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def _nested_json(value: Any, default: Any) -> Any:
    """解析接口响应中被二次 JSON 编码的字段。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


@dataclass(frozen=True)
class PlanScale:
    """平面图百分比与真实地面坐标之间的轴对齐仿射关系。

    `rw/rh` 的单位来自上游数据定义，为米；世界系采用 X 向平面图右侧、
    Y 向平面图上方、Z 向上，因此 `rh` 在转换到世界 Y 时需要取负。
    """

    x_metres_per_percent: float
    x_intercept_metres: float
    y_metres_per_percent: float
    y_intercept_metres: float
    rmse_metres: float
    max_error_metres: float
    control_points: tuple[dict[str, float], ...]

    def percent_to_world(self, points: np.ndarray) -> np.ndarray:
        """把 `(rx, ry)` 百分比转换成右手世界地面 `(X, Y)` 米坐标。"""
        values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        x = values[:, 0] * self.x_metres_per_percent + self.x_intercept_metres
        plan_y = values[:, 1] * self.y_metres_per_percent + self.y_intercept_metres
        return np.column_stack([x, -plan_y])

    def world_to_percent(self, points: np.ndarray) -> np.ndarray:
        """把世界地面 `(X, Y)` 米坐标反算为平面图百分比。"""
        values = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        rx = (values[:, 0] - self.x_intercept_metres) / self.x_metres_per_percent
        plan_y = -values[:, 1]
        ry = (plan_y - self.y_intercept_metres) / self.y_metres_per_percent
        return np.column_stack([rx, ry])

    def as_dict(self) -> dict[str, Any]:
        """输出可直接写入 JSON 的尺度诊断。"""
        return {
            "model": "axis_aligned_affine",
            "input_percent_fields": ["rx", "ry"],
            "metric_fields": ["rw", "rh"],
            "metric_unit": "metre",
            "world_definition": "X right, Y plan-up, Z up; ground plane Z=0",
            "x_metres_per_percent": self.x_metres_per_percent,
            "x_intercept_metres": self.x_intercept_metres,
            "y_metres_per_percent": self.y_metres_per_percent,
            "y_intercept_metres": self.y_intercept_metres,
            "estimated_plan_width_metres_at_0_100": abs(self.x_metres_per_percent) * 100.0,
            "estimated_plan_height_metres_at_0_100": abs(self.y_metres_per_percent) * 100.0,
            "rmse_metres": self.rmse_metres,
            "max_error_metres": self.max_error_metres,
            "control_points": list(self.control_points),
        }


def fit_plan_scale(points: list[dict[str, Any]]) -> PlanScale:
    """从 `rx/ry ↔ rw/rh` 控制点拟合两个一维仿射尺度。

    当前数据给出的控制点主要沿两个轴成对分布。使用全部点做最小二乘可
    保留接口小数舍入造成的残差，并避免只挑选某一对点。
    """
    rows: list[dict[str, float]] = []
    for item in points:
        try:
            row = {name: float(item[name]) for name in ("rx", "ry", "rw", "rh")}
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(list(row.values())).all():
            rows.append(row)
    if len(rows) < 3:
        raise ValueError("scale.txt 至少需要 3 个有效 calibrationPoints")

    rx = np.asarray([row["rx"] for row in rows], dtype=np.float64)
    ry = np.asarray([row["ry"] for row in rows], dtype=np.float64)
    rw = np.asarray([row["rw"] for row in rows], dtype=np.float64)
    rh = np.asarray([row["rh"] for row in rows], dtype=np.float64)
    if np.ptp(rx) < 1e-9 or np.ptp(ry) < 1e-9:
        raise ValueError("scale.txt 控制点没有覆盖两个平面坐标轴")

    x_scale, x_intercept = np.linalg.lstsq(
        np.column_stack([rx, np.ones_like(rx)]), rw, rcond=None
    )[0]
    y_scale, y_intercept = np.linalg.lstsq(
        np.column_stack([ry, np.ones_like(ry)]), rh, rcond=None
    )[0]
    if x_scale <= 0 or y_scale <= 0:
        raise ValueError("scale.txt 拟合得到非正尺度，需检查坐标字段语义")

    predicted = np.column_stack(
        [rx * x_scale + x_intercept, ry * y_scale + y_intercept]
    )
    measured = np.column_stack([rw, rh])
    errors = np.linalg.norm(predicted - measured, axis=1)
    return PlanScale(
        x_metres_per_percent=float(x_scale),
        x_intercept_metres=float(x_intercept),
        y_metres_per_percent=float(y_scale),
        y_intercept_metres=float(y_intercept),
        rmse_metres=float(np.sqrt(np.mean(errors**2))),
        max_error_metres=float(np.max(errors)),
        control_points=tuple(rows),
    )


def load_scale_metadata(path: str | Path) -> tuple[PlanScale, dict[str, Any]]:
    """读取 `scale.txt` 并返回尺度模型和未被误解释的业务元数据。"""
    source = Path(path).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("scale.txt 缺少 data 对象")

    controls = _nested_json(data.get("calibrationPoints"), [])
    if not isinstance(controls, list):
        raise ValueError("scale.txt calibrationPoints 不是列表")
    scale = fit_plan_scale(controls)
    outline = _nested_json(data.get("mallPlanCoordinates"), [])
    devices = [
        str(item.get("serialnum"))
        for item in data.get("deviceList", [])
        if isinstance(item, dict) and item.get("serialnum")
    ]
    metadata = {
        "source_file": str(source),
        "store_id": data.get("id"),
        "store_name": data.get("name"),
        "mall_plan_reference": data.get("mallPlan"),
        "mall_plan_coordinates_percent": outline if isinstance(outline, list) else [],
        "area_raw": data.get("area"),
        "area_interpretation": (
            "业务字段含义未由当前数据定义确认；数值 6.0 与控制点推得的平面范围不一致，"
            "不参与米制尺度或面积计算。"
        ),
        "device_count_declared": data.get("deviceNum"),
        "device_ids": sorted(devices),
        "field_semantics": {
            "calibrationPoints": "平面图相对坐标 rx/ry 与真实地面坐标 rw/rh 的控制点",
            "mallPlanCoordinates": "平面图相对坐标中的门店边界折线",
            "mallPlan": "服务端平面图资源引用；当前实验使用数据集内 footfallplan.png",
            "deviceList": "门店设备身份清单，不包含相机位姿或镜头类型",
        },
    }
    return scale, metadata


def build_identity_audit(
    scale_device_ids: list[str],
    calibration_device_ids: list[str],
    image_device_ids: list[str],
) -> dict[str, Any]:
    """建立 scale、cali 和图像三方设备集合的明确对应关系。"""
    scale_ids = set(scale_device_ids)
    cali_ids = set(calibration_device_ids)
    image_ids = set(image_device_ids)
    all_ids = sorted(scale_ids | cali_ids | image_ids)
    rows = [
        {
            "physical_camera_id": camera_id,
            "in_scale_device_list": camera_id in scale_ids,
            "in_cali_txt": camera_id in cali_ids,
            "has_image": camera_id in image_ids,
            "usable_for_shared_intrinsics": camera_id in cali_ids and camera_id in image_ids,
        }
        for camera_id in all_ids
    ]
    return {
        "rows": rows,
        "intersection_all_three": sorted(scale_ids & cali_ids & image_ids),
        "cali_and_image_not_scale": sorted((cali_ids & image_ids) - scale_ids),
        "scale_not_cali_or_image": sorted(scale_ids - (cali_ids & image_ids)),
        "cali_without_image": sorted(cali_ids - image_ids),
        "image_without_cali": sorted(image_ids - cali_ids),
    }
