# Phase 3 Ray 逐相机拼接合同

## 计算边界

`prepare_stitching_runtime` 在驱动端验证 schema、候选和能力，再从米制地面合同及配置确定共同 `WorldCanvas`。画布、候选、坐标合同、配置和 `TASK_VERSION` 的规范 JSON SHA-256 是 `canvas_hash`。所有相机使用同一画布快照。`prepare_camera_stitch_part` 是串行和 Ray 共用的纯逐相机算法：输入一张已解码图片、该机 K/D/R/t、画布和配置，返回去畸变后投影的 BGR 像素、有效掩膜、羽化权重和图像到画布矩阵。共享 K/D 联合标定、2.5D 跨相机去重仍为原全局消费者，不在本阶段拆分。

Ray task 从快照资源 URI 读取本相机原图，先验证 SHA-256/大小，再计算并写入 `npz` 分片及 JSON 清单；task 只返回小型清单，不广播像素数组，也不改全局覆盖数组。驱动端最多同时保留 `max_in_flight` 个任务引用。任务显式申请 CPU、可选 GPU 和内存资源，Ray 调度与驱动端限流共同约束并发。一个 task 默认一次占 1 CPU、0 GPU、256 MiB 调度内存，最多 2 个在途、每机至多 2 次尝试。资源量应按实际画布和 worker 可用内存调整；Ray 的调度内存声明不是进程内存硬限制。

全部必需相机的分片清单到达后，驱动端按相机集合、身份、目标 attempt、清单 SHA-256、结果 SHA-256、数组尺寸和类型逐一校验。随后按原相机顺序逐片读取，调用 `run_parameter_stitching` 的同一全局汇总逻辑，计算覆盖/重叠、羽化融合、裁剪和业务 manifest。校验阶段只保留当前分片数组，避免随相机数累积数组。`parallel_parts` 记录所选 attempt、worker PID、耗时和分片清单资源。失败时不产生业务成功 manifest；Phase 2 的 artifact/publication 也不会执行。

## 身份、存储与失败

业务身份是 `(job_id, run_id, candidate, canvas_hash, camera_id, task_version)`，执行身份另加 `part_attempt`。S3 对象键位于配置前缀下的 `attempts/<job>/<run>/<candidate>/<canvas_hash>/<camera>/<task_version>/<part_attempt>/objects/<sha-prefix>/<sha256>`；本地替身在同等 attempt 目录使用不可覆盖的 `part.npz` 和 `manifest.json`。分片清单 schema 为 `store-vision-stitch-part/1`，包含业务身份、输入图 SHA-256、相机参数摘要、画布摘要、结果 URI/SHA-256/大小、画布高宽、相机 ID、Ray task ID、worker PID/节点 ID 与耗时。既有 Phase 1/2 run 和 artifact 合同仍保留其独立身份与发布指针。

驱动端控制有限重试，Ray 的自动 task 重试关闭。worker 崩溃与暂态对象读取可在新的 `part_attempt` 重做该相机；错误 schema、候选、画布、相机集合、校验和或坏图片属于合同/数据错误，不合并也不发布。某次 task 即使留下对象，只有驱动端选定并校验的 attempt 能进入 reduce。重复执行已有输出目录会在派发前拒绝覆盖。Phase 2 发布仍由对象存储条件创建指针和 MySQL 终态约束；两者不是跨系统事务。

## 验收口径与边界

合成样例使用 `code/tests/fixtures/sample_store` 的九张去标识化图片、固定的测试 K/D/R/t 与相同画布参数。预定容差：输出尺寸、覆盖/相机统计完全一致，融合图、诊断图和逐机报告的整数像素差为 0。原因是逐相机算法相同，reduce 固定相机顺序，因此浮点加法顺序不变；若以后改为树形 reduce，需重新约定浮点容差。集成测试检查至少两个不同 Ray worker PID、注入单机第一次失败后只该机使用第二次 attempt、重试耗尽时无成功 manifest、重复输出拒绝覆盖；每个测试使用独立 Ray 临时目录，避免复用已停止集群。2026-10-08 本机还通过真实 MinIO/MySQL 的 API → worker → Ray → 分片 → reduce → Artifact 链路，以及真实 MinIO 上的单机故障实验。物理多节点、远程 S3 与 worker 进程被外部杀死的情形仍待独立环境验证。运行命令和资源配置见[开发指南](../../user/development.md#phase-3-ray-逐相机拼接)。

收尾版本中，分片实现位于 `ray_stitching.py`，快照和结果合同位于 `cloud_workspace.py`，Go worker 调用 `cloud_job_runner.py`。已发布清单的 producer 文本不因 Python 文件重命名而失效；旧快照与新 run 共同参与的真实 MinIO/MySQL Job 已成功发布，分片 `task_version` 更新为 `parameter-stitch-part/1.0.9`。模块重命名不改变上述业务身份、校验屏障和单次可见发布规则。
