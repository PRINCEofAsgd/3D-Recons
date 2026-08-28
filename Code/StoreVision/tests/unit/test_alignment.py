from __future__ import annotations

import numpy as np
import pytest

from store_vision.calibration.alignment import align_point_sets


def test_similarity_alignment_recovers_2d_transform():
    source = np.array([[0, 0], [1, 0], [0, 1], [2, 2]], dtype=float)
    rotation = np.array([[0, -1], [1, 0]], dtype=float)
    target = 2.5 * (source @ rotation.T) + np.array([10, -3])
    transform = align_point_sets(source, target)
    assert transform.scale == pytest.approx(2.5)
    np.testing.assert_allclose(transform.rotation, rotation, atol=1e-10)
    np.testing.assert_allclose(transform.apply(source), target, atol=1e-10)


def test_similarity_alignment_rejects_too_few_points():
    with pytest.raises(ValueError, match="at least 2"):
        align_point_sets([[0, 0]], [[1, 1]])
