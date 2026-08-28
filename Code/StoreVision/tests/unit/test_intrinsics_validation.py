"""共享鱼眼内参可信度门禁测试。"""

from __future__ import annotations

import numpy as np
import pytest

from store_vision.calibration.intrinsics_validation import (
    provisional_fisheye_intrinsics,
    validate_fisheye_intrinsics,
)


def test_credible_fisheye_intrinsics_pass_all_hard_gates():
    """主点在图内且投影单调覆盖整幅图时允许进入 SfM。"""

    validation = validate_fisheye_intrinsics(
        [[1136.0, 0.0, 1179.0], [0.0, 1082.0, 813.0], [0.0, 0.0, 1.0]],
        [0.0, 0.0, 0.0, 0.0],
        (2592, 1944),
        fit_summary={
            "accepted_by_reprojection_threshold": True,
            "solver_converged": True,
        },
        ba_summary={
            "accepted_by_reprojection_threshold": True,
            "solver_converged": True,
        },
    )

    assert validation["status"] == "credible"
    assert validation["usable_for_sfm"]
    assert validation["radial_monotonic_through_image"]


def test_outside_principal_point_and_folded_distortion_are_rejected():
    """低 RMSE 不能掩盖主点越界或高阶鱼眼投影折返。"""

    validation = validate_fisheye_intrinsics(
        [[3330.0, 0.0, 478.0], [0.0, 3301.0, -290.0], [0.0, 0.0, 1.0]],
        [-0.745, 0.905, 0.705, -1.253],
        (2592, 1944),
        fit_summary={
            "accepted_by_reprojection_threshold": True,
            "solver_converged": False,
        },
        ba_summary={
            "accepted_by_reprojection_threshold": True,
            "solver_converged": False,
        },
    )

    assert validation["status"] == "rejected"
    assert not validation["usable_for_sfm"]
    assert any("主点" in reason for reason in validation["hard_failures"])


def test_provisional_route_keeps_nonzero_d_and_restores_invertibility():
    """试算分支使用初始 K 和缩放后的非零 D，不偷换成 D=0。"""

    _, distortion, scale, validation = provisional_fisheye_intrinsics(
        [[1136.0, 0.0, 1179.0], [0.0, 1082.0, 813.0], [0.0, 0.0, 1.0]],
        [-0.745, 0.905, 0.705, -1.253],
        (2592, 1944),
    )

    assert 0 < scale < 1
    assert np.any(np.abs(distortion) > 0)
    assert validation["usable_for_sfm"]


def test_provisional_route_rejects_zero_distortion():
    """试算路线不得把固定 D=0 伪装成拟合鱼眼结果。"""

    with pytest.raises(ValueError, match="D 全为零"):
        provisional_fisheye_intrinsics(
            [[1136.0, 0.0, 1179.0], [0.0, 1082.0, 813.0], [0.0, 0.0, 1.0]],
            [0.0, 0.0, 0.0, 0.0],
            (2592, 1944),
        )
