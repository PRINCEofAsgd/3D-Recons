# 统一数据工作区

```text
data/
├── input/<dataset>/
├── intermediate/<dataset>/run_*/
├── map25d_output/<dataset>/run_*/
└── stitching_output/<dataset>/run_*/
```

`data/` 默认纳入版本控制。只允许放入已授权、完成去标识化且属于当前项目的数据；同一数据集和输出层只保留当前版本需要的最新运行。旧运行若需要留存，应移入同样可跟踪的 `archive/`，不得与当前运行混用。

当前数据恢复状态与缺失边界见[项目上下文](../docs/context/project-context.md)。详细字段见 [structure.md](structure.md)。
