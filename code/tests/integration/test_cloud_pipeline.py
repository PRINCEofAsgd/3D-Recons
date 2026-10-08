"""Phase 4 本地阶段命令验收：跨 scratch 的真实合成双消费者与状态。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from store_vision.cloud_pipeline import calibrate, run_branch, run_for, snapshot_for, summarize
from store_vision.cloud_workspace import LocalObjectStore, create_dataset_snapshot
from tests.unit.test_cloud_workspace import _gui_synthetic
from tests.unit.test_workspace import _shared_report


class LocalS3:
    """测试适配器只替换存储传输，保持清单 URI 和不可覆盖语义。"""

    def __init__(self, root: Path):
        self.local = LocalObjectStore(root)
        self.root = root
        self.scope = "unused"

    def put(self, data, media_type):
        return self.local.put(data, media_type)

    def read(self, resource):
        return self.local.read(resource)

    def manifest_uri(self, kind, *identity):
        return "s3://synthetic/phase4/manifests/" + "/".join((kind, *identity, "manifest.json"))

    def _path(self, uri):
        prefix = "s3://synthetic/phase4/"
        if not uri.startswith(prefix) or ".." in uri:
            raise ValueError("对象 URI 越界")
        return self.root / uri[len(prefix):]

    def publish(self, kind, identity, manifest):
        uri = self.manifest_uri(kind, *identity)
        path = self._path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        return uri

    def load_manifest(self, uri):
        try:
            return json.loads(self._path(uri).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("对象不存在") from exc


def _prepared(fixture_dir, tmp_path):
    source = _gui_synthetic(fixture_dir, tmp_path / "source")
    store = LocalS3(tmp_path / "store")
    create_dataset_snapshot(store, source, "synthetic", "snapshot1")
    snapshot_uri = store.manifest_uri("snapshots", "synthetic", "snapshot1")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_shared_report(source), ensure_ascii=False), encoding="utf-8")
    result = calibrate(store, "synthetic", "run1", "attempt1", "estimated",
                       snapshot_uri, tmp_path / "calibration", report)
    return store, snapshot_uri, result


def test_two_branches_and_replayed_calibration(fixture_dir, tmp_path):
    """三个独立 scratch 产生两种真实业务输出，并安全复用已发布上游。"""
    store, snapshot_uri, run = _prepared(fixture_dir, tmp_path)
    reused = calibrate(store, "synthetic", "run1", "attempt1", "estimated",
                       snapshot_uri, tmp_path / "not-materialized")
    assert reused["reused"] == "true"
    assert reused["run_sha256"] == run["run_sha256"]
    for branch in ("map25d", "stitching"):
        result = run_branch(store, branch, "synthetic", "run1", "attempt1", "estimated",
            snapshot_uri, run["run_manifest_uri"], run["run_sha256"],
            tmp_path / branch, "synthetic-small")
        assert result["status"] == "succeeded"
        assert branch in result["artifact_uri"]
    summary = summarize(store, "synthetic", "run1", "attempt1", "estimated",
        snapshot_uri, run["run_manifest_uri"], run["run_sha256"],
        "Succeeded", "Succeeded", "synthetic-small")
    assert summary["status"] == "succeeded"
    assert set(summary["artifacts"]) == {"map25d", "stitching"}
    with pytest.raises(ValueError, match="参数版本"):
        run_branch(store, "stitching", "synthetic", "run1", "attempt1", "estimated",
            snapshot_uri, run["run_manifest_uri"], run["run_sha256"], tmp_path / "retry",
            "default")


def test_failed_map_keeps_stitch_and_rejects_wrong_identity(fixture_dir, tmp_path):
    """2.5D 失败不抹去拼接；错误候选、run 哈希和 dataset 在计算前被拒绝。"""
    store, snapshot_uri, run = _prepared(fixture_dir, tmp_path)
    with pytest.raises(ValueError, match="候选"):
        run_for(store, snapshot_for(store, "synthetic", snapshot_uri), "run1", "attempt1",
                "other", run["run_manifest_uri"], run["run_sha256"])
    with pytest.raises(ValueError, match="哈希"):
        run_for(store, snapshot_for(store, "synthetic", snapshot_uri), "run1", "attempt1",
                "estimated", run["run_manifest_uri"], "0" * 64)
    with pytest.raises(ValueError, match="身份"):
        snapshot_for(store, "other", snapshot_uri)
    run_branch(store, "stitching", "synthetic", "run1", "attempt1", "estimated",
        snapshot_uri, run["run_manifest_uri"], run["run_sha256"],
        tmp_path / "stitching", "synthetic-small")
    summary = summarize(store, "synthetic", "run1", "attempt1", "estimated",
        snapshot_uri, run["run_manifest_uri"], run["run_sha256"],
        "Failed", "Succeeded", "synthetic-small")
    assert summary["status"] == "failed"
    assert summary["stages"]["map25d"]["status"] == "failed"
    assert summary["stages"]["stitching"]["status"] == "succeeded"
    assert set(summary["artifacts"]) == {"stitching"}
