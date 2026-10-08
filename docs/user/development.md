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

对象上传与 MySQL 状态提交没有跨系统事务。上传后数据库更新失败会留下 API 不可见的 attempt 对象；同一 Job 的租约到期后最多重领三次，新 attempt 不复用旧结果。MinIO 暂态失败时，Job 显示 `pending/storage_transient`，15 秒后才可重领；恢复前可能消耗多次机会。过期且未发布的对象需按 Job/attempt 前缀人工清理。Phase 4 新增独立的 `backend=argo`，此处默认 worker 路径继续保留。细节见[Phase 2 一致性说明](../design/cloud-pipeline/contracts-phase2.md)。




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

## Phase 4 Argo DAG 与双分支

本机合成验收使用 Docker Desktop（分配至少 12 GiB 内存）、kind `v0.33.0`、Kubernetes `v1.36.1` 与 Argo Workflows `v4.1.4`。先按 Phase 2 启动 MySQL/MinIO，再从 kind 和 Argo 的官方固定版本发布源建立集群；`kind` 二进制保存在被忽略的 `cloud-local/bin/`。已有集群时先核对 `kubectl config current-context`，不要将 Workflow 部署到其他 context。Argo 官方完整 CRD 必须使用 server-side apply。旧数据库只执行一次 `002_phase4.sql`；新数据库按编号依次执行 `001_phase2.sql`、`002_phase4.sql`。

```bash
mkdir -p cloud-local/bin
curl -fL -o cloud-local/bin/kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-darwin-arm64
chmod +x cloud-local/bin/kind
cloud-local/bin/kind create cluster --name store-vision --image kindest/node:v1.36.1 --wait 5m
kubectl config current-context                 # kind-store-vision
kubectl get nodes                              # Ready
kubectl create namespace argo
kubectl apply --server-side -n argo -f \
  https://github.com/argoproj/argo-workflows/releases/download/v4.1.4/install.yaml
kubectl -n argo rollout status deployment/workflow-controller --timeout=5m
```

`cloud/argo/service-account.yaml` 同时包含独立 ServiceAccount 与仅允许 `workflowtaskresults` 的 `create/patch` Role/RoleBinding；缺少后者时算法容器可能成功而 Argo executor 报权限错误。模板中的 CPU/内存参数通过 `podSpecPatch` 注入，因为 CRD 的资源 Quantity 字段不接受模板占位符。

```bash
cd cloud
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u "$MYSQL_USER" "$MYSQL_DATABASE"' < migrations/002_phase4.sql
cd ..
docker build -f cloud/Dockerfile -t store-vision-cloud:1.0.11 .
cloud-local/bin/kind load docker-image store-vision-cloud:1.0.11 --name store-vision
kubectl get workflows.argoproj.io -A
# 合成输入只从测试夹具生成；首次执行使用新的输出目录。
set -a; . ./cloud/.env; set +a
cd code
MPLCONFIGDIR=../cloud-local/matplotlib .venv/bin/python -m tests.integration.phase2_sample --out ../cloud-local/phase4-sample
.venv/bin/python -m store_vision.cloud_job_runner snapshot --source ../cloud-local/phase4-sample/source --dataset-id phase4synthetic --snapshot-id snapshot1
cd ..
```

镜像必须能由目标集群拉取，API 的 `SV_ARGO_IMAGE` 必须是固定 tag 或 digest。云端 Dockerfile 只安装无界面的算法依赖；桌面 PyQt6 不进入 Linux ARM64 镜像。`cloud/requirements-runtime.txt` 固定本机成功验收镜像的云端运行依赖，避免重建时自动升级间接依赖。`cloud/argo/runtime.env.example` 列出 Pod 需要的 S3、Ray 和资源变量；复制到被忽略的 `cloud-local/argo-runtime.env` 并填写能从 Pod 访问的地址及本地凭据。本机 kind 可用 `SV_S3_ENDPOINT=http://host.docker.internal:9000`；`127.0.0.1:9000` 在 Pod 中通常指 Pod 自身。合成验收设置 `SV_RAY_ADDRESS=ray://ray-head:10001`、`SV_USE_SYNTHETIC_REPORT=1`，并在仅本地隔离的 Ray 示例中设置 `RAY_AUTH_MODE=disabled`；真实部署应启用受控认证。正常标定把合成报告开关设为 `0`；报告只挂载到标定 Pod。不要把真实密钥写进模板或提交环境文件。

