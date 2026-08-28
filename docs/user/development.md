# 开发指南

## 支持环境

- Python 3.11～3.14。
- macOS ARM64 为当前验证平台；Windows 10/11 x64 需实机验证。
- 可选 COLMAP 用于 SfM；基础桌面流程不要求安装 COLMAP。

## 安装

```bash
cd Code/StoreVision
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -e '.[dev]'
```

依赖只写入项目 `.venv`。Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。

## 测试与静态验证

```bash
cd Code/StoreVision
source .venv/bin/activate
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/store-vision-matplotlib python -m pytest
python -m compileall -q src tests
store-vision --help
```

测试夹具位于 `tests/fixtures/sample_store/`，全部为去标识化合成数据。测试不得读取 `data/` 中的业务数据或 `archive/`。

## 运行

```bash
cd Code/StoreVision
source .venv/bin/activate

# 桌面界面
store-vision

# 预加载一个已授权数据集
store-vision --dataset ../../data/input/<dataset>

# 兼容 headless 流水线，必须显式指定输出目录
store-vision --headless \
  --dataset ../../data/input/<dataset> \
  --output /tmp/store-vision-result
```

## 标定与诊断命令

```bash
# 只规划 COLMAP 命令
store-vision calibration-demo \
  --dataset ../../data/input/<dataset> \
  --output /tmp/calibration-demo \
  --dry-run

# 不运行 COLMAP，只验证人工标定与报告链路
store-vision calibration-demo \
  --dataset ../../data/input/<dataset> \
  --output /tmp/calibration-demo \
  --skip-colmap

# 鱼眼联合标定
store-vision fisheye-calibration \
  --dataset-path ../../data/input/<dataset> \
  --output-path /tmp/fisheye-calibration \
  --seed 0

# 只读分析已有 COLMAP 数据库与模型
store-vision geometry-diagnostics \
  --database-path /path/to/run/database.db \
  --image-path ../../data/input/<dataset>/screenshots \
  --model-path /path/to/run/model_txt \
  --output-path /tmp/geometry-reports

# 将已授权外部参数规范化为中间层
store-vision import-external-calibration \
  --source-path /path/to/authorized-source \
  --output-path ../../data/intermediate/<dataset>/run_<time>
```

真实 COLMAP 运行固定使用参数列表而非 shell 字符串；已有 `database.db` 的目录拒绝覆盖。几何诊断以 SQLite 只读模式访问数据库；已有报告必须显式传 `--overwrite` 才会重建。

## 数据与产物管理

- 原始输入放在 `data/input/<dataset>/`，程序不得写回。
- 中间层、2.5D 和拼接结果分别写入 `data/intermediate`、`data/map25d_output`、`data/stitching_output`。
- `data/` 默认进入版本控制；提交前必须确认数据已授权、去标识化，且每个数据集/输出层只保留当前版本需要的最新 `run_*`。
- 旧数据需要留档时移入 `archive/`；该目录不做通配忽略，但正式代码和测试不得依赖它。
- 临时实验写入 `/tmp` 或被忽略的 `outputs/`，不得混入 `data/`。

## 卸载

```bash
deactivate
rm -rf Code/StoreVision/.venv
```

该操作只移除项目虚拟环境，不修改数据目录。
