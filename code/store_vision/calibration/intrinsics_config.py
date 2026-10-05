"""真实相机内参配置模板的解析与保守校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# 参数名称对应 COLMAP 已支持的常用相机模型；不在此表的模型明确拒绝。
MODEL_PARAMETERS: dict[str, tuple[str, ...]] = {
    "SIMPLE_PINHOLE": ("f", "cx", "cy"),
    "PINHOLE": ("fx", "fy", "cx", "cy"),
    "SIMPLE_RADIAL": ("f", "cx", "cy", "k1"),
    "RADIAL": ("f", "cx", "cy", "k1", "k2"),
    "OPENCV": ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2"),
    "FULL_OPENCV": ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"),
}


def _flatten_params(group: dict[str, Any], model: str) -> dict[str, Any]:
    """按相机模型将基础参数和畸变参数合并为统一名称映射。"""
    params = dict(group.get("params") or {})
    distortion = group.get("distortion")
    if isinstance(distortion, dict):
        params.update(distortion)
    elif isinstance(distortion, list):
        distortion_names = [name for name in MODEL_PARAMETERS.get(model, ()) if name not in {"f", "fx", "fy", "cx", "cy"}]
        if len(distortion) == len(distortion_names):
            params.update(dict(zip(distortion_names, distortion)))
        else:
            params["__invalid_distortion_length__"] = len(distortion)
    elif distortion is not None:
        params["__invalid_distortion_type__"] = type(distortion).__name__
    return params


def validate_intrinsics_payload(
    payload: dict[str, Any],
    available_images: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """校验分组、模型、分辨率和参数，不自动猜测任何缺失值。"""

    errors: list[str] = []
    warnings: list[str] = []
    groups = payload.get("camera_groups")
    if not isinstance(groups, list) or not groups:
        errors.append("camera_groups must be a non-empty list")
        groups = []
    assigned: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        label = f"camera_groups[{index}]"
        if not isinstance(group, dict):
            errors.append(f"{label} must be an object")
            continue
        group_id = str(group.get("group_id") or "")
        if not group_id:
            errors.append(f"{label}.group_id is required")
        model = str(group.get("camera_model") or "").upper()
        if model not in MODEL_PARAMETERS:
            errors.append(f"{label}.camera_model is unsupported: {model or '<missing>'}")
        images = group.get("images")
        if not isinstance(images, list) or not images:
            errors.append(f"{label}.images must be a non-empty list")
            images = []
        width, height = group.get("width"), group.get("height")
        if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
            errors.append(f"{label}.width and height must be positive integers")
        for image in images:
            image = str(image)
            if image not in available_images:
                errors.append(f"{label} references unknown image: {image}")
                continue
            if image in assigned:
                errors.append(f"image is assigned to multiple groups: {image} ({assigned[image]}, {group_id})")
            assigned[image] = group_id
            expected_width, expected_height = available_images[image]
            if (width, height) != (expected_width, expected_height):
                errors.append(
                    f"{label} resolution {width}x{height} does not match {image} ({expected_width}x{expected_height})"
                )
        params = _flatten_params(group, model)
        if "__invalid_distortion_length__" in params:
            errors.append(f"{label}.distortion has the wrong number of coefficients")
        if "__invalid_distortion_type__" in params:
            errors.append(f"{label}.distortion must be an object, list or null")
        expected_params = MODEL_PARAMETERS.get(model, ())
        missing_params = [name for name in expected_params if params.get(name) is None]
        if missing_params:
            errors.append(f"{label} missing required parameters: {', '.join(missing_params)}")
        invalid_values = [name for name in expected_params if params.get(name) is not None and not isinstance(params[name], (int, float))]
        if invalid_values:
            errors.append(f"{label} parameters must be numeric: {', '.join(invalid_values)}")
        if len(images) > 1 and not group.get("sharing_basis"):
            warnings.append(
                f"{label} shares intrinsics across {len(images)} physical images without a documented sharing_basis"
            )
        normalized.append(
            {
                "group_id": group_id,
                "camera_model": model,
                "width": width,
                "height": height,
                "params": [params.get(name) for name in expected_params],
                "param_names": list(expected_params),
                "images": [str(image) for image in images],
            }
        )
    unassigned = sorted(set(available_images) - set(assigned))
    if unassigned:
        errors.append(f"images are not assigned to any camera group: {', '.join(unassigned)}")
    return {
        "status": "valid" if not errors else "invalid",
        "ready_for_known_intrinsics": not errors,
        "errors": errors,
        "warnings": warnings,
        "unassigned_images": unassigned,
        "camera_groups": normalized,
    }


def load_and_validate_intrinsics(
    path: str | Path | None,
    available_images: dict[str, tuple[int, int]],
    *,
    template_path: str | Path | None = None,
) -> dict[str, Any]:
    if path is None:
        return {
            "status": "missing_required_intrinsics",
            "ready_for_known_intrinsics": False,
            "source": None,
            "template": str(template_path) if template_path else None,
            "errors": ["No measured intrinsics configuration was provided."],
            "warnings": ["No guessed or EXIF-derived values were substituted."],
        }
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {
            "status": "invalid",
            "ready_for_known_intrinsics": False,
            "source": str(source),
            "errors": [f"intrinsics configuration not found: {source}"],
            "warnings": [],
        }
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "invalid",
            "ready_for_known_intrinsics": False,
            "source": str(source),
            "errors": [f"could not parse intrinsics JSON: {exc}"],
            "warnings": [],
        }
    result = validate_intrinsics_payload(payload, available_images)
    result["source"] = str(source)
    return result
