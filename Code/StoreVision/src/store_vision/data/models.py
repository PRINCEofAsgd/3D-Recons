"""Core data classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Point2D:
    x: float
    y: float

    def as_array(self) -> np.ndarray:
        return np.array([self.x, self.y], dtype=np.float64)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class CameraCalibration:
    """One physical camera. Calibration = 4 ground correspondences."""

    device_serial: str
    name: str
    serialnum: str
    camera_points: list[Point2D]  # image pixels  (u, v)
    map_points: list[Point2D]  # plan percent      (px%, py%)
    image_path: str | None = None
    overlay_polygons_img: list[list[Point2D]] = field(default_factory=list)
    # 输入坐标元数据。标定坐标始终在加载阶段转换到当前 ``image_path`` 的
    # 像素坐标系，原图不缩放、不覆盖。
    calibration_size: tuple[int, int] | None = None
    image_size: tuple[int, int] | None = None
    coordinate_mode: str = "pixel_unknown"
    calibration_to_image: np.ndarray = field(
        default_factory=lambda: np.eye(3, dtype=np.float64)
    )
    image_match_strategy: str = "unmatched"
    input_issues: list[dict[str, Any]] = field(default_factory=list)
    source_metadata: dict[str, Any] = field(default_factory=dict)

    # Filled by calibration pipeline.
    camera_model: str = "OPENCV_FISHEYE"
    K: np.ndarray | None = None  # 3x3 intrinsics (after distortion estimation)
    dist_coeffs: np.ndarray | None = None  # OpenCV fisheye (k1,k2,k3,k4)
    R: np.ndarray | None = None  # rotation matrix
    t: np.ndarray | None = None  # translation
    homography: np.ndarray | None = None  # H: undistorted image px → floor px
    homography_inv: np.ndarray | None = None
    reprojection_rmse: float = 0.0

    def map_points_pct_array(self) -> np.ndarray:
        return np.array([[p.x, p.y] for p in self.map_points], dtype=np.float64)

    def camera_points_array(self) -> np.ndarray:
        return np.array([[p.x, p.y] for p in self.camera_points], dtype=np.float64)


@dataclass
class MapObject25D:
    """A 2.5D footprint with extrusion height."""

    id: str
    label: str
    polygon_cm: list[tuple[float, float]]  # closed-ring optional
    height_cm: float
    source_camera: str | None = None
    score: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_geojson_feature(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [list(p) + [0.0] for p in self.polygon_cm]
                    + [list(self.polygon_cm[0]) + [0.0]]
                ]
                if self.polygon_cm
                else [],
            },
            "properties": {
                "label": self.label,
                "height_cm": self.height_cm,
                "extrude": True,
                "score": self.score,
                "source_camera": self.source_camera,
                **self.meta,
            },
        }


@dataclass
class StoreDataset:
    """Loaded store folder."""

    root: str
    floor_plan_path: str | None
    cameras: dict[str, CameraCalibration]
    floor_plan_size: tuple[int, int] | None = None  # (W, H) in pixels
    # GUI 标准数据集中的第二个标定文件；旧数据布局和 headless 兼容加载可为空。
    scale_path: str | None = None
    # 逐相机输入配对、分辨率和坐标变换诊断；error 会阻止几何流水线。
    input_diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def camera_list(self) -> list[CameraCalibration]:
        return list(self.cameras.values())

    def resolution_errors(self) -> list[dict[str, Any]]:
        """返回会令标定/投影坐标不可信的输入错误。"""
        return [
            row for row in self.input_diagnostics if row.get("severity") == "error"
        ]
