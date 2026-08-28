"""Adapters for the existing manual calibration formats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from store_vision.calibration.models import CameraPose, CameraRecord
from store_vision.data.loader import find_calibration_path, load_cali_txt


def find_manual_calibration(dataset: str | Path) -> Path:
    root = Path(dataset)
    calibration = find_calibration_path(root)
    if calibration is not None:
        return calibration
    for candidate in (root / "cameras" / "manual_pose.json", root / "manual_pose.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No manual calibration found under {root}; expected cali.txt/cali.json "
        "or manual_pose.json"
    )


def _pose_from_json(value: dict[str, Any]) -> CameraPose | None:
    quaternion = value.get("quaternion_wxyz") or value.get("qvec")
    translation = value.get("translation_xyz") or value.get("tvec")
    if quaternion is None or translation is None:
        return None
    if len(quaternion) != 4 or len(translation) != 3:
        raise ValueError("manual pose requires quaternion_wxyz[4] and translation_xyz[3]")
    return CameraPose(tuple(map(float, quaternion)), tuple(map(float, translation)))


def _load_manual_pose_json(path: Path) -> list[CameraRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("cameras", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError(f"manual pose JSON must contain a camera list: {path}")
    records: list[CameraRecord] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"manual pose item {index} must be an object")
        records.append(
            CameraRecord(
                name=str(item.get("name") or item.get("image_name") or index),
                camera_id=item.get("camera_id"),
                image_path=Path(item["image_path"]) if item.get("image_path") else None,
                pose=_pose_from_json(item),
                image_points=[tuple(map(float, point)) for point in item.get("image_points", [])],
                floor_points=[tuple(map(float, point)) for point in item.get("floor_points", [])],
                metadata={"source": str(path)},
            )
        )
    return records


def load_manual_calibration(path_or_dataset: str | Path) -> list[CameraRecord]:
    """加载 ``cali.txt/cali.json`` 或可选的结构化 manual-pose JSON。"""
    path = Path(path_or_dataset)
    if path.is_dir():
        path = find_manual_calibration(path)
    if path.name.lower() == "manual_pose.json":
        return _load_manual_pose_json(path)

    records: list[CameraRecord] = []
    for serial, camera in load_cali_txt(path).items():
        records.append(
            CameraRecord(
                name=serial,
                camera_id=serial,
                image_points=[point.as_tuple() for point in camera.camera_points],
                floor_points=[point.as_tuple() for point in camera.map_points],
                metadata={
                    "source": str(path),
                    "display_name": camera.name,
                    "serialnum": camera.serialnum,
                    "coordinate_unit": "floorplan_percent",
                },
            )
        )
    return records
