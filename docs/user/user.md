# Store Vision 操作手册

## Phase 1 合成合同验收

开发者可按[开发指南](development.md)运行合成样例。运行后在 `outputs/phase1-pytest/` 查看快照、run、artifact 清单及两套临时目录；原桌面端操作与现有中间层选择方式不变。合同资源校验失败会给出 `ValueError`，结果清单不会发布。该样例使用合成标定参数，仅用于验证数据边界和业务入口，不用于真实测量。

## 输入数据

选择一个固定结构的数据集：

```text
data/input/<dataset>/
├── cali.txt 或 cali.json
├── scale.txt 或 scale.json
├── floorplan.png、floorplan.jpg 或 floorplan.jpeg
└── screenshots/
    └── 相机图片
```

| 输入 | 用途 |
| --- | --- |
| 平面图 | 全局俯视坐标基准。 |
| 相机图片 | 桌面检测与拼接输入；文件名需包含与标定一致的相机 ID。 |
| `cali` | 相机像素与平面图百分比坐标的四点对应。 |
| `scale` | 平面百分比、米制地面控制点、边界和设备清单。 |

TXT 与 JSON 使用相同内容结构，同时存在时优先 TXT。程序只从 `screenshots/` 读取相机图，不读取根目录中的预览图片。

图片可使用不同分辨率。相对坐标按当前图片换算；像素坐标必须与当前图片一致，或提供可验证的原标定宽高。宽高比冲突、未知裁剪/填边和无法解释的越界点会阻止运行。

## 启动

```bash
cd code
source .venv/bin/activate
store-vision
```

点击“选择数据集”，确认输入摘要为可运行，再进入输入优化。也可使用 `store-vision --dataset /path/to/dataset` 预加载。

## 三个一级页面

1. **输入优化**
   - 运行共享鱼眼 K/D、逐机 R/t、拟合初始化与联合 BA。
   - 发布不可覆盖的 `data/intermediate/<dataset>/run_*`。
   - 可选运行 SfM 和匹配诊断；SfM 是附加证据，不是基础业务硬约束。
2. **2.5D 建图**
   - 可跟随本次输入优化输出，也可选择其他合法中间层。
   - 检查 K/D/H、平面图、尺度与高度策略后生成 GeoJSON、预览、检测和一致性报告。
   - 支持基础、SfM 支撑加权和 SfM 注册相机对照模式。
   - 可实时纳入弱候选或排除误检，并单独保存人工复核结果。
3. **图像拼接**
   - 独立选择中间层和参数候选。
   - 按 K/D/R/t、共同世界坐标、单位与目标地面生成融合、透明叠加、覆盖和重叠结果。

两个业务页互不依赖，也不会扫描任意文件名猜测输入。缺少资源时，页面会显示具体字段或相机级原因。

## 输出目录

```text
data/
├── intermediate/<dataset>/run_*/
│   ├── manifest.json
│   ├── calibrations/<candidate>/camera_rig.json
│   ├── planar_projections/<candidate>.json
│   ├── frames/
│   └── business/height_policy.json
├── map25d_output/<dataset>/run_*/
│   ├── manifest.json
│   ├── map25d.geojson
│   ├── map25d_candidates.geojson
│   ├── map25d_preview.png
│   ├── review/
│   ├── detection/
│   ├── overlap/
│   └── alignment/
└── stitching_output/<dataset>/run_*/
    ├── manifest.json
    ├── mosaic_fused.jpg
    ├── mosaic_alpha.jpg
    ├── coverage_visualization.png
    ├── overlap_visualization.png
    └── cameras/
```

`manifest.json` 是稳定入口。每个 `run_*` 都不可覆盖；真实输入和运行产物在本地保留，默认不会进入版本库。

## 结果边界

- 2.5D 的 XY 来自平面映射；高度来自业务策略，不是图像量测的真实高度。
- SfM 只有完成业务坐标尺度、方向和原点对齐后，才可成为新的平面候选。
- `provisional` 或 `geometric_trial` 结果只能用于布局趋势检查，不能用于施工、测量或资产确认。
- 拼接基于单一地面；离地物体可能因视差出现错位或重影。
- 输入和已发布中间层不会被业务流程修改。

## 数据安全

使用者只应放入已授权的数据；`data/` 的数据包默认被 Git 忽略，只有 `apple_exam` 示例会被跟踪。历史数据移入可跟踪的 `archive/` 后不参与运行，正式功能只读取 `data/` 中的当前输入和当前口径产物。
