"""Data contracts shared by the calibration-demo workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class CameraIntrinsics:
    camera_id: int | str
    model: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class CameraPose:
    """COLMAP-style world-to-camera pose."""

    quaternion_wxyz: tuple[float, float, float, float]
    translation_xyz: tuple[float, float, float]

    def rotation_matrix(self) -> np.ndarray:
        q = np.asarray(self.quaternion_wxyz, dtype=np.float64)
        norm = float(np.linalg.norm(q))
        if norm == 0:
            raise ValueError("camera pose quaternion must be non-zero")
        w, x, y, z = q / norm
        return np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ],
            dtype=np.float64,
        )

    def camera_center(self) -> tuple[float, float, float]:
        center = -self.rotation_matrix().T @ np.asarray(self.translation_xyz)
        return tuple(float(value) for value in center)


@dataclass
class CameraRecord:
    name: str
    camera_id: int | str | None = None
    image_path: Path | None = None
    intrinsics: CameraIntrinsics | None = None
    pose: CameraPose | None = None
    image_points: list[tuple[float, float]] = field(default_factory=list)
    floor_points: list[tuple[float, float]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconstructionSummary:
    registered_images: int = 0
    points3d: int = 0
    mean_reprojection_error: float | None = None
    cameras: dict[int, CameraIntrinsics] = field(default_factory=dict)
    images: dict[int, CameraRecord] = field(default_factory=dict)


@dataclass
class CalibrationMetrics:
    matched_cameras: int = 0
    position_mean: float | None = None
    position_max: float | None = None
    orientation_mean_deg: float | None = None
    orientation_max_deg: float | None = None
    projection_mean: float | None = None
    projection_max: float | None = None
