"""JSON and console output for actual calibration-demo observations."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_calibration_report(report: Any, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_value(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def console_summary(report: Any) -> str:
    payload = _json_value(report)
    status = payload.get("colmap_status", "unknown")
    manual_count = payload.get("manual_camera_count", 0)
    reconstruction = payload.get("reconstruction") or {}
    registered = payload.get("registered_images", reconstruction.get("registered_images", 0))
    points = payload.get("sparse_points", reconstruction.get("points3d", 0))
    return (
        f"Calibration demo: manual={manual_count}, COLMAP={status}, "
        f"registered={registered}, points3D={points}"
    )
