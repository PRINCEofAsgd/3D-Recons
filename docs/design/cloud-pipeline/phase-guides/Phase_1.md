# Store Vision Cloud Phase 1：基线、数据合同与本地适配

你现在在 `PRINCEofAsgd/3D-Recons` 的最新 `main` 上，完成[总体计划](../phases.md)中的 Phase 1。**你负责写代码、测试和正式文档；用户负责通过你给出的少量实验理解设计。**先核对实际仓库，不照抄本提示词中的旧路径。参考基线为 `8520c4958d2da55f784d6f370e19cfa1c05429b5`。

## 一、必须遵守的项目规则

1. 第一件事读取仓库根目录 `AGENTS.md`，严格执行其中关于理解顺序、文档职责、数据/隐私、中文注释、项目上下文 Step 和版本号的规定。若规则变化，以最新 `AGENTS.md` 为准，并指出影响。
2. 阅读 `README.md`、`docs/context/repository-structure.md`、`docs/user/development.md`、`docs/context/project-context.md`、`docs/design/02_algorithm_design.md`，按需查看 `code/store_vision`；在动代码前检查工作区已有变更，保护用户文件。
3. `code/` 是现有 Python 项目根；不要让新云端代码改变桌面 GUI、原 CLI 与本地 `schema v2` 消费者的行为。测试只用合成/去标识化夹具，不访问真实 `data/`、`archive/`。

## 二、先建立可验证基线

1. 画出实际调用链：输入 `cali/scale/floorplan/screenshots` → 输入预检与共享标定 → `publish_intermediate_package` → `load_intermediate_runtime` → `run_map25d_from_intermediate` / `run_parameter_stitching`。标出可选 COLMAP/SfM 与兼容 `pipeline.py` 的位置。
2. 检查现有单元/集成测试与合成夹具，运行仓库推荐的可行测试，记录通过、失败、跳过和环境限制。不要把尚未能运行的算法链路写成已验证。
3. 针对 `workspace.py` 的 manifest v2 列出 `dataset_id`、`schema_version`、候选 K/D/R/t/H、坐标系与单位、`capabilities`、输入引用、lineage。指出绝对路径为何跨容器失效。

## 三、代码实现

1. 定义一个最小、版本化的云端数据合同，不修改已发布 v2：`dataset snapshot`、`run`、`artifact` 三种清单；字段至少含 dataset/run/attempt 身份、producer/算法版本、参数摘要、对象 URI、字节数、SHA-256、媒体类型、父资源和时间。坐标系、单位、相机 ID、候选选择与能力门禁必须能被保留或引用。
2. 在 `code/store_vision` 下实现边界清晰的本地适配器：从合成数据集建立不可变输入快照；将云端合同资源 materialize 到独立临时目录；调用现有入口；将结果转换成云端 artifact 合同。设计成后续可替换存储后端的接口，避免过度抽象。
3. 对 URI/相对路径做规范化和越界检查；拒绝缺失文件、哈希不符、重复相机 ID、错误 schema、错误 dataset/run 身份、候选不一致。不要把机器绝对路径写入可移植合同或提交内容。
4. 提供一条合成样例的最小命令/测试链路，并证明换一个临时目录仍可恢复与消费。必须有非平凡的合同正反例测试以及原有关键回归测试。

## 四、架构原则与阶段边界

- 原输入和已发布中间层只读；新 run/attempt 使用独立目录。
- manifest 是资源发现与版本边界，不能用目录扫描猜文件；发布标志只能在所有资源校验通过后可见。
- 标定不是“逐相机完全独立”：共享 K/D 与联合 BA 必须作为全局阶段；本 Phase 不引入 Ray/Argo/Go/MySQL/MinIO。
- 若完整共享标定需要真实数据或昂贵可选依赖，保留合成样例可执行的最小链路并明确限制，不能伪造验证结果。

## 五、测试与文档

按 `AGENTS.md` 更新受影响的结构、开发、操作、上下文 Step、版本位置及 `.gitignore`。示例合同只能用合成标识和相对/示例 URI，不纳入运行产物或学习笔记。记录准确的运行命令、输出目录、失败时信息。不要擅自归档阶段摘要。

## 六、代码完成后的教学式讲解（必须在最终答复中）

挑三条高价值链路，先给一张调用顺序表或简图，再逐步解释每一层职责、输入/输出、为什么在这一层做、失败时谁发现：

1. **输入快照 → 中间层合同 → 两个消费者**：解释原 v2 与云端合同的关系，候选/能力门禁如何阻止错误输出，坐标和单位如何传递。
2. **本地 materialize → 现有算法 → stage-out**：解释适配器为什么是分布式边界，哪些是本地暂存，哪些是不可变持久结果。
3. **合同校验失败**：用一个哈希或路径错误例子说明失败类别、为何不重试、为什么不能发布半成品。

每条都给用户一个可亲手执行的小实验、预期现象和应观察的文件/字段；区分你亲自执行过的验证与用户待做的实验。

## 七、最终交付

报告实际改动文件、测试命令与结果、合同示例位置、尚未验证的依赖/风险、三条讲解、建议的提交说明。先完成本 Phase，不开始 Phase 2。现在从只读检查与 `AGENTS.md` 开始。
