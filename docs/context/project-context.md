# 项目上下文

## Step 7：云端 Phase 3 Ray 逐相机拼接（2026-10-06）

### 目标与结果

- 原参数拼接提取共同画布预检与纯逐相机去畸变/投影/有效掩膜/羽化权重函数，串行与 Ray 复用同一计算；覆盖、融合、裁剪和业务 manifest 仍按固定相机顺序全局汇总。
- 新增 Ray 限流调度、显式 CPU/GPU/内存申请、按相机独立 attempt 分片及 SHA-256 清单。所有必需分片通过身份、候选、画布、尺寸和字节校验后才汇总；worker 故障/暂态读取只重试该相机，重试耗尽或合同错误不发布。Phase 2 执行器设置 `SV_RAY_ADDRESS` 时接入，未设置时串行回退；2.5D 与共享标定保持原全局链路。
- `V1.0.6_20261006`。目录与调用链见[结构文档](repository-structure.md)，启动、资源和合成实验见[开发指南](../user/development.md)，分片字段和失败语义见[Phase 3 合同](../design/cloud-pipeline/contracts-phase3.md)。

### 验证与边界

- 本机 macOS ARM64、Python 3.14、Ray 2.59.0、九张 2560×1440 合成图片、100×68 工作画布、2 CPU/0 GPU、每 task 256 MiB 资源申请和最多 2 个在途：两个不同 worker PID 在一个物理节点完成九个 task。原版串行代码、重构后串行和 Ray 的工作画布、裁剪尺寸 100×51、覆盖 4211 像素、重叠 3835 像素、相机统计与三张对照图片逐像素一致，融合 JPEG 字节 SHA-256 相同。单相机注入失败后仅该机由 attempt 0 转 attempt 1；重试耗尽时无成功 manifest，重复目标输出被拒绝。
- 全量 Python 回归 152 项通过（含 Ray 合成集成 3 项）；Go 单元测试通过，源码编译和 Git diff 检查通过。最后一次合成集成运行测得串行拼接 0.186 秒、Ray 启动 3.462 秒、已启动 Ray 的拼接运行含一次相机重试 0.142 秒；只供该小画布本机样例参考，不代表通用加速。真实 S3 传输、更多相机和更大画布可能转移瓶颈。
- 开发指南中的 Ray CLI 本机合成命令已实际执行：启动本地两 CPU Ray、九相机拼接、串行对照和停止均成功；两种输出的画布、裁剪、统计、逐机报告与融合 JPEG 哈希一致，九个 task 落在两个不同 worker PID。自定义 Ray 临时目录需同时传给 CLI 的 `--ray-temp-dir` 或 Phase 2 执行器的 `SV_RAY_TEMP_DIR`。
- 物理多节点、真实 MinIO/MySQL 控制面联调、外部强杀 Ray worker 和 Windows/Intel Mac 尚未验证。Phase 2 的真实依赖限制仍见 Step 6；未提前接入 Argo。

## Step 6：云端 Phase 2 对象存储与最小控制面（2026-10-06）

### 目标与结果

- 新增带前缀隔离、条件写、SHA-256/大小复核的 S3 兼容存储适配；Phase 1 快照、run 和 artifact 合同可经 MinIO URI 跨 scratch 使用，两个结果校验后创建已发布指针。
- 新增 Go API、MySQL migration 和独立轮询执行器，提供 Dataset 注册、Job 提交/查询、Artifact 查询与取消请求。Job 唯一键实现幂等，版本/attempt 条件更新保护终态，运行中取消只登记意图。
- 本地 Compose 固定 MySQL/MinIO 镜像，凭据仅从忽略的 `.env`/环境变量获取。合成样例、启动/迁移/查询/清理命令见开发指南；职责、状态和一致性边界见[Phase 2 合同](../design/cloud-pipeline/contracts-phase2.md)。版本更新为 `V1.0.5_20261006`。

### 验证与边界

