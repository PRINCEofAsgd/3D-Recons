# Phase 4 Argo DAG 与业务状态合同

## 适用范围

本阶段增加 `backend=argo` Job、独立容器阶段、WorkflowTemplate 和 Go 同步器。现有 `backend=worker` 默认路径仍由 Phase 2 worker 执行。本机合成验收用 kind/Kubernetes、Argo 官方固定版本清单及 `cloud/argo/ray-local.yaml` 启动单 Pod Ray；正式集群、Ray 认证和多节点运行仍由部署环境负责。

## DAG 与职责

`verify-snapshot → calibrate-and-publish-intermediate → (map25d-branch || ray-stitch-branch) → summarize-job-state`。标定是全局计算；两个消费者分别物化同一已发布 run 清单，使用自己的 scratch 和对象前缀。Argo 只控制 Pod 依赖、资源、超时与每节点至多一次重试。拼接 Pod 必须设置 `SV_RAY_ADDRESS`，Ray 在该阶段内派发逐相机 task。独立本地命令不设置 `SV_REQUIRE_RAY` 时仍可做串行消费者验收。Go/MySQL 保存对外 Job、Stage、Artifact 元数据。SfM/COLMAP 是可选的上游证据，不是 DAG 硬依赖。

Go API 的 `POST /jobs` 请求体新增 `backend`（`worker` 或 `argo`）、`candidate` 与 `stitch_profile`；默认仍是 `worker/estimated/default`。当前共享标定只发布 `estimated`，其他候选在提交时拒绝。一个 Job 固定 `dataset_id`、快照 URI、`run_id=job_id`、`attempt1`、候选、参数 profile 和镜像版本；同一 owner 的幂等键也绑定这些字段。Argo Workflow 名为 `sv-<job_id>`，同步器重启后按 MySQL 中的 running Job 每 5 秒重新查询或提交同名 Workflow。

阶段之间只传版本化快照 URI、run manifest URI 与 SHA-256，不依赖前一 Pod 的本地文件。`run_for` 会核对 URI 路径中的 dataset/run/attempt、清单字节哈希、候选、算法版本、快照 lineage、全部资源与能力门禁；错误候选或另一数据集不能复用。`calibrate` 重新执行时校验并复用同身份已发布 run。两个分支分别写 `attempts/<dataset>/<run>/<attempt>/map25d/` 和 `/stitching/`，并发布独立 Artifact manifest。已发布分支只有在来源与参数摘要均匹配时可复用。

## 能力、状态与最终准则

run manifest 的 `capabilities.map25d_ready` 与 `capabilities.stitching_ready` 是唯一业务门禁。能力不足的分支写 `blocked` 和缺失原因，不写 Artifact。两个能力已就绪的分支均为必需；拼接始终是必需业务输出。最终成功要求拼接 `succeeded`，且 2.5D 为 `succeeded` 或由能力门禁判定的 `blocked`。能力已就绪但业务失败时 Job 为 `failed/branch_failed`，另一分支的已校验 Artifact 仍在 `GET /jobs/<id>/artifacts` 可查询。上游失败而未启动下游时两分支记 `blocked/upstream_failed`。汇总清单保存两分支状态、原因、Artifact URI、run 哈希和参数 profile；MySQL 的 `failure_type` 保存短错误码，`failure_detail` 保存能力原因或 Argo 节点消息，完整 Python 异常查看 Pod 日志。

Argo 节点失败不自动等于业务结果成功。`summarize_job_state` 同时检查节点相位、run 能力和每个 Artifact 的全部对象。`succeeded` 节点缺少有效 Artifact 时，分支仍记 `failed`。Go 只接受汇总节点输出中固定身份与 URI 结构的结果，并在事务中锁定 Job、条件更新版本、写入两个 Stage 和已验证分支的 Artifact；重复或乱序同步不能覆盖终态。上游失败而 Argo 将后继节点标为 `Omitted`/`Skipped` 时，未运行阶段记为 `blocked/upstream_failed`，Job 记为 `failed/upstream_failed`，根因仍在失败节点及 Pod 日志查看。对象发布与 MySQL 更新不构成分布式事务，数据库不可用时同步器重新读取 Workflow 并重放。取消请求在尚未提交 Workflow 时直接结算；运行中维持 `cancel_requested`，待 Workflow 终态后结算为 `failed/cancelled`，不承诺立刻终止 Pod。

