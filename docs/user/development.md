# 开发指南

## 支持环境

- Python 3.11～3.14。
- macOS ARM64 为当前验证平台；Windows 10/11 x64 需实机验证。
- 可选 COLMAP 用于 SfM；基础桌面流程不要求安装 COLMAP。

## 安装

以下以 `python3.14` 为例；也可使用已安装的 `python3.11`～`python3.13`。先确认解释器版本，再用同一个解释器创建虚拟环境。macOS 的 `python3` 可能指向不受支持的系统 Python 3.9，不能只按命令名判断版本。

```bash
cd code
python3.14 --version
python3.14 -m venv .venv
source .venv/bin/activate
python --version
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -e '.[dev]'
```

激活后 `python --version` 必须显示 3.11～3.14；若版本不符，先运行 `deactivate`，在 `code/` 中删除旧 `.venv`，再用受支持的解释器重新创建。升级 pip 不会改变虚拟环境的 Python 版本；依赖安装失败时，后续测试也不会有 pytest。依赖只写入项目 `.venv`。Windows PowerShell 可用 `py -3.14 -m venv .venv` 创建，再用 `.venv\Scripts\Activate.ps1` 激活；其他受支持版本相应调整版本号。

## 测试与静态验证

```bash
cd code
source .venv/bin/activate
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/store-vision-matplotlib python -m pytest
python -m compileall -q store_vision tests
store-vision --help
```




## Phase 1 云端合同本地验收（仅用合成夹具）：

```bash
cd code
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=../outputs/matplotlib \
  python -m pytest -q tests/unit/test_cloud_workspace.py \
  --basetemp ../outputs/phase1-pytest
```

输出清单位于 `../outputs/phase1-pytest/test_snapshot_and_run_restore_0/store/manifests/`；`first/` 与 `second/` 是独立物化目录，后者含原 2.5D 与拼接结果。`--basetemp` 会清空目标目录。合同字段与失败条件见[Phase 1 合同](../design/cloud-pipeline/contracts-phase1.md)。本地存储根可另设为被忽略的 `cloud-local/`，不得把真实输入或产物提交。

测试夹具位于 `tests/fixtures/sample_store/`，全部为去标识化合成数据。测试不得读取 `data/` 中的业务数据或 `archive/`。




## Phase 2 本地云端 Demo

需要 Docker Compose、Go 1.24+ 和上文安装好依赖的 Python 虚拟环境。只使用 `tests/fixtures/sample_store/` 的合成输入。`cloud/.env` 中设置自己的本地密码与访问键，勿提交该文件。下面各命令从仓库根目录起步。

```bash
cd cloud
cp .env.example .env
# 编辑 .env 中的占位值；SV_MYSQL_DSN 中的密码须与 SV_MYSQL_PASSWORD 一致，DSN 保留引号以便 shell 加载。
docker compose up -d --wait
docker compose ps
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u "$MYSQL_USER" "$MYSQL_DATABASE"' < migrations/001_phase2.sql
set -a; . ./.env; set +a
cd ../code
source .venv/bin/activate
python -m store_vision.cloud_job_runner bucket
MPLCONFIGDIR=../cloud-local/matplotlib python -m tests.integration.phase2_sample --out ../cloud-local/sample
python -m store_vision.cloud_job_runner snapshot --source ../cloud-local/sample/source --dataset-id synthetic --snapshot-id snapshot1
```

`--wait` 会等待 MySQL 健康检查。2026-10-07 本机使用已缓存的 `mysql:8.4.10` 与 `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z`，以 `docker compose up -d --wait --pull never` 启动成功；新机器需要先取得这两个固定镜像，当前未验证该 MinIO 标签的远程仓库可拉取性。`.env` 应限制为仅本机用户可读，且已被 Git 忽略。

另开两个终端，分别运行 API 和执行器；两者读取同一 `cloud/.env`。执行器的 `-synthetic-report` 仅用于该合成验收，省略后会运行原共享标定算法。长计算只在执行器进程中运行。

```bash
cd cloud
set -a; . ./.env; set +a
GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode api -listen 127.0.0.1:8080
```

```bash
cd cloud
set -a; . ./.env; set +a
GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode worker -python ../code/.venv/bin/python -scratch ../cloud-local/scratch -synthetic-report ../cloud-local/sample/synthetic-report.json
```

第三个终端注册、提交并查询：

