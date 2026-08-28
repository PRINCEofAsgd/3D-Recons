"""Coordinate-system conversions. percent ↔ floor px ↔ real cm."""

from __future__ import annotations

import numpy as np

from store_vision.config import StoreConfig


def map_percent_to_floor_px(
    pts: list[tuple[float, float]] | np.ndarray,
    floor_w: int,
    floor_h: int,
) -> np.ndarray:
    arr = np.asarray(pts, dtype=np.float64)
    out = arr.copy()
    out[:, 0] = arr[:, 0] / 100.0 * floor_w
    out[:, 1] = arr[:, 1] / 100.0 * floor_h
    return out


def floor_px_to_cm(
    pts: list[tuple[float, float]] | np.ndarray,
    floor_w: int,
    floor_h: int,
    cfg: StoreConfig,
) -> np.ndarray:
    arr = np.asarray(pts, dtype=np.float64)
    out = arr.copy()
    out[:, 0] = arr[:, 0] / floor_w * cfg.floor_plan_width_cm
    out[:, 1] = arr[:, 1] / floor_h * cfg.floor_plan_height_cm
    return out


def map_percent_to_cm(
    pts: list[tuple[float, float]] | np.ndarray,
    cfg: StoreConfig,
) -> np.ndarray:
    arr = np.asarray(pts, dtype=np.float64)
    out = arr.copy()
    out[:, 0] = arr[:, 0] / 100.0 * cfg.floor_plan_width_cm
    out[:, 1] = arr[:, 1] / 100.0 * cfg.floor_plan_height_cm
    return out


def cm_to_floor_px(
    pts_cm: np.ndarray,
    floor_w: int,
    floor_h: int,
    cfg: StoreConfig,
) -> np.ndarray:
    out = pts_cm.copy()
    out[:, 0] = pts_cm[:, 0] / cfg.floor_plan_width_cm * floor_w
    out[:, 1] = pts_cm[:, 1] / cfg.floor_plan_height_cm * floor_h
    return out
