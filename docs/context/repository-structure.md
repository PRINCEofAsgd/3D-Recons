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
- [macOS 版本说明](../user/macos-release-notes.md)
- [Windows 版本说明](../user/windows-release-notes.md)
- [移动平台版本说明](../user/mobile-release-notes.md)
- [COLMAP 标定说明](../../Code/StoreVision/docs/calibration_demo.md)

## 顶层目录

```text
3DReconstruction/
├── AGENTS.md
├── README.md
├── .gitignore
├── Code/StoreVision/
│   ├── src/store_vision/
│   ├── tests/
│   ├── docs/
│   ├── main.py
│   └── pyproject.toml
├── data/
│   ├── input/
│   ├── intermediate/
│   ├── map25d_output/
│   └── stitching_output/
├── archive/                       # 可跟踪的历史数据，不得被正式代码依赖
└── docs/
    ├── context/
    ├── user/
    └── record/
```

正式业务代码只有 `Code/StoreVision/src/store_vision`，正式测试只有 `Code/StoreVision/tests`。`data/` 与 `archive/` 默认可跟踪；`data/` 只保存已授权的当前输入和最新口径产物，`archive/` 只保存不参与运行的历史数据。

## 源码模块

```text
src/store_vision/
├── __init__.py                    # 包版号
├── __main__.py                    # python -m 入口
├── cli.py                         # CLI 与 GUI/headless 分派
├── config.py                      # 阈值和数据根配置
├── pipeline.py                    # 兼容 headless 流水线
├── resolution.py                  # 异分辨率坐标转换与输入预检
├── assets/                        # 桌面图标
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

各层 `run_*` 防覆盖。提交时同一数据集和输出层只保留当前版本需要的最新运行；旧运行移入 `archive/` 或删除。

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

当前代码版号为 `1.0.2`，平台展示版号为 `V1.0.2_20260824`。修改时同步检查：

- `Code/StoreVision/pyproject.toml`
- `Code/StoreVision/src/store_vision/__init__.py`
- `Code/StoreVision/src/store_vision/calibration/shared_calibration.py`
- `docs/user/*-release-notes.md`
- `docs/context/project-context.md`