```bash
cd cloud
set -a; . ./.env; set +a
SNAPSHOT_URI="s3://$SV_S3_BUCKET/$SV_S3_PREFIX/manifests/snapshots/synthetic/snapshot1/manifest.json"
curl -sS -X POST http://127.0.0.1:8080/datasets -H 'Content-Type: application/json' -H 'X-Owner-ID: synthetic-owner' -d "{\"id\":\"synthetic\",\"snapshot_id\":\"snapshot1\",\"snapshot_uri\":\"$SNAPSHOT_URI\"}"
JOB_ID=$(curl -sS -X POST http://127.0.0.1:8080/jobs -H 'Content-Type: application/json' -H 'X-Owner-ID: synthetic-owner' -H 'Idempotency-Key: sample-001' -d '{"dataset_id":"synthetic"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID"
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID/artifacts"
```

`POST /jobs` 返回 202 和 `pending` 只表示已接收；正常预期状态序列为 `pending → running → succeeded`，轮询 `GET /jobs/<id>` 到 `succeeded` 后再查 artifacts。worker 日志按 `job=<id> attempt=attempt1 status=...` 标记领取和终态。对象键依次位于 `$SV_S3_PREFIX/snapshots/synthetic/snapshot1/objects/`、`$SV_S3_PREFIX/attempts/synthetic/<job>/attempt1/objects/` 和 `$SV_S3_PREFIX/manifests/{runs,artifacts,published}/...`。2026-10-07 的本机真实联调记录见[Phase 2 合同](../design/cloud-pipeline/contracts-phase2.md)。`X-Owner-ID` 只是本地 Demo 的资源分区标识，不是生产认证。取消入口为 `POST /jobs/<id>/cancel`，仍需携带相同 owner 头；运行中先显示 `cancel_requested`，执行器完成后才结算，不能保证立刻中断算法。

可重复的“上传后、数据库提交前崩溃”实验：停止正常 worker，在同样的启动命令末尾加 `-test-fail-after-upload`，另用新的幂等键 `sample-002` 提交 Job。worker 上传并创建 `attempt1` publication 后主动退出，日志出现 `injected failure after upload before database commit`；API 仍显示 `running` 且 artifacts 为 202/空列表。重新启动不带该标志的 worker，约 90 秒租约到期后领取 `attempt2`，正常情况下转为 `succeeded`，API 只返回 `attempt2` 的 URI。`attempt1` 对象仍存在，需按前缀清理；2026-10-07 已在本机真实 MinIO/MySQL 验证该序列。

```bash
# 在已加载 cloud/.env 的 cloud/ 终端启动故障 worker。
GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode worker -python ../code/.venv/bin/python -scratch ../cloud-local/scratch -synthetic-report ../cloud-local/sample/synthetic-report.json -test-fail-after-upload
# 另一终端将上文提交命令的 Idempotency-Key 改为 sample-002 后提交；故障 worker 退出后运行正常 worker 命令。
```

检查合成故障测试：

```bash
cd code
PYTHONPYCACHEPREFIX=../cloud-local/pycache python -m unittest tests.unit.test_s3_object_store -v
cd ../cloud
GOCACHE="$PWD/../cloud-local/go-cache" go test ./...
# 本地 Demo 数据库完成 migration 后，另设 SV_TEST_MYSQL_DSN 才会运行真实并发唯一键实验。
SV_TEST_MYSQL_DSN="$SV_MYSQL_DSN" GOCACHE="$PWD/../cloud-local/go-cache" go test -run TestConcurrentSubmitMySQL -v
```

最后一条只可针对本地 Demo 数据库运行。测试使用随机合成身份并清理自己的行；无需该实验时不要设置 `SV_TEST_MYSQL_DSN`。验收后先停止 API、worker 和 Ray，再在 `cloud/` 执行 `docker compose stop`，保留 MySQL/MinIO 卷供复查。仅在确认这些卷完全属于可丢弃的本地 Demo 时，才执行 `docker compose down -v --remove-orphans` 和清理对应的 `cloud-local/` 运行目录。

对象上传与 MySQL 状态提交没有跨系统事务。上传后数据库更新失败会留下 API 不可见的 attempt 对象；同一 Job 的租约到期后最多重领三次，新 attempt 不复用旧结果。MinIO 暂态失败时，Job 显示 `pending/storage_transient`，15 秒后才可重领；恢复前可能消耗多次机会。过期且未发布的对象需按 Job/attempt 前缀人工清理。Phase 4 的 Argo 将替换本地轮询执行器，不改变 manifest 与数据库边界。细节见[Phase 2 一致性说明](../design/cloud-pipeline/contracts-phase2.md)。




## Phase 3 Ray 逐相机拼接

