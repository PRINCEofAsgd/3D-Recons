# 2.5D 输出数据包

```text
map25d_output/<dataset>/run_<time>/
├── manifest.json
├── map25d.geojson
├── map25d_candidates.geojson
├── map25d_preview.png
├── review/object_selection.json
├── detection/
├── overlap/
└── alignment/
```

本业务读取一个中间层候选，完成桌面检测、桌高平面投影、跨相机去重、一致性分析和 GeoJSON 构建。XY 来自平面映射；Z 与 `height_cm` 来自高度策略，不是稠密点云测量值。

`manifest.json` 记录源中间层、候选、模式、能力快照、对象数和一致性摘要。本目录不得写回中间层或拼接输出。
