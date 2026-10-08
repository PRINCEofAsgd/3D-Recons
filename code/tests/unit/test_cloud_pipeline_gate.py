"""Phase 4 能力门禁和最终 Job 准则的独立业务测试。"""

from __future__ import annotations

from store_vision import cloud_pipeline


class PublishedStore:
    """仅模拟已发布清单目录；算法和对象校验由合成集成测试覆盖。"""

    def __init__(self):
        self.published = {}

    def manifest_uri(self, kind, *identity):
        return f"s3://synthetic/phase4/manifests/{kind}/{'/'.join(identity)}/manifest.json"

    def load_manifest(self, uri):
        if uri not in self.published:
            raise ValueError("对象不存在")
        return self.published[uri]

    def publish(self, kind, identity, manifest):
        uri = self.manifest_uri(kind, *identity)
        self.published[uri] = manifest
        return uri


def test_blocked_map_is_not_success_but_optional_when_stitch_succeeds(monkeypatch, tmp_path):
    """能力缺失时 2.5D 不运行、不发布，拼接成功可让 Job 结算成功。"""
    store = PublishedStore()
    run = {"capabilities": {
        "map25d_ready": {"ready": False, "missing": ["height_policy"]},
        "stitching_ready": {"ready": True, "missing": []}}}
    monkeypatch.setattr(cloud_pipeline, "snapshot_for", lambda *_: {"dataset_id": "synthetic"})
    monkeypatch.setattr(cloud_pipeline, "run_for", lambda *_: run)
    monkeypatch.setattr(cloud_pipeline, "validate_artifact", lambda *_: {})
    monkeypatch.setattr(cloud_pipeline, "artifact_parameters_match", lambda *_: True)
    stitch_uri = store.manifest_uri("artifacts", "synthetic", "run1", "attempt1", "stitching")
    store.published[stitch_uri] = {"artifact_id": "stitching"}
    blocked = cloud_pipeline.run_branch(store, "map25d", "synthetic", "run1", "attempt1",
        "estimated", "snapshot", "run", "hash", tmp_path)
    assert blocked == {"status": "blocked", "reason": "height_policy"}
    summary = cloud_pipeline.summarize(store, "synthetic", "run1", "attempt1",
        "estimated", "snapshot", "run", "hash", "Succeeded", "Succeeded")
    assert summary["status"] == "succeeded"
    assert summary["stages"]["map25d"] == blocked
    assert summary["artifacts"] == {"stitching": stitch_uri}


def test_stitching_blocked_fails_job(monkeypatch):
    """必需拼接缺能力时不能把未运行分支记为成功。"""
    store = PublishedStore()
    run = {"capabilities": {
        "map25d_ready": {"ready": False, "missing": ["height_policy"]},
        "stitching_ready": {"ready": False, "missing": ["camera_rig"]}}}
    monkeypatch.setattr(cloud_pipeline, "snapshot_for", lambda *_: {"dataset_id": "synthetic"})
    monkeypatch.setattr(cloud_pipeline, "run_for", lambda *_: run)
    summary = cloud_pipeline.summarize(store, "synthetic", "run1", "attempt1",
        "estimated", "snapshot", "run", "hash", "Succeeded", "Succeeded")
    assert summary["status"] == "failed"
    assert summary["stages"]["stitching"] == {"status": "blocked", "reason": "camera_rig"}
    assert summary["artifacts"] == {}
