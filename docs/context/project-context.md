# 项目上下文

## Step 3：正式交付整理与版本库重建（2026-08-24）

### 目标与结果

- 清除学习资料、外部项目资料、旧版本输出、版本库历史和非正式产物。
- 将自动化测试数据改为中性、去标识化的合成夹具。
- 移除仅服务旧实验复现的全仓路线审计、人工标定盘点和 B0 基线入口；桌面主流程、共享鱼眼标定、SfM 诊断、2.5D 和拼接保持不变。
- `data/` 与 `archive/` 均默认可跟踪；`data/` 保留原始输入和每层当前口径产物，`archive/` 保存不参与运行的历史数据。
- 完善构建、缓存、凭据与临时产物忽略规则，清除旧 Git/SVN 元数据后初始化新 Git。
- 版本更新为 `V1.0.2_20260824`。

### 验证与边界

- 自动化测试、源码编译、CLI 帮助、敏感信息扫描和忽略规则检查均需通过后才完成重建。
- 已从现存历史副本恢复 `apple_bj_fyh` 与 `decathon` 的图片、标定和平面图；两者原有的其他输入文件仍需从原数据源补回。
- 待恢复输入数据集：`Anta_Shenyang`、`Apple_Guomao`、`Apple_Xian`、`Apple_Yuehui`、`Decathon_Amiens`、`Huawei_Beijing`、`Huawei_Shenzhen`、`osl`。
- 待恢复当前口径产物：各数据集最新的 `intermediate/run_*`、`map25d_output/run_*` 与 `stitching_output/run_*`；准确运行目录清单见本 Step 下方“待恢复运行目录”。
- 本机无数据副本或系统快照，原 SVN 服务当前不可连接；恢复前不得用 `archive/` 中的旧格式输出冒充当前口径产物。

### 待恢复运行目录

- 中间层：`Anta_Shenyang/run_20260803_094601`、`Apple_Guomao/run_20260803_092923`、`Apple_Xian/run_20260731_144919`、`Apple_Yuehui/run_20260803_092931`、`Decathon_Amiens/run_20260803_094717`、`Huawei_Beijing/run_20260803_101120`、`Huawei_Shenzhen/run_20260803_094534`、`Xiaomi/run_20260803_external_import`、`apple_bj_fyh/run_20260803_094806`、`decathon/run_20260803_094650`。
- 2.5D：`Anta_Shenyang/run_20260811_175434`、`Apple_Xian/run_20260811_175344`、`Apple_Yuehui/run_20260811_175214`、`Huawei_Beijing/run_20260811_175444`、`Huawei_Shenzhen/run_20260803_094155`、`apple_bj_fyh/run_20260803_095025`。
- 拼接：`Apple_Xian/run_20260731_145018`、`Apple_Yuehui/run_20260803_093434`、`Huawei_Beijing/run_20260803_094029`、`Huawei_Shenzhen/run_20260803_094150`、`Xiaomi/run_20260803_114216`、`apple_bj_fyh/run_20260803_095030`。

## 当前功能基线

- PyQt6 桌面端包含“输入优化 / 2.5D 建图 / 图像拼接”三个一级页面。
- 输入优化发布版本化中间层；两个业务页可独立选择中间层和候选。
- 2.5D 支持桌高感知投影、基础/SfM 辅助模式和对象人工复核。
- 拼接按 K/D/R/t 和地面坐标合同生成融合、透明叠加、覆盖与重叠结果。
- SfM 为可选诊断与证据来源，不阻塞具备完整基础合同的业务流程。

初始化阶段的必要设计演进见[历史阶段摘要](../record/project-demo-initialization.md)。
