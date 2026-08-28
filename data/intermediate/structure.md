# 中间层数据包

```text
intermediate/<dataset>/run_<time>/
├── manifest.json
├── calibrations/<candidate>/camera_rig.json
├── planar_projections/<candidate>.json
├── observations/camera_plan_controls.json
├── frames/ground_plane.json
├── frames/plan_registration.json
├── business/height_policy.json
├── evidence/sfm.json               # 可选
└── shared_calibration_report.*     # 输入优化诊断
```

`manifest.json` 是唯一稳定入口，记录生产者、父包、数据集身份、资源索引、坐标合同、候选可信等级和能力快照。消费者不得依赖任意文件名扫描。

2.5D 需要 K/D/H、平面图、尺度和高度策略；拼接需要原图、K/D/R/t、共同世界系、单位和目标地面。能力状态为 `ready_verified`、`ready_provisional` 或 `blocked`。