- 离线 S3 故障测试 3 项通过；Go 控制面单元测试 4 项通过，竞态检查和 `go vet` 通过；Python 编译检查通过。真实 MySQL 并发测试已提供并跳过：本机 Docker daemon 无法启动，因而未执行真实 MinIO/MySQL 联调、端到端合成 Job 与对象键/状态序列实测。当前 Python 环境缺项目开发依赖，Phase 1 测试无法在本次重跑；Step 5 记录的 146 项通过仍为此前验收证据。
- S3 上传和 MySQL 状态没有跨系统事务；数据库提交失败时已上传 attempt 可能不可见，重领会使用新 attempt。API 的 HTTP 200/202 不代表算法完成。`X-Owner-ID` 仅为本地分区字段，尚无生产认证；Windows/Intel Mac 仍未实测。

## Step 5：云端 Phase 1 合同与本地适配（2026-10-05）

### 目标与结果

- 在原 `schema v2` 与 GUI/CLI 行为不变的前提下，新增 `store-vision-cloud/1` 的 dataset snapshot、run 和 artifact 清单，以及本地内容寻址对象存储接口。
- 合成固定输入经预检后冻结为只读快照；run 将 v2 候选 K/D/R/t/H、坐标/单位、能力门禁和来源关系封装为可移植资源；跨临时目录恢复后，原 2.5D 与参数拼接入口分别生成结果并发布 artifact。
- 合同拒绝路径越界、对象缺失/哈希或大小不符、错误 schema 与 dataset/run/attempt 身份、重复相机 ID、候选不一致。资源校验完成前无发布清单；run/attempt 使用独立命名空间。
- 版本更新为 `V1.0.4_20261005`。正式合同字段、运行命令与输出位置见[Phase 1 合同](../design/cloud-pipeline/contracts-phase1.md)。

### 验证与边界

- 参考基线为 `8520c4958d2da55f784d6f370e19cfa1c05429b5`；开工前工作区已有设计文档迁移与未跟踪云端计划，均予保留。
- 原有 142 项自动化测试在本机已有隔离 Python 环境中通过；加入 Phase 1 的 4 项合成合同测试后，全量回归为 146 项通过、0 项失败、0 项跳过。合成样例跨目录真实运行两个原业务消费者；源码编译、原 CLI 帮助与运行产物忽略规则通过。
- 合成共享报告只用于合同和消费链路验收；真实共享 K/D 与联合 BA、可选 COLMAP/SfM、Windows 与 Intel Mac 未在本阶段验证。Phase 1 不包含远程对象存储、控制面或并行编排。

## Step 4：数据层级归位与 Python 项目扁平化（2026-10-05）

### 目标与结果

- 将本机源目录中的九组数据包复制到 `data/input/`，共核对 259 个有效文件的逐文件 SHA-256；系统缩略图和目录状态文件未复制。`Decathon_amiens` 规范为 `Decathon_Amiens`，中文标定、尺度、平面图与截图目录名统一为程序支持的 `cali.json`、`scale.json`、`floorplan.*` 和 `screenshots/`。旧 `decathon` 截图尾部采集时间戳已移除，原相机 ID 和图像字节保持不变。
- 九组包只含原始截图、人工标定、尺度或平面图及可选 CAD 辅助资料，没有中间层 `manifest.json` 与候选合同，因此全部归入 input；本次没有可归入 intermediate 的源包。
- `apple_bj_fyh` 和 `decathon` 缺少 `scale` 文件，保持原样并在输入结构文档标明，不能作为完整数据集直接运行。其余七组具备输入合同的四类必需文件。
- Python 项目扁平化为 `code/store_vision/` 与 `code/tests/` 并列；四份专项文档归入 `docs/design/`，相机内参示例归入包内 `examples/`。路径定位、打包发现、测试夹具、文档命令和平台版号同步更新。
- `data/` 数据包默认被 Git 忽略，仅保留结构说明与 `input/apple_exam/` 示例；`archive/` 不做通配忽略。版本更新为 `V1.0.3_20261005`。

### 验证与边界

- 142 项合成夹具自动化测试通过；源码编译、CLI 帮助、离线 wheel 构建与源数据哈希比对通过。
- 本机软件源不可访问，新的独立 `.venv` 未完成依赖安装；自动化测试使用本机已有 Python 3.14 环境并以新 `code/` 为导入路径。Windows 与 Intel Mac 仍待实机验证。
- 旧 Step 3 的待恢复列表为当时历史状态；当前源目录没有 `intermediate`、`map25d_output` 或 `stitching_output` 运行包，本次未补造产物。`Huawei_Wangfujing` 按源包身份保留，未擅自等同于历史清单中的 `Huawei_Beijing`。

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
