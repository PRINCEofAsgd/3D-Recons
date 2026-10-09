# 仓库结构与调用链

本文档记录正式交付目录、运行产物、主要模块调用链和版号位置。安装与测试见[开发指南](../user/development.md)，界面操作见[操作手册](../user/user.md)。

## 文档索引

- [工作要求](../../AGENTS.md)
- [项目说明](../../README.md)
- [当前上下文](project-context.md)
- [待办事项](todo-list.md)
- [开发指南](../user/development.md)
- [操作手册](../user/user.md)
- [历史阶段摘要](../record/project-demo-initialization.md)
- [需求评审](../design/algorithm/01_requirements_review.md)
- [算法设计](../design/algorithm/02_algorithm_design.md)
- [测试计划](../design/algorithm/03_test_plan.md)
- [COLMAP 标定说明](../design/algorithm/calibration_demo.md)
- [云端 Phase 1 合同与验收](../design/cloud-pipeline/contracts-phase1.md)
- [云端 Phase 2 一致性与状态合同](../design/cloud-pipeline/contracts-phase2.md)
- [云端 Phase 3 Ray 分片合同](../design/cloud-pipeline/contracts-phase3.md)
- [云端 Phase 4 Argo DAG 与双分支合同](../design/cloud-pipeline/contracts-phase4.md)

## 顶层目录

```text
3D-Recons/
├── AGENTS.md
├── README.md
├── .gitignore
├── .dockerignore                   # 云端镜像构建上下文排除规则
├── code/
│   ├── store_vision/
│   │   └── examples/               # 可随包分发的配置示例
│   ├── tests/
│   ├── main.py
│   └── pyproject.toml
├── cloud/
│   ├── main.go                     # Go API、默认 worker 与进程模式入口
│   ├── ⭐️ argo_sync.go             # Argo Workflow 提交等 K8s API
│   ├── compose.yaml                # 本地 MySQL 与 MinIO
│   ├── .env.example                # 本地控制面与 Compose 环境变量示例
│   ├── Dockerfile                  # Python 云端阶段镜像
│   ├── requirements-runtime.txt    # 已验收云端运行依赖版本
│   ├── migrations/
│   │   ├── 001_phase2.sql      # Dataset、Job、Stage、Artifact 基础表
│   │   └── 002_phase4.sql      # Argo 后端与阶段失败明细
│   └── argo/
│       ├── workflow-template.yaml  # 快照、标定、双分支与汇总 DAG
│       ├── service-account.yaml    # Workflow executor 最小权限
│       ├── ray-local.yaml          # 仅供本机合成验收的 Ray 服务
│       └── runtime.env.example     # Pod Secret 键名与资源配置示例
├── data/
│   ├── input/
│   ├── intermediate/
│   ├── map25d_output/
│   └── stitching_output/
├── archive/                       # 可跟踪的历史数据，不得被正式代码依赖
└── docs/
    ├── context/
    ├── design/                     # 专项设计与标定说明
    ├── user/
    └── record/
```

`code/` 是唯一 Python 项目根目录，`store_vision/` 是可安装包，`tests/` 与包并列。配置示例位于包内 `examples/`，可随安装包分发；`docs/design/algorithm/` 保存四份算法专项文档，`docs/design/cloud-pipeline/` 保存云端阶段设计与合同。`data/` 的真实输入和运行产物默认被 Git 忽略，只跟踪结构说明与 `apple_exam` 示例；`archive/` 可跟踪但正式代码和测试不得依赖。

## cloud/ 控制面与部署资源

`main.go` 是同一个 Go 程序的 `api`、`worker` 和 `argo-sync` 模式入口：API 注册 Dataset、提交和查询 Job，默认 worker 领取 MySQL Job 并调用 `python -m store_vision.cloud_job_runner`；Argo 模式由 `argo_sync.go` 创建或恢复 Workflow，将节点状态、汇总清单和 Artifact 条件写回 MySQL。`go.mod`、`go.sum` 固定 Go 模块依赖。`main_test.go`、`main_integration_test.go` 和 `argo_sync_test.go` 分别覆盖控制面逻辑、真实 MySQL 并发约束及 Argo 同步状态。

`compose.yaml` 仅提供本机 MySQL、MinIO 及其 Docker 卷，`cloud/.env.example` 是本机连接和凭据键名示例；实际 `cloud/.env` 不跟踪。`migrations/001_phase2.sql` 建立基础业务表，`002_phase4.sql` 在其上增加 Argo 后端字段与 `blocked` 阶段状态，旧数据库按编号应用增量迁移。数据库内容和对象文件保存在本地卷，对应的 Python scratch、kind 二进制、运行环境文件与临时输出保存在被忽略的 `cloud-local/`，均不是仓库源码。

