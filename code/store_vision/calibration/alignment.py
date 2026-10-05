"""Similarity alignment from reconstruction coordinates to floor-plan coordinates."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SimilarityTransform:
    scale: float
    rotation: np.ndarray
    translation: np.ndarray

    def apply(self, points: np.ndarray | list[list[float]]) -> np.ndarray:
        values = np.asarray(points, dtype=np.float64)
        return self.scale * (values @ self.rotation.T) + self.translation


def align_point_sets(
    source_points: np.ndarray | list[list[float]],
    target_points: np.ndarray | list[list[float]],
) -> SimilarityTransform:
    """Least-squares Umeyama alignment with scale, rotation and translation."""
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2:
        raise ValueError("source and target point sets must have the same (N, D) shape")
    count, dimensions = source.shape
    if count < dimensions:
        raise ValueError(f"at least {dimensions} corresponding points are required")
    if np.linalg.matrix_rank(source - source.mean(axis=0)) < dimensions - 1:
        raise ValueError("source points are degenerate for similarity alignment")

    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = target_centered.T @ source_centered / count
    left, singular_values, right_t = np.linalg.svd(covariance)
    correction = np.eye(dimensions)
    if np.linalg.det(left) * np.linalg.det(right_t) < 0:
        correction[-1, -1] = -1
    rotation = left @ correction @ right_t
    variance = float(np.mean(np.sum(source_centered * source_centered, axis=1)))
    if variance <= np.finfo(float).eps:
        raise ValueError("source points must not all be identical")
    scale = float(np.sum(singular_values * np.diag(correction)) / variance)
    translation = target_mean - scale * (rotation @ source_mean)
    return SimilarityTransform(scale, rotation, translation)
