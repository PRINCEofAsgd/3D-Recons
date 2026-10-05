# Phase 2 对象存储与控制面合同

## 边界与调用链

`code/store_vision/cloud_s3.py` 用 S3 Signature V4 的 path-style 请求对带前缀的键执行条件 PUT、GET 与 SHA-256/大小复核。`cloud_phase2.upload_snapshot` 经 Phase 1 预检上传输入快照；清单仅含 `s3://` URI。Go `POST /datasets` 登记快照 URI，`POST /jobs` 只创建数据库元数据。独立 `cloud/main.go` worker 领取 Job，调用 `cloud_phase2.execute`：下载到 Job/attempt 独立 scratch → 原共享标定/测试显式合成报告 → 原 v2 中间层 → 原 2.5D 与拼接消费者 → 上传 attempt 对象 → 校验 run/artifact → 条件创建 `published` 指针 → MySQL 条件提交结果。API 不同步执行算法，也不把图像存入 MySQL。

```text
snapshots/<dataset>/<snapshot>/objects/<prefix>/<sha256>
attempts/<dataset>/<run>/<attempt>/objects/<prefix>/<sha256>
manifests/snapshots/<dataset>/<snapshot>/manifest.json
manifests/runs/<dataset>/<run>/<attempt>/manifest.json
manifests/artifacts/<dataset>/<run>/<attempt>/<workflow>/manifest.json
manifests/published/<dataset>/<run>/<attempt>/manifest.json
```

以上对象键均位于配置的 `SV_S3_PREFIX` 下。单对象 `If-None-Match: *` 防止覆盖同键；同键重试后重新读取并比较完整字节。run/artifact 清单在 Phase 1 全量资源校验后创建；publication 指针在两个 artifact 校验后创建。条件写只保证**单个对象键**不覆盖；MySQL 更新与 S3 写入之间没有跨系统事务。publication 对象可能已存在而数据库仍非成功，用户 API 只在 `jobs.status=succeeded` 时公开 Artifact URI。未提交的 attempt 可由重新领取的新 attempt 隔离，之后按 Job/attempt 前缀清理；不宣称恰好一次执行。

## 数据表与状态

`datasets` 保存 owner、快照 URI；`jobs` 同时是 run 元数据，保存 dataset、幂等键与请求摘要、`status_version`、`attempts`、失败类型和输入/输出清单 URI；`stages` 保存单阶段状态与 attempt；`artifacts` 保存两个业务 manifest URI。`jobs(owner_id,idempotency_key)` 唯一约束保证重复提交不能新建 Job；摘要相同返回原 ID，摘要不同返回 409。状态 CAS 使用 `status_version`，重领还核对 `attempts`。合法状态路径为：

```text
pending → running → succeeded
pending → running → failed
running → pending                  # 对象存储暂态失败，最多三次领取
pending/running → cancel_requested → failed(cancelled)
```

执行中的取消只登记意图；本阶段无法中断 Python 算法。若完成时已收到取消，worker 不向数据库提交成功 Artifact。已经 `succeeded` 的 Job 拒绝取消，原有结果仍可见。API/worker 重启后状态从 MySQL 读取；运行中租约 90 秒并每 20 秒续约，过期可重新领取，超过三次则以 `lease_expired` 失败。旧 attempt 的完成不通过 attempt 和版本条件。MinIO 暂不可用时 Python 以退出码 75 标示，worker 以 `storage_transient` 有界重试；坏资源/合同错误终止为 `execution`。

`X-Owner-ID` 是本地 Demo 的分区标识，API 校验 ID 格式并按 owner 限制查询；它不是身份认证。请求 JSON 限 4 KiB，拒绝未知字段、错误身份和不同前缀的快照 URI。`POST /jobs` 的 HTTP 200/202 都不是计算完成的证明。

## 验证边界

本阶段可离线执行的测试：Python S3 协议替身覆盖重复 PUT、坏校验和、部分上传、缺对象、暂不可用；Go SQL mock 覆盖提交幂等分支、原子创建 stage、重启后的状态读取、过期 attempt 重领和取消 CAS。`TestConcurrentSubmitMySQL` 需显式传本地 Demo 的 `SV_TEST_MYSQL_DSN`，才会通过真实 MySQL 唯一约束检验 12 个并发提交；未设置时跳过。`-test-fail-after-upload` 可在真实依赖启动后制造对象发布与数据库提交之间的故障。完整合成从注册到结果查询的命令见[开发指南](../../user/development.md)。当前环境 Docker daemon 无法启动，真实 MinIO/MySQL、端到端状态序列与对象键尚未实测；不能将离线替身视作真实联调。