云端模块按职责命名为 `cloud_workspace`（清单合同与物化）、`s3_object_store`（对象存储）、`cloud_job_runner`（快照与 Job 命令）和 `ray_stitching`（逐机分片与汇总）。Go worker 已使用 `store_vision.cloud_job_runner`；独立命令从仓库文档中的新模块路径启动。

Ray 是可选依赖；桌面拼接和未设置 `SV_RAY_ADDRESS` 的 Phase 2 执行器仍走串行路径。先在项目虚拟环境安装 Ray。下面只使用合成输入，生成目录位于被忽略的 `cloud-local/`；每次重新执行样例需换一个输出目录和 attempt ID。

```bash
cd code
source .venv/bin/activate
python -m pip install -e '.[dev,ray]'
python -m tests.integration.phase2_sample --out ../cloud-local/phase3-sample
python - <<'PY'
import json
from pathlib import Path
from store_vision.data.workspace import publish_intermediate_package
root = Path('../cloud-local/phase3-sample')
report = json.loads((root / 'synthetic-report.json').read_text(encoding='utf-8'))
publish_intermediate_package(root / 'source', root / 'intermediate', report)
PY
```

本机两个 Ray worker 进程的启动、运行和停止命令如下。`/tmp/svray` 避免 macOS 的 Unix socket 路径长度限制。`--task-cpus`、`--task-gpus`、`--task-memory-bytes` 是每 task 的 Ray 资源申请，`--max-in-flight` 是驱动端上限；相机数增加时应结合画布尺寸调节。`--task-gpus=0` 仍运行 CPU 版 OpenCV。

```bash
ray start --head --num-cpus=2 --include-dashboard=false --temp-dir=/tmp/svray --disable-usage-stats
python -m store_vision.ray_stitching \
  --intermediate ../cloud-local/phase3-sample/intermediate \
  --output ../cloud-local/phase3-sample/ray-stitching \
  --store-root ../cloud-local/phase3-sample/objects \
  --job-id synthetic-job --run-id synthetic-run --attempt-id attempt1 \
  --ray-temp-dir /tmp/svray \
  --task-cpus 1 --task-gpus 0 --task-memory-bytes 268435456 \
  --max-in-flight 2 --max-attempts 2
ray stop
```

串行对照使用相同画布参数：

```bash
python - <<'PY'
from store_vision.mapping.parameter_stitcher import ParameterStitchConfig, run_parameter_stitching
run_parameter_stitching('../cloud-local/phase3-sample/intermediate',
    '../cloud-local/phase3-sample/serial-stitching',
    config=ParameterStitchConfig(pixels_per_metre=8, max_canvas_long_edge=400,
                                 max_canvas_pixels=120000))
PY
```

`ray-stitching/manifest.json` 的 `parallel_parts` 包含相机、目标 attempt、Ray task ID、worker PID/节点 ID、耗时与分片资源；不同 PID 证明本机跨进程执行。`serial-stitching` 和 `ray-stitching` 的尺寸、`summary`、逐机统计和输出图片应完全一致。重复使用已存在的 `--output` 会拒绝覆盖。自动化验收含单相机首尝试失败、只重试该机与失败不发布：

```bash
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/store-vision-matplotlib \
  python -m pytest -q tests/integration/test_ray_stitching.py
```

Phase 2 Go worker 可通过 `SV_RAY_ADDRESS=auto` 与 `SV_RAY_TEMP_DIR=/tmp/svray` 接入上述本地 Ray：它仍先运行共享标定和中间层，再把快照里的逐机图片资源交给 Ray；未设置 `SV_RAY_ADDRESS` 时运行串行拼接。可选 `SV_RAY_TASK_CPUS`、`SV_RAY_TASK_GPUS`、`SV_RAY_TASK_MEMORY_BYTES`、`SV_RAY_MAX_IN_FLIGHT`、`SV_RAY_MAX_ATTEMPTS` 对应上述资源选项。Ray worker 必须能访问相同 Python 包和 S3 环境变量；运行产物、凭据和日志不得提交。正式分片合同见[Phase 3 合同](../design/cloud-pipeline/contracts-phase3.md)。

2026-10-08 已在本机用真实 MySQL/MinIO 和一个物理节点的两个 Ray worker 进程完成合成 Job 联调。复现时先按 Phase 2 步骤启动 Compose、迁移、建桶与上传合成快照；在加载同一 `cloud/.env` 的终端启动上述 `ray start`，再给 Phase 2 worker 命令增加以下环境变量，API、Dataset 注册、Job 提交和查询命令仍按上节执行。每次验收使用新的 Dataset/Snapshot 身份或新的 Job 幂等键，避免复用已经发布的 attempt。

