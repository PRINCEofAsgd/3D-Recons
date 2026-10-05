# Store Vision Cloud Phase 4：Argo DAG 与双下游业务 Pipeline

你现在在 `PRINCEofAsgd/3D-Recons` 最新 `main` 上完成[总体计划](../phases.md)中的 Phase 4。你负责实现可运行编排、测试、正式文档；实现后向用户教学式讲清楚 Argo、Ray、控制面与算法消费者之间的分工。先核对 Phase 1～3 的真实状态，不能假设它们已经完成。

## 一、规则和现状检查

先读仓库根 `AGENTS.md`，严格执行中文注释、文档职责、上下文 Step、版号、数据/凭据保护；再按该文件阅读结构、开发、上下文和源码。保护工作区已有改动。对照实际 `workspace.py` 能力门禁、`run_map25d_from_intermediate`、`run_parameter_stitching` 和 Phase 3 Ray 入口确认哪些命令真正可在容器中运行。

## 二、DAG 的实际业务语义

```text
verify_snapshot
       ↓
calibrate_and_publish_intermediate（全局共享标定）
       ↓ 版本化 manifest URI + candidate
       ├───────────────┐
       ↓               ↓
map25d_branch     ray_stitch_branch
       └───────┬───────┘
               ↓
        summarize_job_state
```

1. 将每个阶段包装成可单独运行的容器命令，阶段间只传 job/run 身份与**已发布 manifest URI**，不通过容器本地路径共享状态。可选 COLMAP/SfM 是独立输入侧证据，主链路不依赖它。
2. Argo 只管跨阶段依赖、条件执行、超时、有界重试和 Pod 资源；Ray 只管拼接阶段内部逐相机并行；Go/MySQL 是对外业务状态真相；Store Vision 继续负责算法与业务输出。不要把相同重试策略无界叠加。
3. 中间层 `capabilities` 明确决定 2.5D/拼接分支可否运行。不满足时标 `blocked` 或带原因的 `skipped`，不能把未运行标成成功。两分支结果与失败原因分别保存；最终 Job 的成功准则在合同中明确（例如必需分支集合），不可临时含糊处理。
4. 固定 dataset、run、候选、参数版本和 manifest 哈希；重跑可复用已发布且验证通过的上游阶段，绝不能跨 dataset 或候选复用。两个分支各写独立输出前缀。
5. Argo workflow 事件与 Go 状态通过可重放的回报/同步器连接；回调重复或乱序时数据库状态条件更新保护最终结果。若使用轮询，写明频率、终止状态和重启恢复。

## 三、代码、配置和验证

实现 WorkflowTemplate/参数模板、容器入口、最小提交器/同步器、结果查询和配置示例。资源 requests/limits、secret 引用、镜像版本、超时和重试次数显式可配；真实密钥不提交。使用合成输入在本地 Kubernetes 环境至少贯通一条真实下游业务输出，并尽可能验证双分支；不能把 YAML 语法检查当作 DAG 成功。

测试覆盖 DAG 参数身份、能力门禁、重复回报、某分支失败/另一分支成功、重跑复用正确 manifest、错误候选拒绝。真实运行时记录 Argo 节点状态、Ray task 归属、两个输出 manifest 与 MySQL Job/Stage 状态。没有 K8s 环境则完成静态/单元/本地命令验证，明确真实 DAG 未验证。

## 四、仓库文档

依 `AGENTS.md` 更新结构、开发、用户使用、上下文 Step、版本和 `.gitignore`。给出从提交 Job 到查看 Workflow、阶段日志、对象结果和失败原因的准确步骤。不要归档对话或实验草稿，不提前做 Phase 5 的全量故障矩阵。

## 五、代码完成后的教学式讲解

选择三条链路，用实际文件/函数/资源名逐步讲：

1. **Job → Argo DAG**：为什么 Go 保存业务身份，Argo 管执行顺序，API 提交为何不等算法完成。
2. **中间层 → 两业务分支**：manifest/capability 如何解耦消费者；一条分支失败时另一条结果和最终 Job 状态如何定义。
3. **Argo → Ray → object store**：Argo task 与 Ray task 的粒度、资源与重试范围，各自何时重跑。

每条必须说明职责、调用关系、设计原因、输入/输出合同、失败/重试和分布式边界；安排一个用户可亲手执行的小实验和预期状态。最终答复含代码改动、真实验证与限制、讲解、建议提交说明。现在从只读检查开始。