```bash
mkdir -p cloud-local
cp cloud/argo/runtime.env.example cloud-local/argo-runtime.env
# 编辑 cloud-local/argo-runtime.env 后执行；namespace 可按部署环境调整。
kubectl create namespace store-vision-demo
kubectl -n store-vision-demo create secret generic store-vision-runtime \
  --from-env-file=cloud-local/argo-runtime.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n store-vision-demo create configmap store-vision-synthetic-report \
  --from-file=synthetic-report.json=cloud-local/phase4-sample/synthetic-report.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n store-vision-demo apply -f cloud/argo/service-account.yaml -f cloud/argo/workflow-template.yaml
kubectl -n store-vision-demo apply -f cloud/argo/ray-local.yaml
kubectl -n store-vision-demo rollout status deployment/ray-head --timeout=3m
```

ConfigMap 与 `ray-local.yaml` 只供本机合成实验；真实标定可不创建该 ConfigMap，正式 Ray 集群由运行环境提供。模板 `arguments.parameters` 显式给出资源、Secret 名、各节点超时和重试上限默认值，整个 Workflow 最多运行两小时。拼接 Pod 强制要求 `SV_RAY_ADDRESS`，否则分支失败，不能把串行结果冒充 Ray。Ray worker 应安装相同代码版本并可访问同一 S3 凭据；若集群启用了 Ray token 认证，应通过本地 Secret 提供 `RAY_AUTH_TOKEN`。

在两个终端分别启动 API 与同步器；不要为同一 Argo Job 启动旧 `-mode worker`。API 在提交时把镜像和 `stitch_profile` 存入 MySQL，同步器重启后仍使用提交时参数。`SV_ARGO_NAMESPACE` 与 `kubectl` 当前访问权限须指向同一个集群。

```bash
cd cloud
set -a; . ./.env; set +a
export SV_ARGO_NAMESPACE=store-vision-demo SV_ARGO_IMAGE=store-vision-cloud:1.0.11
GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode api -listen 127.0.0.1:8080
# 在第二个同样加载环境变量的终端：
GOCACHE="$PWD/../cloud-local/go-cache" go run . -mode argo-sync
```

用已上传的合成 Dataset（这里示例为 `phase4synthetic`）提交新 Job；每次新实验使用新的幂等键。响应 202 只说明 MySQL 建立 Job，不代表算法完成。最终成功要求拼接成功，且 2.5D 成功或因能力门禁明确 blocked；能力已就绪的任一分支失败则 Job 失败，另一分支已提交 Artifact 仍可查。

```bash
SNAPSHOT_URI="s3://$SV_S3_BUCKET/$SV_S3_PREFIX/manifests/snapshots/phase4synthetic/snapshot1/manifest.json"
curl -sS -X POST http://127.0.0.1:8080/datasets \
  -H 'Content-Type: application/json' -H 'X-Owner-ID: synthetic-owner' \
  -d "{\"id\":\"phase4synthetic\",\"snapshot_id\":\"snapshot1\",\"snapshot_uri\":\"$SNAPSHOT_URI\"}"
JOB_ID=$(curl -sS -X POST http://127.0.0.1:8080/jobs \
  -H 'Content-Type: application/json' -H 'X-Owner-ID: synthetic-owner' \
  -H 'Idempotency-Key: phase4-sample-001' \
  -d '{"dataset_id":"phase4synthetic","backend":"argo","candidate":"estimated","stitch_profile":"synthetic-small"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
export JOB_ID
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID"
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID/workflow"
kubectl -n store-vision-demo get workflows.argoproj.io "sv-$JOB_ID" -o wide
kubectl -n store-vision-demo get pods -l "workflows.argoproj.io/workflow=sv-$JOB_ID"
# 用上一行实际 Pod 名查看各阶段日志：
kubectl -n store-vision-demo logs POD_NAME --all-containers=true
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID/stages"
curl -sS -H 'X-Owner-ID: synthetic-owner' "http://127.0.0.1:8080/jobs/$JOB_ID/artifacts"
```

