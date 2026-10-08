# 项目上下文

## Step 8：云端 Phase 4 Argo DAG 与双下游业务分支（2026-10-08）

### 目标与结果

- 在 Phase 1～3 的真实快照、S3、MySQL 和 Ray 实现上新增 `cloud_pipeline` 独立容器阶段、Argo WorkflowTemplate 与 Go 可重放同步器。DAG 固定快照验证、全局标定/中间层、2.5D 与 Ray 拼接并行分支、汇总；跨 Pod 只传发布清单 URI 与 run SHA-256。两个消费者各自校验身份、候选、能力与参数，使用独立输出前缀。已发布且验证通过的同身份上游和分支可复用，错误数据集或候选拒绝。
- API 支持 `backend=argo` 与固定 `stitch_profile`，MySQL 新增 backend、候选、镜像与 profile 记录及 Stage 失败明细。同步器每 5 秒查询 Argo Workflow，以 Job 版本条件更新终态；重复回报不能覆盖结果，失败 Job 仍能查询另一成功分支 Artifact。最终成功要求拼接成功且 2.5D 成功或由能力门禁明确 blocked。模板显式配置 Secret、镜像、Pod 资源、超时及每阶段最多一次重试；拼接 Pod 要求 Ray 地址。
- 本次迭代于 2026-10-09 完成，当前版本为 `V1.0.11_20261009`。云端 Linux ARM64 镜像排除了不参与阶段命令的桌面 Qt，Workflow 资源参数改由 `podSpecPatch` 注入，独立 ServiceAccount 获得 executor 所需的最小回报权限，并新增仅供本机合成验收的 Ray head 清单。命令、部署与查询见[开发指南](../user/development.md#phase-4-argo-dag-与双分支)，合同与失败语义见[Phase 4 合同](../design/cloud-pipeline/contracts-phase4.md)。

### 验证与边界

- 本机 MySQL 8.4.10 应用 Phase 4 migration 并核对新列；原真实 MySQL 并发幂等测试通过。Go 单元测试覆盖 Workflow 身份参数、重复终态回报、分支部分成功与 API 部分结果查询。本次变更后 Phase 4 定向 4 项、全量 Python 合成回归 156 项及 Go 全量测试通过。WorkflowTemplate 已通过 Argo v4.1.4 的真实 CRD 服务端校验；资源 Quantity 占位符被 CRD 拒绝的问题已用 `podSpecPatch` 修复，标定 Pod 实际带 `2 CPU/4GiB` request 与 `4 CPU/8GiB` limit。
- 本机 MinIO 使用合成快照，在独立 scratch 运行当时的 1.0.10 `calibrate → map25d/stitching → summarize`；run `phase4run2/attempt1` 的两个 Artifact 分别包含 63 与 25 个经重读校验的文件，融合 JPEG SHA-256 为 `5b8a491bee60d1716f458a793ad265b5bb815a1049c0f672bd5eab22897ca6ce`。Ray 九个相机 task 分布在两个 worker PID、一个物理节点；首次连接因新版本 Ray token 认证配置不一致失败，隔离本机实验统一认证模式后成功。合成报告只用于本地链路，不代表真实标定质量。
- 本次在本机 kind `v0.33.0` / Kubernetes `v1.36.1` / Argo Workflows `v4.1.4` 建立真实集群；Docker Desktop 分配 12 GiB，Compose 的 MySQL/MinIO、单 Pod Ray 和 Go API/同步器同时运行。合成 Job `d99266650a42b9b5f3f2b21ca42238f2` 经 Go `POST /jobs` 创建真实 Workflow；verify、calibrate、map25d、ray-stitch、summarize 逻辑节点均 `Succeeded`，MySQL Job `succeeded` 且五个 Stage 均 `succeeded`。2.5D 和拼接分别发布 63、25 个逐对象校验通过的文件；九个 Ray task ID 分布在同一 Ray 节点的两个 worker PID，融合 JPEG SHA-256 与 Phase 3 串行对照一致。复验 Job `d318d784bb5e24ffa98e7b5745b21b30` 同样成功；两次 Ray Client 冷连接均先出现服务端进程竞态，拼接 Pod 在 Argo 的一次有界重试内成功，该不稳定性仍是后续运行边界。
- 真实上游失败验收使用不存在的合成快照，Job `49abd4715b60253bc0fdb3a03a4425b7` 的 verify 重试耗尽、Workflow 为 `Failed`；MySQL Job 为 `failed/upstream_failed`，verify Stage 为 `failed`，未启动的标定、两个分支和汇总均为 `blocked/upstream_failed`，没有发布结果。此项修复了 Argo `Omitted` 原先被误记为 `pending`、未运行汇总被误记为 `invalid_summary` 的状态缺口；Go 回归新增对应测试。
- 2026-10-09 重建并加载 `1.0.11` 云端镜像后，Ray Pod 内版本核对为 `V1.0.11_20261009`。未固定的间接依赖与前一成功镜像有五项版本差异；Job `b2fb7efbf6a803f854581e8895981805` 的 Workflow 到 `Succeeded`，但拼接 Pod 两次都因 Ray Client 服务端进程竞态失败；MySQL Job 正确结算为 `failed/branch_failed`，2.5D Stage 和 Artifact 仍为 `succeeded` 且可查，拼接与汇总为 `failed`。这也证明不能用 Workflow 总相位代替业务终态。
- 将前一成功镜像的云端运行依赖版本固定在 `cloud/requirements-runtime.txt` 后重新构建，镜像内 `pip freeze` 与该镜像完全一致。新 Job `1ff4c99b00c96d01f62fcaf21054cf9d` 的 Workflow 和 MySQL Job 均为 `succeeded`，五个 Stage 全部成功且分支 Pod 无重试；从 MinIO 重读校验 63 个 2.5D 文件、25 个拼接文件，九个 Ray task ID 分布于两个 worker PID，融合 JPEG SHA-256 与 Phase 3 基线一致。间接依赖漂移与失败同时出现，但未单独定位具体依赖；Ray Client 冷连接在先前相同依赖镜像中仍出现过首次失败，继续作为运行边界。
- 本机集群、Ray、MySQL/MinIO 及 API/同步器保留运行，供复查与继续合成实验；Kubernetes 单节点与 Ray 单 Pod 不代表物理多节点，真实共享标定、远程 S3、生产认证及 Phase 5 全量故障矩阵仍未验收。

## Step 7：云端 Phase 3 Ray 逐相机拼接（2026-10-06）

### 目标与结果

- 原参数拼接提取共同画布预检与纯逐相机去畸变/投影/有效掩膜/羽化权重函数，串行与 Ray 复用同一计算；覆盖、融合、裁剪和业务 manifest 仍按固定相机顺序全局汇总。
- 新增 Ray 限流调度、显式 CPU/GPU/内存申请、按相机独立 attempt 分片及 SHA-256 清单。所有必需分片通过身份、候选、画布、尺寸和字节校验后才汇总；worker 故障/暂态读取只重试该相机，重试耗尽或合同错误不发布。Phase 2 执行器设置 `SV_RAY_ADDRESS` 时接入，未设置时串行回退；2.5D 与共享标定保持原全局链路。
- 本 Step 初版为 `V1.0.6_20261006`；真实本地依赖联调为 `V1.0.8_20261008`；模块规范命名及 Phase 3 收尾为 `V1.0.9_20261008`。四个模块分别为 `cloud_workspace`、`s3_object_store`、`cloud_job_runner` 和 `ray_stitching`，Go worker、Python 命令、测试及文档引用已同步。目录与调用链见[结构文档](repository-structure.md)，启动、资源和合成实验见[开发指南](../user/development.md)，分片字段和失败语义见[Phase 3 合同](../design/cloud-pipeline/contracts-phase3.md)。

### 验证与边界

- 本机 macOS ARM64、Python 3.14、Ray 2.59.0、九张 2560×1440 合成图片、100×68 工作画布、2 CPU/0 GPU、每 task 256 MiB 资源申请和最多 2 个在途：两个不同 worker PID 在一个物理节点完成九个 task。原版串行代码、重构后串行和 Ray 的工作画布、裁剪尺寸 100×51、覆盖 4211 像素、重叠 3835 像素、相机统计与三张对照图片逐像素一致，融合 JPEG 字节 SHA-256 相同。单相机注入失败后仅该机由 attempt 0 转 attempt 1；重试耗尽时无成功 manifest，重复目标输出被拒绝。
- 全量 Python 回归 152 项通过（含 Ray 合成集成 3 项）；Go 单元测试通过，源码编译和 Git diff 检查通过。最后一次合成集成运行测得串行拼接 0.186 秒、Ray 启动 3.462 秒、已启动 Ray 的拼接运行含一次相机重试 0.142 秒；只供该小画布本机样例参考，不代表通用加速。真实 S3 传输、更多相机和更大画布可能转移瓶颈。
- 开发指南中的 Ray CLI 本机合成命令已实际执行：启动本地两 CPU Ray、九相机拼接、串行对照和停止均成功；两种输出的画布、裁剪、统计、逐机报告与融合 JPEG 哈希一致，九个 task 落在两个不同 worker PID。自定义 Ray 临时目录需同时传给 CLI 的 `--ray-temp-dir` 或 Phase 2 执行器的 `SV_RAY_TEMP_DIR`。
- 2026-10-08 本地 MySQL 8.4.10、MinIO RELEASE.2025-04-22T22-12-26Z 与两个进程的 Ray 联调：九张合成图片的 Job `129420cc449ab5a5189ca68615d72db9` 经 `pending → running → succeeded`，attempt1 发布 map25d、stitching 两个 Artifact。逐对象校验快照/run/两种 Artifact 分别为 12/7/63/25 个文件；九个不同 task ID 分布在两个 worker PID、一个物理节点。九个分片清单及 NPZ 均从 MinIO 重读并通过身份、SHA-256、尺寸和类型检查；还原 run 后串行对照的画布 165×101、裁剪 69×28、覆盖 1653、重叠 1549、逐机报告与融合/诊断图字节一致，融合 JPEG SHA-256 为 `5b8a491bee60d1716f458a793ad265b5bb815a1049c0f672bd5eab22897ca6ce`。
- 在真实 MinIO 上另行注入 CAMERA-01 首次 task 故障：最多 1 次尝试时无成功 manifest；最多 2 次时只有 CAMERA-01 使用第二次 part attempt，其他八机保持首次 attempt。Ray 集成测试改用每例独立短临时目录，消除残留集群地址引起的误连接。定向 Python 回归 10 项、全量 152 项及真实 MySQL 的 Go 测试通过。
- 规范命名后用旧 `producer=store_vision.cloud_phase1` 的已发布快照运行新 Job `c2456a0b28ff4cb9921a73008691d82e`：Go worker 调用新 `cloud_job_runner` 入口，attempt1 成功发布两种 Artifact；新 run 的 producer 为 `store_vision.cloud_workspace`，Ray 分片版本为 `parameter-stitch-part/1.0.9`。快照/run/map25d/stitching 的 12/7/63/25 个资源及九个分片从真实 MinIO 复核通过，两个 worker PID 的结果与串行输出逐字节一致，融合 JPEG SHA-256 保持 `5b8a491bee60d1716f458a793ad265b5bb815a1049c0f672bd5eab22897ca6ce`。新 `cloud_job_runner snapshot` 命令另上传并校验 12 个合成资源，producer 为新模块名。旧清单读取与新清单写入均已验证。
- 收尾回归：重命名后定向 Python 10 项、全量 152 项、真实 MySQL 的 Go 测试、`go vet`、源码编译与差异检查通过。临时 Ray/API/worker 和原有本地 Demo API 已停止，Compose 中 MySQL 与 MinIO 均为 `exited (0)`；保留数据卷和被忽略的合成验收产物供复查。
- 物理多节点、远程 S3、外部强杀 Ray worker、真实共享标定以及 Windows/Intel Mac 尚未验证；本次执行器使用合成 K/D/R/t 报告。Phase 2 串行执行器的真实依赖联调见 Step 6；未提前接入 Argo。

## Step 6：云端 Phase 2 对象存储与最小控制面（2026-10-06）

### 目标与结果

- 新增带前缀隔离、条件写、SHA-256/大小复核的 S3 兼容存储适配；Phase 1 快照、run 和 artifact 合同可经 MinIO URI 跨 scratch 使用，两个结果校验后创建已发布指针。
- 新增 Go API、MySQL migration 和独立轮询执行器，提供 Dataset 注册、Job 提交/查询、Artifact 查询与取消请求。Job 唯一键实现幂等，版本/attempt 条件更新保护终态，运行中取消只登记意图。
- 本地 Compose 固定 MySQL/MinIO 镜像，凭据仅从忽略的 `.env`/环境变量获取。合成样例、启动/迁移/查询/清理命令见开发指南；职责、状态和一致性边界见[Phase 2 合同](../design/cloud-pipeline/contracts-phase2.md)。本 Step 初版为 `V1.0.5_20261006`；2026-10-07 续验并修正本地启动配置与暂态重试后，版本更新为 `V1.0.7_20261007`。

### 验证与边界

- 初版离线 S3 故障测试 3 项、Go 控制面单元测试 4 项通过，竞态检查、`go vet` 与 Python 编译检查通过；当时 Docker daemon 未启动，真实依赖实验留待本次续验。
- 2026-10-07 本机 Docker Desktop 中以固定缓存镜像启动 MySQL 8.4.10、MinIO RELEASE.2025-04-22T22-12-26Z，迁移与 bucket 创建成功。合成 Job `82f272f2aff914154fe74f12f6a305dc` 经 `pending → running → succeeded`，attempt1 发布 2 个 Artifact；快照/run/map25d/stitching 的跨 S3 资源校验通过，资源数依次为 12/7/63/25。真实 MySQL 12 客户端同键并发提交测试通过；同键同体复用 Job、同键异体返回 409，API 重启后仍读取成功结果。
- 注入对象发布后、MySQL 提交前故障时，Job `723b88863b5c43502f2e02375b707a31` 的 attempt1 publication 已存在而 API 仍为 `running`、Artifact 为空；租约过期后 attempt2 成功且 API 仅公开 attempt2。未领取 Job 取消后结算 `failed/cancelled`，成功 Job 拒绝取消。关闭 MinIO 后 Job `b9f7c2c56683deb105f0c627ed24bf4e` 经 attempt1/2 的 `storage_transient` 与 15 秒重领等待，在恢复后的 attempt3 成功。Phase 1 与 S3 定向回归 7 项通过；Go 竞态测试、`go vet`、真实 MySQL 并发测试通过。以独立本地 Ray 实例排除已有集群干扰后，全量 Python 回归 152 项通过。完整命令及观察位置见开发指南与 Phase 2 合同。
- 本机固定 MinIO 镜像已缓存，远程镜像标签可拉取性尚未确认。当前合成验收未覆盖生产认证、物理多节点、远程 S3、真实共享标定或 Windows/Intel Mac。
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
- 2026-10-07 用户按 Phase 1 合同文档复验合成样例，终端结果为 `4 passed in 5.42s`，Phase 1 本地合同与跨目录消费的定向验收通过。该命令只覆盖 4 项合成测试，不替代全量回归或真实共享标定验收。
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
