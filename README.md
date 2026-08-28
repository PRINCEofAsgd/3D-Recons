# Store Vision

Store Vision 是面向零售门店多相机画面的 Python 2.5D 地图 Demo。项目以人工标定和平面图为坐标基准，提供相机标定、白色桌面检测、跨相机覆盖与一致性分析、2.5D GeoJSON、俯视拼接，以及 PyQt6 桌面交互界面。

当前正式产品链路采用 OpenCV、SciPy 与 PyQt6。桌面流程分为“输入优化 / 2.5D 建图 / 图像拼接”三个一级页面；两个业务页既可跟随本次输入优化发布的中间层，也可分别选择已有数据集和运行版本。2.5D 使用 K/D/R/t 把桌面轮廓投到高度策略指定的世界平面，再由 H 锚定到门店平面图；页面可实时勾选自动对象和弱单相机候选，人工结果单独保存。基础、SfM 支撑加权或注册相机对照模式均可选择，SfM 失败不会阻塞基础建图。图像拼接按 K/D/R/t 执行世界地面投影、覆盖/重叠诊断和羽化融合。外部精确参数可直接导入中间层；坐标仅有假设而未独立验收时允许生成明确标记为 `geometric_trial` 的试算，不会伪装为测量真值。

## 主要文档

- 说明文档：[README.md](README.md)
- 结构文档：[docs/context/repository-structure.md](docs/context/repository-structure.md)
- 开发文档：[docs/user/development.md](docs/user/development.md)
- 操作手册：[docs/user/user.md](docs/user/user.md)