对象结果可以按 API 返回的 `publication_uri` 与每个 `manifest_uri` 使用同一 S3 环境读取。下列命令重新校验每个业务结果的资源字节并显示 Ray task 归属；`parallel_parts` 位于拼接业务 `manifest.json`。运行前在当前终端加载 `cloud/.env`，并保留 `JOB_ID` 环境变量。

```bash
cd ..
PYTHONPATH=code code/.venv/bin/python - <<'PY'
import json, os
from store_vision.cloud_workspace import validate_artifact
from store_vision.s3_object_store import S3ObjectStore
store = S3ObjectStore.from_env()
dataset, run = 'phase4synthetic', os.environ['JOB_ID']
uri = store.manifest_uri('published', dataset, run, 'attempt1')
summary = store.load_manifest(uri)
source = store.load_manifest(summary['run_manifest_uri'])
print('summary', summary['status'], summary['stages'])
for name, artifact_uri in summary['artifacts'].items():
    files = validate_artifact(store, store.load_manifest(artifact_uri), source)
    business = json.loads(files['manifest.json'])
    print(name, artifact_uri, len(files), business.get('parallel_parts', []))
PY
```

2026-10-08 本机真实 Workflow `sv-d99266650a42b9b5f3f2b21ca42238f2` 和复验 Workflow `sv-d318d784bb5e24ffa98e7b5745b21b30` 均到 `Succeeded`；两个分支及汇总成功，Go/MySQL Job 为 `succeeded`、五个 Stage 为 `succeeded`。首个 Job 从 MinIO 重读 63 个 2.5D 文件和 25 个拼接文件全部通过合同校验，九个 Ray task ID 分布在两个 worker PID、一个 Ray 节点，融合 JPEG SHA-256 与 Phase 3 对照一致。两次 Ray Client 冷连接的首个拼接 Pod 都因服务端进程竞态失败，Argo 的一次有界重试成功；复现实验应检查 Workflow 节点列表，不能只看最终成功。另用不存在的合成快照验收了上游失败：Workflow `sv-49abd4715b60253bc0fdb3a03a4425b7` 为 `Failed`，Go/MySQL Job 为 `failed/upstream_failed`；verify Stage 失败，未启动的标定、两个分支和汇总均为 `blocked/upstream_failed`，没有 Artifact。

2026-10-09 版号日期更新后，未固定间接依赖的重建镜像与前一成功镜像有五项依赖版本差异。Job `b2fb7efbf6a803f854581e8895981805` 的拼接在两次 Ray Client 连接中均失败：Workflow 汇总为 `Succeeded`，但 MySQL Job 正确为 `failed/branch_failed`，成功的 2.5D Artifact 仍可查询。固定云端依赖版本并重建后，Job `1ff4c99b00c96d01f62fcaf21054cf9d` 的 Workflow、MySQL Job 和五个 Stage 均为 `succeeded`，两个分支 Pod 首次成功；从 MinIO 重读的 63 个 2.5D 文件与 25 个拼接文件均通过校验，九个 Ray task ID 属于两个 worker PID，融合 JPEG SHA-256 与 Phase 3 对照一致。未单独定位五项依赖中哪一项导致连接故障，先前相同依赖版本仍发生过首次连接失败；因此 Ray Client 冷连接稳定性仍是边界。真实标定和生产集群未验收。Phase 4 定向 4 项、全量 Python 156 项与 Go 全量测试均通过。运行后可用下列命令复核合成阶段代码和控制面：

```bash
cd code
PYTHONPYCACHEPREFIX=../cloud-local/pycache QT_QPA_PLATFORM=offscreen \
  MPLCONFIGDIR=../cloud-local/matplotlib .venv/bin/python -m pytest -q tests/integration/test_cloud_pipeline.py
cd ../cloud
GOCACHE="$PWD/../cloud-local/go-cache" go test ./...
```

业务状态、重放与失败准则见[Phase 4 合同](../design/cloud-pipeline/contracts-phase4.md)。
本机集群、Ray、MySQL/MinIO 和 API/同步器在本次验收后保持运行。结束实验时可停止 API/同步器，再执行 `kubectl -n store-vision-demo scale deployment/ray-head --replicas=0` 与 `docker compose -f cloud/compose.yaml stop`；保留 kind 集群、MinIO/MySQL 卷及忽略目录供复查。只有确认不再需要本机合成 Workflow 和对象数据后，才删除集群或卷。

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