```bash
SV_RAY_ADDRESS=auto SV_RAY_TEMP_DIR=/tmp/svray SV_RAY_MAX_IN_FLIGHT=2 \
  GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode worker \
  -python ../code/.venv/bin/python -scratch ../cloud-local/phase3-scratch \
  -synthetic-report ../cloud-local/sample/synthetic-report.json
```

本机验收 Job `129420cc449ab5a5189ca68615d72db9` 的 `attempt1` 成功发布两个 Artifact。快照/run/map25d/stitching 各有 12/7/63/25 个已校验资源，九个相机对应九个 task ID、两个 worker PID；从 MinIO 重读九个分片清单和 NPZ 均通过合同校验。恢复 run 后与串行拼接对照，165×101 工作画布、69×28 裁剪、覆盖 1653 像素、重叠 1549 像素、逐机报告及融合/诊断图字节一致。融合 JPEG SHA-256 为 `5b8a491bee60d1716f458a793ad265b5bb815a1049c0f672bd5eab22897ca6ce`。真实 MinIO 上的单机故障实验确认重试耗尽无 manifest；允许第二次尝试时仅故障相机重跑。Ray 自动化测试使用各自独立的短临时目录，可在停止验收集群后重复运行。物理多节点、远程 S3、外部强杀 worker、真实共享标定与其他平台仍待验证。

模块规范命名后的收尾 Job `c2456a0b28ff4cb9921a73008691d82e` 读取了旧 `producer` 的快照，由新的 Go → `cloud_job_runner` → Ray 入口完成 attempt1，并发布两个 Artifact；九个分片的 `task_version` 为 `parameter-stitch-part/1.0.9`，两进程输出与串行结果逐字节一致。另经新的 `python -m store_vision.cloud_job_runner snapshot` 入口上传并校验了 12 个合成资源。已有清单的 producer 文本无需迁移。

Phase 3 收尾时可在 `cloud/` 执行 `docker compose stop`，预期 `docker compose ps -a` 中 `mysql`、`minio` 都显示 `exited (0)`；该命令保留卷。本机 2026-10-08 已按此方式停止两容器和 Demo API、worker、Ray。需要再次联调时先用 `docker compose up -d --wait` 恢复依赖，并重新启动所需进程。




## 运行

```bash
cd code
source .venv/bin/activate

# 桌面界面
store-vision

# 预加载一个已授权数据集
store-vision --dataset ../data/input/<dataset>

# 兼容 headless 流水线，必须显式指定输出目录
store-vision --headless \
  --dataset ../data/input/<dataset> \
  --output /tmp/store-vision-result
```

## 标定与诊断命令

```bash
# 只规划 COLMAP 命令
store-vision calibration-demo \
  --dataset ../data/input/<dataset> \
  --output /tmp/calibration-demo \
  --dry-run

# 不运行 COLMAP，只验证人工标定与报告链路
store-vision calibration-demo \
  --dataset ../data/input/<dataset> \
  --output /tmp/calibration-demo \
  --skip-colmap

# 鱼眼联合标定
store-vision fisheye-calibration \
  --dataset-path ../data/input/<dataset> \
  --output-path /tmp/fisheye-calibration \
  --seed 0

# 只读分析已有 COLMAP 数据库与模型
store-vision geometry-diagnostics \
  --database-path /path/to/run/database.db \
  --image-path ../data/input/<dataset>/screenshots \
  --model-path /path/to/run/model_txt \
  --output-path /tmp/geometry-reports

# 将已授权外部参数规范化为中间层
store-vision import-external-calibration \
  --source-path /path/to/authorized-source \
  --output-path ../data/intermediate/<dataset>/run_<time>
```

真实 COLMAP 运行固定使用参数列表而非 shell 字符串；已有 `database.db` 的目录拒绝覆盖。几何诊断以 SQLite 只读模式访问数据库；已有报告必须显式传 `--overwrite` 才会重建。

## 数据与产物管理

- 原始输入放在 `data/input/<dataset>/`，程序不得写回。
- 中间层、2.5D 和拼接结果分别写入 `data/intermediate`、`data/map25d_output`、`data/stitching_output`。
- `data/` 的数据包默认被 Git 忽略，只跟踪目录说明和 `input/apple_exam/` 示例。源数据仅在已授权的本地环境使用。
- 旧数据需要留档时移入 `archive/`；该目录不做通配忽略，但正式代码和测试不得依赖它。
- 临时实验写入 `/tmp` 或被忽略的 `outputs/`，不得混入 `data/`。

## 卸载

```bash
deactivate
rm -rf code/.venv
```

该操作只移除项目虚拟环境，不修改数据目录。