每个 Argo 容器阶段 `retryStrategy.limit=1`，`retryPolicy=OnFailure`，因此确定性失败也最多多运行一次；`activeDeadlineSeconds=7200`，每节点另有 10～45 分钟超时。Ray 逐机 task 默认至多 2 次 attempt，与 Argo 的整 Pod 重试属于不同层级；总次数有界，不能把 Argo 重试当作逐机重试。拼接 Pod 强制要求 `SV_RAY_ADDRESS`；没有地址时阶段失败。每次运行把 `stitch_profile` 固定在 Job 与 Workflow 参数中；当前配置 `default` 或仅用于合成验收的 `synthetic-small`。合成报告只在 `SV_USE_SYNTHETIC_REPORT=1` 且标定 Pod 挂载报告时读取。

## 查询与验证边界

`GET /jobs/<id>` 返回业务终态、版本和汇总 URI；`GET /jobs/<id>/stages` 返回各阶段状态、attempt、manifest URI、短错误码与 Argo 节点消息/能力缺失明细；`GET /jobs/<id>/artifacts` 在 Job 失败时也返回已提交的独立成功分支；`GET /jobs/<id>/workflow` 返回 Kubernetes namespace 和 Workflow 名。完整 Python 异常需查看对应 Pod 日志。同步器每轮扫描后等待 5 秒，每次 `kubectl` 请求限时 15 秒；大量或不可达的 Workflow 会延长实际轮询间隔。Job 从领取起 2 小时 15 分钟仍无终态则以 `workflow_timeout` 收束；重启后可按持久的 Workflow 名恢复。所有 API 仍使用 Phase 2 的本地 `X-Owner-ID` 资源分区，并非生产身份认证。具体合成命令见[开发指南](../../user/development.md#phase-4-argo-dag-与双分支)。

2026-10-08 本机以 kind `v0.33.0`、Kubernetes `v1.36.1`、Argo Workflows `v4.1.4`、MySQL `8.4.10` 和 MinIO 完成真实合成 DAG 验收。Workflow `sv-d99266650a42b9b5f3f2b21ca42238f2` 的 verify、calibrate、map25d、stitching 和 summarize 逻辑节点均为 `Succeeded`；Ray 拼接首次 Pod 连接时服务端进程异常退出，Argo 按一次重试上限在第二次 Pod 成功。复验 Workflow `sv-d318d784bb5e24ffa98e7b5745b21b30` 也成功，但重复出现相同的 Ray Client 冷连接竞态，不能视为已解决。Go/MySQL Job 为 `succeeded`，五个 Stage 为 `succeeded`，发布两个独立 Artifact；从 MinIO 重读并校验 2.5D 的 63 个文件、拼接的 25 个文件。九个相机 Ray task 有九个 task ID，分布在一个 Ray 节点的两个 worker PID；融合 JPEG SHA-256 与 Phase 3 串行对照一致，为 `5b8a491bee60d1716f458a793ad265b5bb815a1049c0f672bd5eab22897ca6ce`。另用不存在的合成快照触发真实上游失败：Workflow `Failed`，Job `failed/upstream_failed`，未运行的标定、两分支和汇总均为 `blocked/upstream_failed`，没有 Artifact。2026-10-09 的新镜像复验还触发了真实部分成功：Workflow 总相位为 `Succeeded`，但拼接两次 Ray Client 冷连接失败，Job `failed/branch_failed`，2.5D Artifact 仍能查询；这证明 Workflow 总相位不能代替 Go 业务终态，也说明该 Ray Client 竞态可能耗尽现有重试。本机结果仍只证明合成输入、单节点 Kubernetes/Ray 与本地 MinIO/MySQL 路径；真实共享标定、物理多节点、远程 S3、生产认证及完整故障矩阵未在本阶段验收。

2026-10-09 将前一成功镜像的云端依赖版本固定到 `cloud/requirements-runtime.txt` 后，Job `1ff4c99b00c96d01f62fcaf21054cf9d` 在更新版号镜像上完成双分支：Workflow、MySQL Job 与五个 Stage 均成功，两个分支 Pod 均无重试，两个 Artifact 分别含 63 和 25 个已校验对象，九个 Ray task ID 分布在两个 worker PID，融合 JPEG SHA-256 保持一致。依赖漂移与前述失败同时出现，但具体影响项未单独定位；Ray Client 冷连接在原依赖镜像也曾首次失败，仍需作为运行边界观察。
