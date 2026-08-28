"""桌面应用图标资源定位与 Qt 安装。"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QWidget


def application_icon_path() -> Path:
    """返回随 ``store_vision`` 包安装的 1024×1024 PNG 图标路径。"""
    resource = files("store_vision").joinpath("assets", "app_icon.png")
    return Path(str(resource))


def load_application_icon() -> QIcon:
    """加载应用图标；资源异常时返回空图标，让 Qt 使用平台默认回退。"""
    path = application_icon_path()
    return QIcon(str(path)) if path.is_file() else QIcon()


def apply_application_icon(
    application: QApplication | None,
    window: QWidget | None = None,
) -> bool:
    """同时设置进程级图标和窗口图标。

    进程级图标用于 macOS Dock/应用切换器以及其他桌面环境的任务栏；窗口
    图标确保直接构造 ``MainWindow`` 的测试或嵌入式入口也采用同一资源。
    """
    icon = load_application_icon()
    if icon.isNull():
        return False
    if application is not None:
        application.setWindowIcon(icon)
    if window is not None:
        window.setWindowIcon(icon)
    return True
