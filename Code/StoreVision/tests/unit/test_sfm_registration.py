"""多候选 SfM 编排的输出隔离测试。"""

from __future__ import annotations

from store_vision.calibration.sfm_registration import (
    _clear_previous_generated_models,
)


def test_previous_models_are_cleared_without_touching_other_outputs(tmp_path):
    """同目录重跑不得复用旧模型，也不得删除无关报告和日志。"""

    generated = (
        tmp_path / "sparse_candidates",
        tmp_path / "model_candidates",
        tmp_path / "pose_seed_model",
        tmp_path / "sparse",
        tmp_path / "model_txt",
    )
    for directory in generated:
        directory.mkdir(parents=True)
        (directory / "stale.bin").write_bytes(b"old")
    comparison = tmp_path / "reports" / "sfm_registration.json"
    comparison.parent.mkdir(parents=True, exist_ok=True)
    comparison.write_text("{}", encoding="utf-8")
    retained_report = comparison.parent / "calibration_report.json"
    retained_report.write_text("{}", encoding="utf-8")
    retained_log = tmp_path / "logs" / "feature.log"
    retained_log.parent.mkdir()
    retained_log.write_text("current", encoding="utf-8")

    _clear_previous_generated_models(tmp_path)

    assert all(not directory.exists() for directory in generated)
    assert not comparison.exists()
    assert retained_report.is_file()
    assert retained_log.is_file()
