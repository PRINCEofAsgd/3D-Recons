# 测试计划

## 测试层次

```text
集成测试：完整 pipeline、CLI 工作流、GUI smoke
单元测试：loader、分辨率、标定、SfM、检测、映射、工作区和 UI 组件
静态检查：compileall、CLI help、敏感信息扫描、忽略规则与 Git 差异检查
```

## 测试夹具

`tests/fixtures/sample_store/` 为去标识化合成数据：

- 中性相机 ID 与精简的 `cali.txt`；
- 合成平面图；
- 合成相机图、白色桌面和叠加线；
- 不包含真实业务图片、账户字段、设备编号或外部项目参数。

夹具可由 `tests/generate_fixtures.py` 重新生成。

## 核心覆盖

- 数据加载、固定目录、异分辨率坐标转换与输入预检。
- 共享鱼眼 K/D、H、逐机位姿、联合 BA 和物理门禁。
- COLMAP 参数、数据库解析、相机分组、注册质量与只读几何诊断。
- 桌面检测、叠加线修复、覆盖/重叠、2.5D、人工复核和拼接。
- schema 兼容、中间层发布/发现、候选能力与业务输出 manifest。
- PyQt6 页面实例化、后台任务、来源选择和结果回填隔离。

## 执行

```bash
QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/store-vision-matplotlib python -m pytest
python -m compileall -q src tests
store-vision --help
```

验收以本次实际命令结果为准，不在文档中固化易过期的用例总数或性能数字。
