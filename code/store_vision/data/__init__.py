"""数据层公共导出。

基础模型和加载器可在包初始化阶段直接导入；工作区模块会反向依赖标定模块，
因此必须按属性惰性加载，避免命令行冷启动形成循环导入。
"""

from importlib import import_module
from typing import Any

from store_vision.data.loader import (
    load_gui_dataset_folder,
    load_store_folder,
    load_store_inputs,
)
from store_vision.resolution import (
    adapt_calibration_points,
    canonicalize_homographies,
    group_rows_by_aspect,
)
from store_vision.data.models import (
    CameraCalibration,
    MapObject25D,
    Point2D,
    StoreDataset,
)

_WORKSPACE_EXPORTS = frozenset(
    {
        "CapabilityReport",
        "IntermediatePackageSummary",
        "IntermediateRuntime",
        "WorkspacePaths",
        "candidate_names",
        "create_unique_run_directory",
        "discover_intermediate_packages",
        "find_latest_intermediate_package",
        "import_external_calibration_package",
        "load_intermediate_runtime",
        "load_manifest",
        "load_sfm_evidence",
        "publish_intermediate_package",
        "publish_sfm_derived_package",
        "resolve_capabilities",
        "workspace_paths",
    }
)


def __getattr__(name: str) -> Any:
    """首次访问工作区公开符号时再加载模块，并缓存解析结果。"""

    if name == "workspace":
        module = import_module("store_vision.data.workspace")
        globals()[name] = module
        return module
    if name in _WORKSPACE_EXPORTS:
        module = import_module("store_vision.data.workspace")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """让交互式补全能够看到惰性公开符号。"""

    return sorted(set(globals()) | _WORKSPACE_EXPORTS | {"workspace"})

__all__ = [
    "CameraCalibration",
    "MapObject25D",
    "Point2D",
    "StoreDataset",
    "load_store_folder",
    "load_gui_dataset_folder",
    "load_store_inputs",
    "adapt_calibration_points",
    "canonicalize_homographies",
    "group_rows_by_aspect",
    "CapabilityReport",
    "IntermediatePackageSummary",
    "IntermediateRuntime",
    "WorkspacePaths",
    "candidate_names",
    "create_unique_run_directory",
    "discover_intermediate_packages",
    "find_latest_intermediate_package",
    "import_external_calibration_package",
    "load_intermediate_runtime",
    "load_manifest",
    "load_sfm_evidence",
    "publish_intermediate_package",
    "publish_sfm_derived_package",
    "resolve_capabilities",
    "workspace_paths",
]
