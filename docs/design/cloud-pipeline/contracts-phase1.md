# Phase 1 本地合同与验收

## 边界与调用顺序

```text
cali/scale/floorplan/screenshots（只读）
→ load_gui_dataset_folder 输入预检
→ create_dataset_snapshot（云端合同 v1，内容寻址）
→ materialize_snapshot（独立 scratch）
→ run_shared_intrinsics_calibration（共享 K/D、逐机 R/t、联合 BA；本阶段样例使用显式合成报告）
→ publish_intermediate_package（现有 schema v2；本地绝对路径）
→ stage_intermediate（云端 run v1；局部引用）
→ materialize_run → load_intermediate_runtime
├── run_map25d_from_intermediate（能力 map25d_ready）
└── run_parameter_stitching（能力 stitching_ready）
→ stage_artifact（两个独立 artifact）
```

可选 `calibration-demo`/COLMAP/SfM 只提供输入侧证据，失败不改变父中间层。`pipeline.py` 是原 headless 兼容入口，不属于新合同控制面。Phase 1 未引入远程存储、控制面或分布式调度。

## 合同

本地存储的对象 URI 为 `svlocal://objects/<sha256>`；URI 的含义由存储根目录解释，不含主机路径。`LocalObjectStore` 仅提供字节写入、读取、校验和清单发布；后续存储实现可替换这一接口。清单位于 `manifests/{snapshots,runs,artifacts}/.../manifest.json`，其父目录按 dataset、snapshot 或 run/attempt/artifact 身份分隔。对象按 SHA-256 寻址且只写一次；所有对象校验完毕才创建清单。清单一经创建不可覆盖。

以下是字段形状示意，合成 ID 与 URI 只用于解释，不是可直接加载的样例文件；可加载清单由下方测试生成。

```json
{
  "schema_version": "store-vision-cloud/1",
  "kind": "dataset_snapshot",
  "dataset_id": "synthetic",
  "snapshot_id": "snapshot1",
  "producer": "store_vision.cloud_workspace",
  "algorithm_version": "1.0.9",
  "parameters_digest": "<sha256>",
  "created_at": "<UTC ISO-8601>",
  "parents": [],
  "camera_ids": ["CAMERA_ID_1", "CAMERA_ID_2"],
  "files": [{"role": "image", "camera_id": "CAMERA_ID_1", "path": "screenshots/CAMERA_ID_1.jpg", "uri": "svlocal://objects/<sha256>", "bytes": 123, "sha256": "<sha256>", "media_type": "image/jpeg"}]
}
```

`run` 另含 `run_id`、`attempt_id`、`snapshot_id`、`candidate`、`camera_ids`、`capabilities`、`coordinate_contract`、`resources` 和指向 snapshot 的 `parents`。`artifact` 另含 `artifact_id`、`workflow`、`candidate`、`coordinate_contract`、`files` 和指向 run/attempt 的 `parents`。每一资源行均保存对象 URI、字节数、SHA-256、媒体类型和相对路径。参数摘要、生产者、算法版本与 UTC 创建时间在三种清单中一致使用。

`producer` 是已发布清单的来源记录。模块重命名后新清单写入 `store_vision.cloud_workspace`；旧清单中的 `store_vision.cloud_phase1` 仍可读取，合同校验不以当前 Python 模块路径作为身份门禁。

原 schema v2 不修改。导出 run 时仅从 v2 manifest 的正式键枚举 `manifest.json`、候选 rig/H、观测、平面框架与高度策略等资源。资源 JSON 内指向输入或中间层的绝对路径转为 `svref://input/...` 或 `svref://intermediate/...`；恢复时才重建 scratch 内绝对路径供原消费者使用。未知的机器绝对路径直接拒绝。输入快照记录相机 ID；run 记录所选候选和两种业务能力。世界地面坐标系与单位从 v2 地面合同读取，合成样例是 `store_ground_world`、米；平面图样例是 `floorplan_pixel`、像素。K/D/R/t/H 留在被校验的 v2 候选资源中。原 v2 的 `dataset_id`、`schema_version`、`calibration_candidates`、`active_calibration`、`planar_projection_candidates`、`frames`、`capabilities`、`input` 和 `lineage` 继续由原业务入口解释。v2 中的输入与图像绝对路径绑定一台机器，因此不能直接拷入另一个容器。

## 验收命令与输出

在 `code/` 使用已安装开发依赖的 Python 环境：

```bash
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=../outputs/matplotlib \
  python -m pytest -q tests/unit/test_cloud_workspace.py \
  --basetemp ../outputs/phase1-pytest
```

测试仅复制 `tests/fixtures/sample_store/` 的去标识化合成图片，并生成合成 scale。可查看 `../outputs/phase1-pytest/test_snapshot_and_run_restore_0/store/manifests/` 下的三类清单、`first/` 与 `second/` 两套 scratch，以及 `second/map25d/`、`second/stitching/`。第二套 scratch 的两个消费者实际运行；另一个测试直接调用统一适配入口。预期 4 项通过；若合同坏路径、哈希、schema、dataset/run 身份、重复相机 ID、候选或缺资源，抛出 `ValueError`，无对应清单发布。错误属于确定性输入/合同失败，修正源对象或清单后重新建立新身份，不自动重试。`--basetemp` 会清空其目标目录，请只用于专门的忽略目录。

原项目回归：

```bash
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=../outputs/matplotlib python -m pytest -q
python -m compileall -q store_vision tests
python -m store_vision --help
```

本阶段测试的共享报告由合成 K/D/R/t 构造，只验证合同及两个原消费者的贯通，不代表真实共享鱼眼标定或联合 BA 通过。完整标定需要合规输入及可选计算依赖；COLMAP/SfM、Windows 和 Intel Mac 未在本阶段验证。
