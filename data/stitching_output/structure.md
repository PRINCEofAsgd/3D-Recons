# 图像拼接输出数据包

```text
stitching_output/<dataset>/run_<time>/
├── manifest.json
├── mosaic_fused.jpg
├── mosaic_alpha.jpg
├── coverage_visualization.png
├── overlap_visualization.png
├── overlap_mask.png
├── overlap_count.png
└── cameras/
    ├── <camera_id>_warped.jpg
    └── <camera_id>_mask.png
```

本业务读取中间层候选的原图、K/D/R/t、共同世界坐标合同和目标地面，生成逐机映射、覆盖/重叠诊断、透明叠加和羽化融合。

`manifest.json` 记录候选、坐标约定、画布、裁剪、配置与逐机统计。未验证坐标合同只能生成 `ready_provisional` 的 `geometric_trial`。
