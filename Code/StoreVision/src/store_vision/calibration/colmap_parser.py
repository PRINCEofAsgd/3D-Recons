"""Parsers for COLMAP's documented text model format."""

from __future__ import annotations

from pathlib import Path

from store_vision.calibration.models import (
    CameraIntrinsics,
    CameraPose,
    CameraRecord,
    ReconstructionSummary,
)


def _data_lines(path: str | Path) -> list[str]:
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def parse_cameras_text(path: str | Path) -> dict[int, CameraIntrinsics]:
    cameras: dict[int, CameraIntrinsics] = {}
    for line in _data_lines(path):
        fields = line.split()
        if len(fields) < 5:
            raise ValueError(f"invalid COLMAP camera line: {line}")
        camera_id = int(fields[0])
        cameras[camera_id] = CameraIntrinsics(
            camera_id=camera_id,
            model=fields[1],
            width=int(fields[2]),
            height=int(fields[3]),
            params=tuple(float(value) for value in fields[4:]),
        )
    return cameras


def parse_images_text(
    path: str | Path,
    cameras: dict[int, CameraIntrinsics] | None = None,
) -> dict[int, CameraRecord]:
    """解析两行一图格式，并保留真实三维点观测数作为 SfM 验收证据。"""
    lines = [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    images: dict[int, CameraRecord] = {}
    index = 0
    while index < len(lines):
        if not lines[index]:
            index += 1
            continue
        fields = lines[index].split()
        if len(fields) < 10:
            raise ValueError(f"invalid COLMAP image line: {lines[index]}")
        image_id = int(fields[0])
        camera_id = int(fields[8])
        observation_fields: list[str] = []
        if index + 1 < len(lines):
            observation_fields = lines[index + 1].split()
        if len(observation_fields) % 3 != 0:
            raise ValueError(
                f"invalid COLMAP POINTS2D row for image {image_id}: "
                f"{lines[index + 1] if index + 1 < len(lines) else ''}"
            )
        point3d_ids = [
            int(observation_fields[offset + 2])
            for offset in range(0, len(observation_fields), 3)
        ]
        images[image_id] = CameraRecord(
            name=" ".join(fields[9:]),
            camera_id=camera_id,
            intrinsics=(cameras or {}).get(camera_id),
            pose=CameraPose(
                quaternion_wxyz=tuple(float(value) for value in fields[1:5]),
                translation_xyz=tuple(float(value) for value in fields[5:8]),
            ),
            metadata={
                "point2d_count": len(point3d_ids),
                "point3d_observation_count": sum(
                    point3d_id >= 0 for point3d_id in point3d_ids
                ),
            },
        )
        index += 1
        if index < len(lines):
            index += 1  # the following physical line is the POINTS2D observation list
    return images


def parse_points3d_text(path: str | Path) -> tuple[int, float | None]:
    errors: list[float] = []
    for line in _data_lines(path):
        fields = line.split()
        if len(fields) < 8:
            raise ValueError(f"invalid COLMAP point3D line: {line}")
        errors.append(float(fields[7]))
    mean_error = sum(errors) / len(errors) if errors else None
    return len(errors), mean_error


def parse_text_model(model_dir: str | Path) -> ReconstructionSummary:
    root = Path(model_dir)
    cameras_path = root / "cameras.txt"
    images_path = root / "images.txt"
    points_path = root / "points3D.txt"
    missing = [str(path.name) for path in (cameras_path, images_path, points_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"COLMAP text model is incomplete in {root}: missing {', '.join(missing)}")
    cameras = parse_cameras_text(cameras_path)
    images = parse_images_text(images_path, cameras)
    point_count, mean_error = parse_points3d_text(points_path)
    return ReconstructionSummary(
        registered_images=len(images),
        points3d=point_count,
        mean_reprojection_error=mean_error,
        cameras=cameras,
        images=images,
    )