`Dockerfile` 从 `code/` 构建无桌面 Qt 的 Python 阶段镜像，先安装 `requirements-runtime.txt` 固定的运行依赖。`argo/workflow-template.yaml` 让各 Pod 调用 `code/store_vision/cloud_pipeline.py`，通过已发布清单 URI 串联阶段；`service-account.yaml` 提供 executor 的最小回报权限。`argo/runtime.env.example` 只列 Pod Secret 所需键名，实际 Secret 在本地 Kubernetes 中创建。`ray-local.yaml` 仅提供本机合成验收的 Ray head 与 Service；Argo 控制器和正式 Ray 集群由部署环境提供。部署与启动命令见[开发指南](../user/development.md#phase-4-argo-dag-与双分支)。

## 源码模块

```text
store_vision/
├── __init__.py                    # 包版号
├── __main__.py                    # python -m 入口
├── ⭐️ cloud_workspace.py          # 计算执行流程化，包含数据规则与算法调用
├── ⭐️ s3_object_store.py          # S3 对象存储数据面实现
├── ⭐️ cloud_job_runner.py         # 基于对象存储的计算执行流程化
├── ⭐️ ray_stitching.py            # Ray 分布式计算框架下的逐相机并发计算
├── ⭐️ cloud_pipeline.py           # K8s 编排调度 Worker 的 Argo 工作流
├── cli.py                         # CLI 与 GUI/headless 分派
├── config.py                      # 阈值和数据根配置
├── pipeline.py                    # 兼容 headless 流水线
├── resolution.py                  # 异分辨率坐标转换与输入预检
├── assets/                        # 桌面图标
├── examples/                      # 相机内参配置示例
├── data/
│   ├── models.py                  # 数据模型
│   ├── loader.py                  # 标定、图片与平面图加载
│   └── workspace.py               # 中间层发布、发现、能力解析与恢复
├── calibration/
│   ├── distortion.py              # 去畸变与旧格式兼容
│   ├── homography.py              # 图像与平面图单应
│   ├── bundle.py                  # 兼容位姿近似
│   ├── fisheye_bundle.py          # 共享 K/D、逐机位姿和联合 BA
│   ├── intrinsics_validation.py   # K/D 物理与数值门禁
│   ├── shared_intrinsics.py       # 共享内参约束与 H 位姿分解
│   ├── shared_calibration.py      # 输入优化编排、报告和可视化
│   ├── scale_metadata.py          # 尺度、边界和设备身份解析
│   ├── manual.py                  # 人工标定适配
│   ├── models.py                  # 标定 Demo 数据结构
│   ├── colmap_runner.py           # COLMAP 参数与子进程调用
│   ├── colmap_fisheye.py          # 鱼眼参数和 camera 分组写入
│   ├── pose_seed.py               # 位姿与数据库种子
│   ├── sfm_registration.py        # SfM 候选编排与发布
│   ├── sfm_quality.py             # 三维观测、轨迹和误差验收
│   ├── colmap_database.py         # SQLite 匹配统计
│   ├── pair_geometry.py           # 图像对覆盖、H/F 与排名
│   ├── geometry_visualization.py  # 匹配与位移诊断图
│   ├── geometry_diagnostics.py    # 只读几何诊断编排
│   ├── model_geometry.py          # 模型轨迹、深度与基线
│   ├── intrinsics_config.py       # 外部内参配置校验
│   ├── colmap_parser.py           # COLMAP 文本模型解析
│   ├── alignment.py               # 模型到业务平面的相似变换
│   ├── evaluation.py              # 位置、朝向与投影误差
│   ├── colmap_refine.py           # 可选 pycolmap 步骤
│   └── workflow.py                # calibration-demo 主编排
├── detection/
│   ├── overlay_filter.py          # 叠加线检测与修复
│   ├── white_table.py             # 单相机桌面候选
│   └── table_locator.py           # 多相机汇总与去重
├── geometry/
│   ├── coords.py                  # 百分比、像素与厘米坐标
│   └── projection.py              # 投影、多边形与相机选择
├── mapping/
│   ├── overlap.py                 # 相机覆盖交集
│   ├── map25d.py                  # GeoJSON 构建与读取
│   ├── map25d_preview.py          # 2.5D 预览
│   ├── map25d_review.py           # 人工选择与派生结果
│   ├── map25d_pipeline.py         # 中间层驱动 2.5D 流水线
│   ├── parameter_stitcher.py      # K/D/R/t 地面拼接
│   ├── sfm_assisted.py            # SfM 相机证据辅助 2.5D
│   ├── alignment_report.py        # 跨相机一致性
│   └── stitcher.py                # 兼容俯视融合
├── reporting/
│   └── calibration_report.py      # 标定报告
└── ui/
    ├── main_window.py             # 主窗口与任务分发
    ├── app_identity.py            # 桌面应用身份
    ├── app_icon.py                # 图标加载
    ├── calibration_workspace.py   # 标定结果归一化
    ├── qt_utils.py                # Qt 通用辅助
    ├── tabs/                       # 三个一级页面及子页面
    └── widgets/                    # 共用界面组件
```

## 测试结构

```text
tests/
├── conftest.py
├── generate_fixtures.py
├── fixtures/sample_store/         # 去标识化合成输入
├── unit/
└── integration/
```

pytest 只从 `tests` 收集，不读取 `data/` 或 `archive/`。

`tests/unit/test_cloud_workspace.py` 用合成夹具验证快照、跨目录恢复、两个原业务消费者和合同拒绝条件。
`tests/unit/test_s3_object_store.py` 用合成字节验证 S3 故障边界；`tests/integration/phase2_sample.py` 生成本地验收输入。`cloud/main_test.go` 验证控制面分支，`cloud/main_integration_test.go` 可对真实 MySQL 唯一约束执行并发提交实验。
`tests/integration/test_ray_stitching.py` 用合成图片验证串行/Ray 一致性、两个 worker 进程、逐机失败重试、分片校验与不覆盖。
`tests/integration/test_cloud_pipeline.py` 与 `tests/unit/test_cloud_pipeline_gate.py` 验证 Phase 4 跨 scratch 双业务输出、候选/哈希拒绝、阶段复用、分支失败及能力 blocked；`cloud/argo_sync_test.go` 验证提交参数、重复回报和部分结果查询。

## 数据合同与运行产物

```text
data/input/<dataset>/
├── cali.txt 或 cali.json
├── scale.txt 或 scale.json
├── floorplan.png/.jpg/.jpeg
└── screenshots/

data/intermediate/<dataset>/run_<time>/
├── manifest.json
├── calibrations/<candidate>/camera_rig.json
├── planar_projections/<candidate>.json
├── observations/camera_plan_controls.json
├── frames/
├── business/height_policy.json
└── evidence/sfm.json               # 可选

data/map25d_output/<dataset>/run_<time>/
├── manifest.json
├── map25d.geojson
├── map25d_candidates.geojson
├── map25d_preview.png
├── review/
├── detection/
├── overlap/
└── alignment/

data/stitching_output/<dataset>/run_<time>/
├── manifest.json
├── mosaic_fused.jpg
├── mosaic_alpha.jpg
├── coverage_visualization.png
├── overlap_visualization.png
└── cameras/
```

各层 `run_*` 防覆盖。数据包默认只保存在本地；旧运行可移入 `archive/` 或删除。

Phase 1 本地云端合同另存于被忽略的 `outputs/` 或 `cloud-local/`：`objects/<hash-prefix>/<sha256>` 为内容寻址对象，`manifests/snapshots/<dataset>/<snapshot>/manifest.json`、`manifests/runs/<dataset>/<run>/<attempt>/manifest.json` 和 `manifests/artifacts/<dataset>/<run>/<attempt>/<artifact>/manifest.json` 为发布清单。scratch 内恢复的 v2 中间层及业务结果按 run/attempt 隔离。详见[Phase 1 合同](../design/cloud-pipeline/contracts-phase1.md)。

Phase 2 远程对象键按配置前缀分为输入快照、attempt 对象、run/artifact 清单与已发布指针。`cloud/` 的 Go API 把 Dataset/Job/Stage/Artifact 元数据写入 MySQL，独立 worker 调用 Python 原算法；本地 scratch 位于被忽略的 `cloud-local/`。详见[Phase 2 合同](../design/cloud-pipeline/contracts-phase2.md)。

Phase 3 在 `attempts/<job>/<run>/<candidate>/<canvas_hash>/<camera>/<task_version>/<part_attempt>/` 下保存逐机 `npz` 与清单。Ray task 从对象存储按需读取本相机图片；驱动端校验全部必需分片后按稳定顺序复用原覆盖/融合逻辑。业务 manifest 增加 `parallel_parts` 来源记录；原串行格式继续可用。详见[Phase 3 合同](../design/cloud-pipeline/contracts-phase3.md)。

Phase 4 的 `cloud/argo/workflow-template.yaml` 在 Kubernetes 中串联快照校验、全局中间层、两个业务分支与汇总；资源参数经 `podSpecPatch` 注入。`cloud/argo/service-account.yaml` 提供 executor 的最小回报权限，`cloud/argo/ray-local.yaml` 是仅供本机合成验收的 Ray head/Service。`cloud/requirements-runtime.txt` 固定云端镜像运行依赖，`cloud/Dockerfile` 在源码层之前安装依赖。`cloud_pipeline.py` 每节点从 S3 manifest 恢复所需资源；`cloud/argo_sync.go` 每 5 秒同步 Workflow 节点与终态到 MySQL。两个分支独立写 `manifests/artifacts/<dataset>/<run>/<attempt>/<branch>/manifest.json`；汇总写 `manifests/published/<dataset>/<run>/<attempt>/manifest.json`。详见[Phase 4 合同](../design/cloud-pipeline/contracts-phase4.md)。

## 程序入口

```text
store-vision / python -m store_vision
→ cli.main
├── 默认：PyQt6 桌面端
├── --headless：兼容完整流水线
├── calibration-demo：COLMAP/SfM 标定与报告
├── geometry-diagnostics：已有数据库和模型的只读诊断
├── sfm-assisted-map25d：SfM 相机证据辅助 2.5D
├── import-external-calibration：外部参数规范化导入
├── fisheye-calibration：共享鱼眼联合标定
└── shared-intrinsics-calibration：兼容别名
```

`main.py` 与 `__main__.py` 只转发到 `cli.main`。

`cloud_workspace` 为独立 Python API，不改 GUI/CLI 路由：输入预检 → 输入快照 → 本地物化 → 原共享标定结果发布 v2 → run 合同 → 跨目录恢复 v2 → 两个原消费者 → artifact 合同。已有清单的 `producer` 字段作为来源文本保留，读取时按合同字段校验，不要求等于当前模块名。`pipeline.py` 仍为兼容 headless 路径。

Phase 2 默认 worker 调用链：`cloud_job_runner snapshot` → S3 快照；Go `/datasets`、`/jobs` → MySQL；独立 Go worker → `cloud_job_runner execute` → S3 attempt、清单与指针 → MySQL 终态 → `/jobs/<id>/artifacts`。Phase 4 的 `backend=argo` 走下述独立 DAG。

Phase 3 可选调用链：`cloud_job_runner execute` → `prepare_stitching_runtime` 确定共同画布 → `ray_stitching.run_ray_stitching` 限流派发逐机 task → 分片清单与数组全量校验 → `run_parameter_stitching` 按相机顺序全局 reduce → 原 Phase 2 artifact/指针发布。无 `SV_RAY_ADDRESS` 时直接运行同一逐机函数的串行路径。

Phase 4 调用链：Go `POST /jobs` → MySQL Argo Job → `argo_sync.go` 创建 Workflow → `cloud_pipeline verify/calibrate` → 已发布 run URI/哈希 → `map25d` 与 `stitching` 容器阶段（后者要求 Ray 地址）→ `summarize` 校验并发布独立结果/汇总 → Go 同步器条件提交 Job/Stage/Artifact → 查询 API。

## GUI 调用链

```text
cli.main
→ QApplication → MainWindow
├── 输入优化 / InputTab
│   ├── load_gui_dataset_folder → 输入配对、分辨率转换和预检
│   ├── run_shared_intrinsics_calibration
│   └── publish_intermediate_package → manifest + 能力快照
├── IntermediateSourceSelector
│   └── discover_intermediate_packages → 选择候选并解析能力
├── 2.5D 建图 / Map25DWorkflowTab
│   └── run_map25d_from_intermediate
│       ├── load_intermediate_runtime
│       ├── detect_tables_with_review + compute_overlaps
│       ├── 桌高平面投影 + H 平面锚定
│       └── GeoJSON、预览、人工复核与一致性报告
└── 图像拼接 / StitchingWorkflowTab
    └── run_parameter_stitching
        ├── 按相机模型去畸变
        ├── K·[r1 r2 t] 地面投影
        └── 逐机映射、覆盖/重叠、羽化融合与 manifest
```

后台任务只携带启动时的中间层路径、候选和模式；输入切换不会覆盖业务页的自由选择，旧任务结果不会载入新的来源。

## 标定与 SfM 调用链

```text
fisheye-calibration
→ 读取 cali/scale/图片并完成输入预检
→ 构建逐机 H 与统一参考像素系
→ 共享 K/D、逐机 R/t 拟合初始化
→ 联合 BA 与物理门禁
→ 报告、可视化和中间层候选

calibration-demo
→ 校验输入优化报告身份与活动 K/D
→ COLMAP 特征提取、匹配和候选 Mapper
→ 逐相机三维观测、轨迹和误差门禁
→ 发布最高分合法模型与报告
→ 可选派生带 SfM 证据的新中间层
```

SfM 失败不会修改原图、人工标定或父中间层，也不会自动触发业务输出。

## 版号位置

当前包版号为 `1.0.13`，实验展示版号为 `V1.0.13_20261010`。修改时同步检查：

- `code/pyproject.toml`
- `code/store_vision/__init__.py`
- `code/store_vision/calibration/shared_calibration.py`
- `cloud/argo/workflow-template.yaml` 与 `cloud/argo/ray-local.yaml` 的镜像 tag
- `cloud/argo_sync_test.go` 的镜像参数夹具及 `docs/user/development.md` 的构建命令
- `docs/context/project-context.md`
