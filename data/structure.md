# data 工作区结构

四层数据按“输入—中间合同—业务输出”分离：

```text
data/
├── input/<dataset>/
├── intermediate/<dataset>/run_<time>/
├── map25d_output/<dataset>/run_<time>/
└── stitching_output/<dataset>/run_<time>/
```

- 原始输入只读。
- 中间层和业务输出使用唯一 `run_*`，不得覆盖。
- 两个业务页只消费中间层，不读取或修改对方输出。
- 同一数据集、同一输出层提交时只保留当前版本所需的最新运行。
- 历史运行移入可跟踪的 `archive/`，正式代码和测试不得依赖。

各层字段说明：

- [input/structure.md](input/structure.md)
- [intermediate/structure.md](intermediate/structure.md)
- [map25d_output/structure.md](map25d_output/structure.md)
- [stitching_output/structure.md](stitching_output/structure.md)
