# 云端分布式数据处理 Demo：分阶段开发与学习计划

> 基线：[3D-Recons `main`](https://github.com/PRINCEofAsgd/3D-Recons/tree/8520c4958d2da55f784d6f370e19cfa1c05429b5)，提交 `8520c4958d2da55f784d6f370e19cfa1c05429b5`（2026-10-05）。风格参考 [DeviceHub `docs/foundation/phases.md`](https://github.com/PRINCEofAsgd/DeviceHub/blob/9a4fceafada19b940a594b1fce98de655a53b538/docs/foundation/phases.md) 与其 [阶段提示词](https://github.com/PRINCEofAsgd/DeviceHub/tree/9a4fceafada19b940a594b1fce98de655a53b538/docs/record/phase-guides)，提交 `9a4fceafada19b940a594b1fce98de655a53b538`。开工时仍须重新核对最新代码和 `AGENTS.md`。

> 文档建议放在 `docs/design/cloud-pipeline/`：沿用 DeviceHub 的分阶段写法，但 3D-Recons 的 `AGENTS.md` 把 `docs/record` 限定为明确要求归档的阶段摘要，设计路线与可复用提示词更适合 `docs/design`。本交付文件尚未写入 3D-Recons 仓库。

## 目标与边界

基于 Store Vision 真实的多相机输入、共享标定、统一中间层、2.5D 建图与图像拼接，做一套**可运行、可解释、可故障验证的云端批处理 Demo**。重点是数据合同、任务拆分、跨进程并行、DAG 编排、资源与状态边界；不开发 Spark/Flink/Ray 内核，不宣称 TB/PB 级生产能力。

这是一项基于原算法项目的**新扩展/PoC**。原有三维重建算法链路与参数调优可作为真实经历；云端控制面和分布式运行能力只能在实际完成、验证后作为 Demo 经历描述。

## 现有项目事实与新扩展位置

```text
data/input/<dataset>/
  cali、scale、floorplan、screenshots
       │ 原始输入只读
       ▼
输入预检 + 共享鱼眼 K/D、逐机 R/t、联合 BA（全局阶段）
       ▼
data/intermediate/<dataset>/run_*/manifest.json（schema v2）
       ├── camera_rig.json / planar_projections / observations / frames
       ├── 2.5D：逐相机检测 → 全局去重、复核、GeoJSON
       └── 拼接：逐相机去畸变/投影 → 全局覆盖、羽化融合
```

现有入口是 `code/store_vision/cli.py`，数据合同集中在 `code/store_vision/data/workspace.py`；业务消费分别由 `mapping/map25d_pipeline.py` 和 `mapping/parameter_stitcher.py` 承担。`pipeline.py` 是兼容 headless 流水线，不应被误认成新的云端控制面。`data/` 的真实数据默认忽略，测试只用 `code/tests/fixtures/sample_store` 等合成/去标识化数据。原中间层 manifest 引用本机绝对路径，**不能直接当作对象存储可移植合同**。

云端建议保留 `code/` 为唯一 Python 项目根，新控制面可在根目录独立 `cloud/`（最终布局以开工时仓库结构与 `AGENTS.md` 为准）：

```text
Go API + MySQL       控制面：Dataset、Job、Stage、Artifact、幂等请求与状态
MinIO/S3             数据面：原始快照、云端合同、attempt 临时结果、已发布产物
Ray                  计算面：逐相机 CPU 任务并行及全局 reduce
Argo Workflows       编排面：阶段依赖、条件分支、超时、重试和资源声明
Kubernetes           运行面：容器放置、CPU/内存隔离、服务与任务扩缩
现有 Store Vision    算法面：共享标定、2.5D 与拼接，不复制重写核心算法
```

最小贯通链路：提交一个合成数据集 → 冻结输入快照 → 建立 Job → 运行标定/中间层 → Ray 逐相机拼接任务 → 汇总拼接图；2.5D 在能力满足时独立从中间层运行 → 发布带来源关系的结果 → 查询状态和失败原因。可选 SfM/COLMAP 作为输入侧证据，不作为主链路成功的必要条件；其失败不应覆盖父中间层。

## 不变量（每个 Phase 都要复查）

1. 输入与已发布中间层只读；每个 run 和 attempt 有独立命名空间，失败结果不能伪装为成功发布。
2. 现有本地 `schema v2` 由原消费者继续读取。新建**明确版本的云端合同/适配层**保存 `s3://` 资源 URI、校验和、尺寸、候选、坐标系/单位和 lineage；不偷偷把 v2 的绝对路径替换成 URI。
3. 业务消费者只经 manifest/适配层取资源；不得靠扫描目录猜测输入；拒绝越界路径、错误数据集身份与不支持的 schema。
4. Job/Stage/Task 的重试按“可能重复执行”设计。对象存储写入和 MySQL 状态更新不是一个事务；用 attempt 前缀、校验、发布指针和幂等/CAS 解决可见性与恢复。
5. 标定是跨相机全局计算；Ray 只并行真正独立的逐相机步骤。全局画布、覆盖/融合、跨相机去重属于 barrier 后的 reduce。
6. 只以真实实验支持吞吐、扩展与容错结论；记录输入规模、资源配置、耗时、结果校验和，不制造“高吞吐/PB 级”数字。
7. 每次代码迭代遵循仓库 `AGENTS.md`：必要中文注释、同步正式文档和 `.gitignore`、在 `docs/context/project-context.md` 顶部新增/续写 Step、按版号位置同步更新。阶段学习笔记默认留在 Codex 讲解中，不擅自写进正式仓库。




## Phase 1：基线、数据合同与本地可复现链路

### 1-1 当前链路与输入边界

1. 对照最新仓库走通合成样例的输入预检、标定/中间层、两个消费者；记录真正能跑通的范围与外部依赖。
2. 建立“原始输入 → 中间层 → 业务输出”的资源清单、坐标/单位/候选/能力门禁表。
3. 给云端扩展定义最小验收样例与结果比较口径；不以真实 `data/` 包作测试夹具。

### 1-2 可移植云端合同与本地适配

1. 定义版本化 dataset snapshot、cloud run manifest、artifact manifest，注明哈希、大小、媒体类型、URI、lineage、算法/参数版本。
2. 实现本地文件存储适配与安全 materialize/stage-out；保留原 v2 语义，不破坏 GUI/CLI。
3. 实现 manifest 校验、重复导入/坏文件/错误 schema 的测试。

**交付门槛：**合成样例可在另一临时目录复现，云端合同无本机绝对路径，原有测试继续通过。

提示词：[Phase_1.md](phase-guides/Phase_1.md)。




## Phase 2：对象存储与最小控制面

### 2-1 数据面

1. 用 MinIO/S3 兼容 API 上传不可变输入快照，记录对象校验和和内容大小。
2. 下载到独立 scratch 目录供现有算法消费；产物先写 attempt 前缀，校验后再发布 manifest。

### 2-2 控制面

1. 实现最小 Go API：Dataset 注册、Job 提交/查询、结果 URI 查询与取消请求；不引入无关微服务。
2. MySQL 保存 Dataset、Job、Stage/Task、Artifact 元数据与幂等键，定义合法状态迁移。
3. 提交同一幂等键不能创建两个 Job；数据库只存元数据，不存图像大对象。

**交付门槛：**用户可提交一个合成数据集并查询状态；MinIO 断开、重复提交和部分上传有可解释结果。

提示词：[Phase_2.md](phase-guides/Phase_2.md)。




## Phase 3：Ray 最小分布式计算

### 3-1 真正可拆的计算单元

1. 提取逐相机拼接准备步骤：从已选标定候选与共同画布读取该相机图片、去畸变、投影，输出有校验和的独立分片。
2. Ray 将不同相机分配为任务；控制并发、CPU/内存需求和内存中的大对象传递。
3. 汇总器在全部必需分片通过后进行全局覆盖统计、羽化融合和 manifest 发布。

### 3-2 分布式证据

1. 对比原单机拼接结果、尺寸和统计量，约定数值容差。
2. 在至少两个 Ray worker 进程/节点上记录任务归属和失败重试；用可控失败证明一个分片重试不使整个输入重做。

**交付门槛：**有真实跨 worker 的逐相机并行证据，结果与单机基线一致，失败分片不能被汇总器读取。

提示词：[Phase_3.md](phase-guides/Phase_3.md)。




## Phase 4：Argo DAG 与双下游 Pipeline

### 4-1 阶段编排

1. DAG：输入快照校验 → 标定/中间层发布 → 并行分支（2.5D、Ray 拼接）→ 汇总最终状态。
2. 2.5D 在 manifest 能力门禁不满足时明确标为 blocked/skipped，不能报成功；可选 SfM 独立于主链路。
3. Argo 管阶段依赖、容器资源、超时和有限重试；Ray 管阶段内逐相机计算，Go/MySQL 保存业务状态。

### 4-2 可追踪性

1. 用 job_id/run_id/candidate/manifest URI 贯穿 API、Argo、Ray 和产物。
2. DAG 重跑复用已发布的不可变上游结果，不能误用另一候选或另一 dataset 的结果。

**交付门槛：**DAG 可在本地集群完成至少一条真实业务输出，两个分支的成功/失败独立可见。

提示词：[Phase_4.md](phase-guides/Phase_4.md)。




## Phase 5：失败、重试与可观测性

### 5-1 失败语义

1. 区分输入/合同错误（不可重试）、资源与网络暂态错误（有界重试）、算法质量门禁失败（需修正数据或参数）。
2. 验证 Worker 崩溃、对象上传后状态更新前崩溃、重试重复执行、取消与超时；补偿/回收未发布 attempt。
3. 用数据库约束和状态版本控制并发发布；明确“至少一次执行、至多一次可见发布”的适用条件。

### 5-2 观察与排障

1. 结构化日志、Job/Stage/Task 耗时、失败类型、重试次数、输入/输出字节和 Ray 并发指标。
2. 一条 job_id 能定位 API 接收、DAG 节点、Ray task、对象产物和最终状态。

**交付门槛：**故障矩阵均有真实运行证据；不能重复发布、跨 run 污染或覆盖原始数据。

提示词：[Phase_5.md](phase-guides/Phase_5.md)。




## Phase 6：容器化部署、性能与讲述材料

### 6-1 最小部署

1. 固定版本镜像与配置；在本地 Kubernetes 部署 API、MySQL/MinIO（Demo 配置）、Argo、Ray worker；记录 CPU/内存请求与限制。
2. 冷启动新环境，以合成样例完成提交到结果下载的验收；密钥仅走本地环境/Secret 示例。

### 6-2 性能与学习总结

1. 固定数据集与画布尺寸，比较单机和 1/2/N worker 的端到端耗时、纯计算耗时、上传下载耗时、吞吐、资源占用。
2. 分析非线性扩展：全局标定/reduce、对象传输、Ray 调度、内存/CPU 饱和。
3. 形成架构图、调用链、故障矩阵、结果与局限；把已实现与仅设计的部分清楚分开。

**交付门槛：**另一台/新建环境能按文档复现 Demo；性能结论只引用可追溯的实测数据。

提示词：[Phase_6.md](phase-guides/Phase_6.md)。




## 阶段之间的检查点

| 完成后 | 必须能回答 | 实物证据 |
| --- | --- | --- |
| Phase 1 | 为什么现有 manifest 不能直接搬到 S3？ | 合同样例、迁移适配测试 |
| Phase 2 | 控制面与数据面分别保存什么？ | API/DB 状态、对象清单、重复提交测试 |
| Phase 3 | 哪些步骤能并行，哪里必须等待全体相机？ | worker 归属、对照结果、失败分片实验 |
| Phase 4 | Argo 与 Ray 各管哪一层调度？ | DAG 运行图、两个输出 manifest |
| Phase 5 | 重试会不会产生两个对外可见结果？ | 故障矩阵、状态/对象一致性证据 |
| Phase 6 | 增加 worker 后瓶颈转移到哪里？ | 实测报告、复现命令与架构图 |

## 使用阶段提示词

每次只向 Codex 发送一个 Phase 文档。Codex 先读最新 `AGENTS.md` 与仓库事实，再独立完成该阶段代码、测试和正式文档；随后挑关键链路用教学方式解释职责、调用顺序、设计原因、数据合同、分布式边界和失败/重试。阶段验收应给用户可亲手执行的少量实验及预期现象；没有执行的实验明确标为待验证。不要让下一阶段技术提前膨胀当前范围。
