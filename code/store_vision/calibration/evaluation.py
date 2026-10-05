"""Calibration metrics computed only from supplied measurements."""

from __future__ import annotations

import numpy as np

from store_vision.calibration.models import CalibrationMetrics


def position_errors(reference: np.ndarray, estimate: np.ndarray) -> np.ndarray:
    reference = np.asarray(reference, dtype=np.float64)
    estimate = np.asarray(estimate, dtype=np.float64)
    if reference.shape != estimate.shape or reference.ndim != 2:
        raise ValueError("position arrays must have matching (N, D) shapes")
    return np.linalg.norm(reference - estimate, axis=1)


def orientation_errors_deg(reference_wxyz: np.ndarray, estimate_wxyz: np.ndarray) -> np.ndarray:
    reference = np.asarray(reference_wxyz, dtype=np.float64)
    estimate = np.asarray(estimate_wxyz, dtype=np.float64)
    if reference.shape != estimate.shape or reference.ndim != 2 or reference.shape[1] != 4:
        raise ValueError("orientation arrays must have matching (N, 4) shapes")
    reference = reference / np.linalg.norm(reference, axis=1, keepdims=True)
    estimate = estimate / np.linalg.norm(estimate, axis=1, keepdims=True)
    dots = np.clip(np.abs(np.sum(reference * estimate, axis=1)), 0.0, 1.0)
    return np.degrees(2.0 * np.arccos(dots))


def projection_errors(reference_xy: np.ndarray, estimate_xy: np.ndarray) -> np.ndarray:
    reference = np.asarray(reference_xy, dtype=np.float64)
    estimate = np.asarray(estimate_xy, dtype=np.float64)
    if reference.shape != estimate.shape or reference.ndim != 2 or reference.shape[1] != 2:
        raise ValueError("projection arrays must have matching (N, 2) shapes")
    return np.linalg.norm(reference - estimate, axis=1)


def _summary(values: np.ndarray | None) -> tuple[float | None, float | None]:
    if values is None or len(values) == 0:
        return None, None
    return float(np.mean(values)), float(np.max(values))


def evaluate_calibration(
    *,
    reference_positions: np.ndarray | None = None,
    estimated_positions: np.ndarray | None = None,
    reference_orientations: np.ndarray | None = None,
    estimated_orientations: np.ndarray | None = None,
    reference_projections: np.ndarray | None = None,
    estimated_projections: np.ndarray | None = None,
) -> CalibrationMetrics:
    positions = (
        position_errors(reference_positions, estimated_positions)
        if reference_positions is not None and estimated_positions is not None
        else None
    )
    orientations = (
        orientation_errors_deg(reference_orientations, estimated_orientations)
        if reference_orientations is not None and estimated_orientations is not None
        else None
    )
    projections = (
        projection_errors(reference_projections, estimated_projections)
        if reference_projections is not None and estimated_projections is not None
        else None
    )
    position_mean, position_max = _summary(positions)
    orientation_mean, orientation_max = _summary(orientations)
    projection_mean, projection_max = _summary(projections)
    counts = [len(values) for values in (positions, orientations) if values is not None]
    return CalibrationMetrics(
        matched_cameras=min(counts) if counts else (len(positions) if positions is not None else 0),
        position_mean=position_mean,
        position_max=position_max,
        orientation_mean_deg=orientation_mean,
        orientation_max_deg=orientation_max,
        projection_mean=projection_mean,
        projection_max=projection_max,
    )
