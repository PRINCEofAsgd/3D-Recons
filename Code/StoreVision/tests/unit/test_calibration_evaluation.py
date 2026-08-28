from __future__ import annotations

import numpy as np
import pytest

from store_vision.calibration.evaluation import evaluate_calibration


def test_calibration_metrics_come_from_inputs():
    metrics = evaluate_calibration(
        reference_positions=np.array([[0, 0, 0], [1, 0, 0]], dtype=float),
        estimated_positions=np.array([[0, 0, 0], [4, 4, 0]], dtype=float),
        reference_orientations=np.array([[1, 0, 0, 0], [1, 0, 0, 0]], dtype=float),
        estimated_orientations=np.array([[1, 0, 0, 0], [0, 0, 0, 1]], dtype=float),
        reference_projections=np.array([[0, 0], [3, 4]], dtype=float),
        estimated_projections=np.array([[0, 0], [0, 0]], dtype=float),
    )
    assert metrics.matched_cameras == 2
    assert metrics.position_mean == pytest.approx(2.5)
    assert metrics.position_max == pytest.approx(5.0)
    assert metrics.orientation_mean_deg == pytest.approx(90.0)
    assert metrics.orientation_max_deg == pytest.approx(180.0)
    assert metrics.projection_mean == pytest.approx(2.5)
    assert metrics.projection_max == pytest.approx(5.0)


def test_empty_evaluation_does_not_invent_values():
    metrics = evaluate_calibration()
    assert metrics.matched_cameras == 0
    assert metrics.position_mean is None
    assert metrics.orientation_mean_deg is None
    assert metrics.projection_mean is None
