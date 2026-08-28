"""加载人工标定、平面图和相机图片数据集。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from store_vision.data.models import CameraCalibration, Point2D, StoreDataset
from store_vision.resolution import (
    adapt_calibration_points,
    coordinates_are_normalized,
    points_inside_image,
    read_image_size,
    same_aspect_ratio,
    transform_points,
)

DEVICE_REGEX = re.compile(
    r"([0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4})"
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
CALIBRATION_FILENAMES = ("cali.txt", "cali.json")
SCALE_FILENAMES = ("scale.txt", "scale.json")
FLOOR_PLAN_FILENAMES = ("floorplan.png", "floorplan.jpg", "floorplan.jpeg")


def find_dataset_file(
    folder: str | Path,
    filenames: tuple[str, ...],
    *,
    include_calibration_subdir: bool = False,
) -> Path | None:
    """按稳定优先级寻找数据集输入文件。

    旧 ``.txt`` 文件仍排在首位以保持既有数据集行为；新增 ``.json`` 只是
    同一 API JSON 结构的显式扩展名，不改变解析语义。
    """

    root = Path(folder).expanduser()
    roots = (root / "calibration", root) if include_calibration_subdir else (root,)
    return next(
        (
            candidate
            for filename in filenames
            for candidate_root in roots
            if (candidate := candidate_root / filename).is_file()
        ),
        None,
    )


def find_calibration_path(
    folder: str | Path, *, include_calibration_subdir: bool = True
) -> Path | None:
    """寻找 ``cali.txt`` 或 ``cali.json``。"""

    return find_dataset_file(
        folder,
        CALIBRATION_FILENAMES,
        include_calibration_subdir=include_calibration_subdir,
    )


def find_scale_path(
    folder: str | Path, *, include_calibration_subdir: bool = True
) -> Path | None:
    """寻找 ``scale.txt`` 或 ``scale.json``。"""

    return find_dataset_file(
        folder,
        SCALE_FILENAMES,
        include_calibration_subdir=include_calibration_subdir,
    )


def find_floor_plan_path(folder: str | Path) -> Path | None:
    """寻找固定 GUI 数据集根目录中的 PNG/JPG 平面图。"""

    return find_dataset_file(folder, FLOOR_PLAN_FILENAMES)


def _parse_coordinates(raw: str | dict[str, Any]) -> tuple[list[Point2D], list[Point2D]]:
    obj = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(obj, dict):
        return [], []
    cam = [Point2D(float(p["x"]), float(p["y"])) for p in obj.get("cameraPoints", [])]
    mp = [Point2D(float(p["x"]), float(p["y"])) for p in obj.get("mapPoints", [])]
    return cam, mp


def _parse_overlay_polygons(raw: str | dict[str, Any]) -> list[list[Point2D]]:
    """Polygons drawn by upstream algorithms (recxysets / linexysets / passxysets)."""
    if not raw:
        return []
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    polys: list[list[Point2D]] = []
    for key in ("recxysets", "linexysets", "passxysets", "directxysets"):
        pts = obj.get(key)
        if not pts:
            continue
        polys.append([Point2D(float(p["x"]), float(p["y"])) for p in pts])
    return polys


def _positive_size(width: Any, height: Any) -> tuple[int, int] | None:
    """读取上游可选宽高字段，仅接受正整数。"""
    try:
        size = int(width), int(height)
    except (TypeError, ValueError):
        return None
    return size if min(size) > 0 else None


def _camera_from_item(item: dict[str, Any]) -> CameraCalibration | None:
    """把一条业务标定记录转换为尚未绑定图片的相机候选。"""
    serial = str(item.get("deviceSerialnum") or "")
    if not serial:
        return None
    cam_pts, map_pts = _parse_coordinates(item.get("coordinates", "{}"))
    if len(cam_pts) < 4 or len(map_pts) < 4:
        return None
    return CameraCalibration(
        device_serial=serial,
        name=item.get("name", serial),
        serialnum=item.get("serialnum", serial),
        camera_points=cam_pts[:4],
        map_points=map_pts[:4],
        overlay_polygons_img=_parse_overlay_polygons(item.get("areaInfo", "")),
        calibration_size=_positive_size(
            item.get("deviceSnapWidth"), item.get("deviceSnapHeight")
        ),
        source_metadata={
            "record_id": item.get("id"),
            "create_time": item.get("createTime"),
            "modify_time": item.get("modifyTime"),
        },
    )


def _load_cali_candidates(path: Path) -> dict[str, list[CameraCalibration]]:
    """保留同一物理设备的全部业务记录，供图片尺寸和快照编号联合配对。"""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("data", {}).get("list", [])
    by_device: dict[str, list[CameraCalibration]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        calibration = _camera_from_item(item)
        if calibration is not None:
            by_device.setdefault(calibration.device_serial, []).append(calibration)
    return by_device


def load_cali_txt(path: Path) -> dict[str, CameraCalibration]:
    """解析 cali.txt/cali.json，并按物理设备去重（无图片时优先 -101）。

    完整数据集加载由 ``load_store_inputs`` 进一步结合快照文件名和实际尺寸
    选择记录；保留此函数的旧返回形式供人工标定审计等只读调用使用。
    """
    candidates = _load_cali_candidates(path)
    by_device: dict[str, CameraCalibration] = {}
    for serial, rows in candidates.items():
        by_device[serial] = next(
            (row for row in rows if row.serialnum.endswith("-101")), rows[0]
        )
    return by_device


def _find_image(paths: list[Path], device_serial: str) -> Path | None:
    """在已选相机图片中按设备序列号匹配对应的一张图片。"""
    norm = device_serial.replace("-", "").upper()
    for p in paths:
        stem = p.stem.replace("-", "").upper()
        if norm in stem or stem in norm:
            return p
    parts = device_serial.split("-")
    if len(parts) >= 2:
        prefix = (parts[0] + parts[1]).upper()
        for p in paths:
            if prefix in p.stem.replace("-", "").upper():
                return p
    return None


def _matching_images(paths: list[Path], device_serial: str) -> list[Path]:
    """返回设备 ID 匹配的全部图片，不再在发现第一张时提前结束。"""
    normalized = device_serial.replace("-", "").upper()
    exact = [
        path
        for path in paths
        if normalized in path.stem.replace("-", "").upper()
    ]
    if exact:
        return sorted(exact)
    parts = device_serial.split("-")
    if len(parts) < 2:
        return []
    prefix = (parts[0] + parts[1]).upper()
    return sorted(
        path
        for path in paths
        if prefix in path.stem.replace("-", "").upper()
    )


def _pair_score(
    calibration: CameraCalibration,
    image_path: Path,
    image_size: tuple[int, int],
) -> tuple[int, str]:
    """按完整快照号、声明分辨率和坐标可用性给候选配对评分。"""
    stem = image_path.stem.replace("-", "").upper()
    snapshot = calibration.serialnum.replace("-", "").upper()
    device = calibration.device_serial.replace("-", "").upper()
    score = 0
    reasons: list[str] = []
    if snapshot and (snapshot in stem or stem in snapshot):
        score += 1000
        reasons.append("完整快照编号")
    elif device in stem:
        score += 100
        reasons.append("完整设备编号")

    if calibration.calibration_size == image_size:
        score += 500
        reasons.append("声明分辨率精确匹配")
    elif calibration.calibration_size and same_aspect_ratio(
        calibration.calibration_size, image_size
    ):
        score += 250
        reasons.append("声明分辨率同宽高比")
    elif calibration.calibration_size:
        score -= 500
        reasons.append("声明分辨率宽高比冲突")

    points = calibration.camera_points_array()
    if coordinates_are_normalized(points):
        score += 80
        reasons.append("归一化坐标")
    elif points_inside_image(points, image_size):
        score += 80
        reasons.append("像素坐标在图内")
    elif calibration.calibration_size is None:
        score -= 300
        reasons.append("像素坐标越界且无标定尺寸")
    if calibration.serialnum.endswith("-101"):
        score += 5
    return score, "、".join(reasons)


def _adapt_overlay_polygons(
    polygons: list[list[Point2D]],
    transform: np.ndarray,
    image_size: tuple[int, int],
) -> list[list[Point2D]]:
    """把叠加线多边形同步转换到当前图片坐标系。"""
    converted: list[list[Point2D]] = []
    for polygon in polygons:
        if not polygon:
            continue
        points = np.asarray([[point.x, point.y] for point in polygon], dtype=np.float64)
        if coordinates_are_normalized(points):
            width, height = image_size
            polygon_transform = np.array(
                [[width, 0.0, 0.0], [0.0, height, 0.0], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
        else:
            polygon_transform = transform
        values = transform_points(points, polygon_transform)
        converted.append([Point2D(float(x), float(y)) for x, y in values])
    return converted


def _select_camera_input(
    calibrations: list[CameraCalibration],
    image_paths: list[Path],
) -> tuple[CameraCalibration, list[dict[str, Any]]]:
    """联合选择业务标定记录与图片，并完成坐标适配。"""
    diagnostics: list[dict[str, Any]] = []
    serial = calibrations[0].device_serial
    matching = _matching_images(image_paths, serial)
    if not matching:
        selected = next(
            (row for row in calibrations if row.serialnum.endswith("-101")),
            calibrations[0],
        )
        issue = {
            "severity": "error",
            "code": "image_missing",
            "physical_camera_id": serial,
            "message": "没有与完整设备编号匹配的截图",
        }
        selected.input_issues.append(issue)
        diagnostics.append(issue)
        return selected, diagnostics

    sizes = {path: read_image_size(path) for path in matching}
    unreadable = [path for path, size in sizes.items() if size is None]
    for path in unreadable:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "image_unreadable",
                "physical_camera_id": serial,
                "image_path": str(path),
                "message": "候选截图无法解码，已跳过",
            }
        )
    ranked = sorted(
        (
            (
                *_pair_score(calibration, image, size),
                calibration,
                image,
                size,
            )
            for calibration in calibrations
            for image, size in sizes.items()
            if size is not None
        ),
        key=lambda row: (-row[0], str(row[3]), row[2].serialnum),
    )
    if not ranked:
        selected = calibrations[0]
        issue = {
            "severity": "error",
            "code": "all_images_unreadable",
            "physical_camera_id": serial,
            "message": "设备的全部候选截图均无法解码",
        }
        selected.input_issues.append(issue)
        diagnostics.append(issue)
        return selected, diagnostics

    score, reason, selected, image_path, image_size = ranked[0]
    selected.image_path = str(image_path)
    selected.image_size = image_size
    selected.image_match_strategy = reason
    top_ties = [row for row in ranked if row[0] == score]
    if len(top_ties) > 1:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "ambiguous_image_pair",
                "physical_camera_id": serial,
                "message": (
                    f"{len(top_ties)} 个标定/图片组合评分相同，已按文件名稳定选择 "
                    f"{image_path.name}"
                ),
            }
        )

    converted, transform, mode, issues = adapt_calibration_points(
        selected.camera_points_array(),
        image_size,
        selected.calibration_size,
    )
    selected.camera_points = [
        Point2D(float(point[0]), float(point[1])) for point in converted
    ]
    selected.overlay_polygons_img = _adapt_overlay_polygons(
        selected.overlay_polygons_img, transform, image_size
    )
    selected.calibration_to_image = transform
    selected.coordinate_mode = mode
    for issue in issues:
        issue.update(
            {
                "physical_camera_id": serial,
                "record_serialnum": selected.serialnum,
                "image_path": str(image_path),
                "calibration_size": list(selected.calibration_size)
                if selected.calibration_size
                else None,
                "image_size": list(image_size),
            }
        )
    selected.input_issues.extend(issues)
    diagnostics.extend(issues)
    diagnostics.append(
        {
            "severity": "info",
            "code": "camera_input_selected",
            "physical_camera_id": serial,
            "record_serialnum": selected.serialnum,
            "image_path": str(image_path),
            "image_size": list(image_size),
            "calibration_size": list(selected.calibration_size)
            if selected.calibration_size
            else None,
            "coordinate_mode": mode,
            "calibration_to_image": transform.tolist(),
            "match_strategy": reason,
        }
    )
    return selected, diagnostics


def _image_paths_in_folder(folder: Path) -> list[Path]:
    """Collect camera images from canonical or legacy dataset layouts."""
    paths: list[Path] = []
    roots = (
        folder / "screenshots",
        folder / "cameras" / "images",
        folder / "images",
        folder,
    )
    for root in roots:
        if root.is_dir():
            candidates = root.rglob("*") if root != folder else root.iterdir()
            paths.extend(
                sorted(
                    path
                    for path in candidates
                    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
                )
            )
    floor_names = {"footfallplan.png", "footfallplan.jpg", "floorplan.png", "floorplan.jpg", "plan.png"}
    return list(dict.fromkeys(p for p in paths if p.name.lower() not in floor_names))


def load_store_inputs(
    cali_path: str | Path,
    floor_plan_path: str | Path | None,
    camera_image_paths: list[str | Path],
) -> StoreDataset:
    """从用户分别选择的标定、平面图和多张相机图构建数据集。

    不复制或重命名用户文件；相机图依旧按文件名中的设备序列号与标定记录匹配。
    """
    cali = Path(cali_path).expanduser().resolve()
    if not cali.is_file():
        raise FileNotFoundError(f"找不到标定文件: {cali}")

    calibration_candidates = _load_cali_candidates(cali)
    floor = Path(floor_plan_path).expanduser().resolve() if floor_plan_path else None
    if floor is not None and not floor.is_file():
        raise FileNotFoundError(f"找不到平面图: {floor}")

    image_paths = [Path(path).expanduser().resolve() for path in camera_image_paths]
    image_paths = [path for path in image_paths if path.is_file()]
    cameras: dict[str, CameraCalibration] = {}
    diagnostics: list[dict[str, Any]] = []
    for serial, candidates in calibration_candidates.items():
        calibration, camera_diagnostics = _select_camera_input(
            candidates, image_paths
        )
        cameras[serial] = calibration
        diagnostics.extend(camera_diagnostics)

    floor_size: tuple[int, int] | None = None
    if floor is not None:
        image = cv2.imread(str(floor))
        if image is not None:
            floor_size = (image.shape[1], image.shape[0])

    return StoreDataset(
        # 标定文件所在目录仅用于界面初始路径和旧接口兼容，不要求所有输入同目录。
        root=str(cali.parent),
        floor_plan_path=str(floor) if floor else None,
        cameras=cameras,
        floor_plan_size=floor_size,
        input_diagnostics=diagnostics,
    )


def load_store_folder(
    folder: str | Path,
    auto_placeholder: bool = False,
) -> StoreDataset:
    folder = Path(folder)
    cali_path = find_calibration_path(folder)
    if cali_path is None:
        raise FileNotFoundError(
            f"Missing cali.txt/cali.json under calibration/ or dataset root: {folder}"
        )

    floor_path: Path | None = None
    floor_candidates = (
        folder / "floorplan" / "floorplan.png",
        folder / "floorplan" / "floorplan.jpg",
        folder / "floorplan" / "floorplan.jpeg",
        folder / "footfallplan.png",
        folder / "footfallplan.jpg",
        folder / "floorplan.png",
        folder / "floorplan.jpg",
        folder / "floorplan.jpeg",
        folder / "plan.png",
    )
    for candidate in floor_candidates:
        if candidate.is_file():
            floor_path = candidate
            break

    if floor_path is None and auto_placeholder:
        from store_vision.data.placeholder import ensure_floor_plan

        floor_path = ensure_floor_plan(folder)

    dataset = load_store_inputs(cali_path, floor_path, _image_paths_in_folder(folder))
    # 整合目录模式仍以所选目录作为数据根目录，保持旧命令行和界面行为不变。
    dataset.root = str(folder.resolve())
    scale_path = find_scale_path(folder)
    dataset.scale_path = str(scale_path.resolve()) if scale_path else None
    return dataset


def load_gui_dataset_folder(folder: str | Path) -> StoreDataset:
    """按桌面端固定目录规范读取一个完整数据集。

    根目录必须直接包含一组 ``cali.txt/cali.json``、
    ``scale.txt/scale.json`` 和 ``floorplan.png/.jpg/.jpeg``，相机图片只
    从 ``screenshots`` 子目录递归收集，避免把示意图或历史输出误当作输入。
    """

    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"数据集目录不存在：{root}")

    cali_path = find_calibration_path(root, include_calibration_subdir=False)
    scale_path = find_scale_path(root, include_calibration_subdir=False)
    floor_path = find_floor_plan_path(root)
    screenshots = root / "screenshots"
    missing = [
        name
        for name, path in (
            ("cali.txt 或 cali.json", cali_path),
            ("scale.txt 或 scale.json", scale_path),
            ("floorplan.png/jpg/jpeg", floor_path),
            ("screenshots/", screenshots),
        )
        if path is None
        or not (path.is_dir() if name.endswith("/") else path.is_file())
    ]
    if missing:
        raise FileNotFoundError(
            f"数据集缺少固定输入：{', '.join(missing)}（目录：{root}）"
        )

    image_paths = sorted(
        path
        for path in screenshots.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not image_paths:
        raise FileNotFoundError(f"screenshots 中没有可读取的图片：{screenshots}")

    assert cali_path is not None and scale_path is not None and floor_path is not None
    dataset = load_store_inputs(cali_path, floor_path, image_paths)
    dataset.root = str(root)
    dataset.scale_path = str(scale_path)
    return dataset


def device_serial_from_filename(name: str) -> str | None:
    m = DEVICE_REGEX.search(name)
    return m.group(1).upper() if m else None
