"""Per-store configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _default_output_root() -> Path:
    """返回兼容旧完整流水线的输出根目录。"""

    # 代码包与 tests 并列于 code/，上两级是仓库根目录。
    repository = Path(__file__).resolve().parents[2]
    if (repository / "code" / "pyproject.toml").is_file():
        return repository / "outputs"
    return Path("outputs")


def _default_workspace_data_root() -> Path:
    """返回统一输入、中间层和两类业务输出的 data 根目录。"""

    # 与输出根目录使用同一仓库定位规则。
    repository = Path(__file__).resolve().parents[2]
    if (repository / "code" / "pyproject.toml").is_file():
        return repository / "data"
    return Path("data")


@dataclass
class StoreConfig:
    """Tunables shared across modules; JSON-serialisable."""

    # 输入几何安全：未知裁剪、无标定尺寸的越界点等错误默认阻止流水线。
    # 只读加载仍会成功，便于 GUI 展示逐相机诊断。
    strict_input_resolution: bool = True

    # Physical floor plan size in cm — used to convert percent → real XY.
    floor_plan_width_cm: float = 2000.0
    floor_plan_height_cm: float = 1200.0

    # Object heights in cm (used by 2.5D extrusion).
    table_height_cm: float = 70.0
    shelf_height_cm: float = 150.0

    # White-tabletop detection (HSV "white" + LAB neutrality).
    # Real Apple-store tables are V>=230, S<=20; floor wood is V~160-185 with
    # measurable saturation, so these thresholds carve cleanly.
    white_v_min: int = 210
    white_s_max: int = 30
    white_lab_l_min: int = 195
    white_lab_chroma_max: int = 14  # |a-128| & |b-128| <= chroma_max
    table_size_cm_range: tuple[float, float] = (60.0, 280.0)
    table_aspect_max: float = 3.5
    table_convexity_min: float = 0.80
    table_rectangularity_min: float = 0.60
    # Morphological close kernel for merging fragmented tabletops (px).
    table_close_kernel_px: int = 21
    # 上述历史像素参数以既有 2560px 测试/生产基线为参考；实际按长边缩放。
    detection_reference_long_side_px: int = 2560
    # Floor-side gate: a candidate's projected centroid must lie within this
    # many cm of the camera's calibrated floor patch.
    table_floor_gate_cm: float = 350.0
    # Final acceptance score.
    table_score_threshold: float = 0.55
    # Single-camera confirmations must clear this stricter score; multi-camera
    # confirmations always pass with the lower threshold above. This is the
    # main false-positive killer for wall displays / shelves.
    table_score_threshold_single_cam: float = 0.70
    # Cross-camera deduplication (cm) — tables closer than this on the floor
    # plane are merged into one MapObject25D.
    table_dedup_distance_cm: float = 80.0

    # Overlay (virtual line) filter.
    overlay_min_saturation: int = 120
    overlay_line_min_len_px: int = 60
    overlay_inpaint_radius: int = 5

    # Stitching.
    stitch_feather_px: float = 60.0
    stitch_quad_expand: float = 0.10  # use only inside (quad * (1+expand))
    stitch_gain_compensation: bool = True
    stitch_weight_blur_px: int = 31  # Gaussian blur on per-camera weight mask
    # 精确 K/D/R/t 路线按米制地面建画布；尺寸上限用于保护桌面端内存。
    parameter_stitch_pixels_per_metre: float = 40.0
    parameter_stitch_fallback_radius_metres: float = 20.0
    parameter_stitch_max_ground_distance_metres: float = 20.0
    parameter_stitch_max_canvas_long_edge: int = 5000
    parameter_stitch_max_canvas_pixels: int = 20_000_000

    # COLMAP toggle.
    use_colmap: bool = False

    # Output.
    output_root: Path = field(default_factory=_default_output_root)
    workspace_data_root: Path = field(
        default_factory=_default_workspace_data_root
    )
    thumb_max_px: int = 110


DEFAULT_CONFIG = StoreConfig()
