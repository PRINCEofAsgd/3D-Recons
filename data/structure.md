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
- 数据包默认只保存在本地，不纳入版本控制；仅跟踪本目录及各层 `structure.md` 与 `input/apple_exam/` 示例。
- 真实输入与运行产物仍按四层路径放置，运行包不得覆盖。
- 历史运行可以移入 `archive/`，正式代码和测试不得依赖。

各层字段说明：

- [input/structure.md](input/structure.md)
- [intermediate/structure.md](intermediate/structure.md)
- [map25d_output/structure.md](map25d_output/structure.md)
- [stitching_output/structure.md](stitching_output/structure.md)
