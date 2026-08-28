# Store Vision Python project

安装、数据格式和所有运行命令以仓库根目录 [README](../../README.md) 为准。

正式入口只有 `store-vision`，正式业务代码只有 `src/store_vision/`，正式测试只有 `tests/`。本目录的 `main.py` 只是兼容薄入口。

```bash
.venv/bin/python -m pip install -e '.[dev]'
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```
