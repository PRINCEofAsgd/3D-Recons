# COLMAP 标定优化

## 目标与边界

该链路使用 COLMAP 图像约束校验和补充已有人工标定，不运行稠密重建。OpenCV 继续负责输入优化、平面投影、检测与拼接；COLMAP 只负责可选特征提取、匹配、稀疏重建和文本模型导出。

- `cali.txt/cali.json` 提供图像像素与平面图的四点关系，不能替代真实 K/D/R/t。
- SfM 模型只有通过逐相机三维观测、轨迹和重投影门禁后才可发布。
- SfM 任意尺度坐标必须通过真实控制点完成业务坐标对齐。
- 工作流不会修改输入图片、人工标定或已有数据库。

## 输入

- 数据集中的 `cali.txt/cali.json` 和相机图片。
- 鱼眼联合标定运行目录或报告。
- 可选真实内参配置与控制点。

## 输出

- `database.db`、`sparse/`、`model_txt/`：本轮数据库和通过门禁的模型。
- `sparse_candidates/`、`model_candidates/`：未发布候选。
- `reports/match_statistics.*`、`camera_match_graph.png`、`matches/`：匹配统计与可视化。
- `reports/reconstruction_summary.json`、`sfm_intrinsics.json`、`sfm_registration.json`：参数、注册与质量门禁。
- `reports/pair_diagnostics/`、`initial_pair_ranking.*`、`current_model_geometry.json`：只读几何诊断。
- `reports/calibration_report.json`：工作流汇总。
- `logs/`：每条外部命令、输出和退出码。

不会生成伪造数据库、占位结果或演示数值。`--dry-run` 只规划命令，不写报告。

## 用法

```bash
store-vision calibration-demo \
  --dataset DATASET \
  --output OUTPUT \
  --fisheye-calibration FISHEYE_RUN \
  --dry-run

store-vision calibration-demo \
  --dataset DATASET \
  --output OUTPUT \
  --skip-colmap

store-vision geometry-diagnostics \
  --database-path RUN/database.db \
  --image-path DATASET/screenshots \
  --model-path RUN/model_txt \
  --output-path RUN/reports
```

真实运行使用 `OPENCV_FISHEYE`、固定随机种子和独立运行目录。同分辨率图片共用 camera；异分辨率按比例换算 K 并共享无量纲 D。拟合参数先经过主点、焦距、视场覆盖和投影可逆性门禁；Mapper、补三角化和 BA 冻结活动内参。

## 降级策略

未安装 COLMAP、命令失败或模型不完整时，状态写为 `unavailable` 或 `failed`，并保留已加载的人工标定与可用诊断。图像有位姿但没有三维点时标记为 `registered_without_geometry`，不会发布为成功模型。

新实验使用唯一 `run_<时间戳>`，分析使用唯一 `analysis_<时间戳>`，不覆盖数据库、旧报告或输入。
