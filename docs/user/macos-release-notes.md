# macOS 版本说明

## 当前版本

`V1.0.11_20261009`

## 整体功能

- 多相机固定结构数据加载与输入预检。
- 同型号鱼眼共享 K/D、逐机位姿拟合与联合 BA。
- 版本化中间层、2.5D GeoJSON、对象复核和参数驱动拼接。
- PyQt6“输入优化 / 2.5D 建图 / 图像拼接”三页界面。
- 可选 COLMAP/SfM 注册证据与只读几何诊断。

## 本次更新

- 本机完成 Phase 4 Kubernetes/Argo 合成 DAG 验收，两个业务结果与 MySQL 状态可查询；补齐云端镜像、固定运行依赖、Workflow 资源配置和 executor 权限。

## 平台状态

- Apple Silicon ARM64：源码与自动化测试已验证。
- Intel Mac：未验证。
- 当前通过 Python 虚拟环境运行，不提供 `.app` 安装包。
