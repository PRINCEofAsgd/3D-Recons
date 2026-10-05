"""桌面运行实例的品牌名称与 Qt 身份设置。"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication


APPLICATION_NAME = "Vion Recons"
MAIN_WINDOW_TITLE = f"{APPLICATION_NAME} — 输入优化 / 2.5D / 图像拼接"


def apply_application_identity(application: QApplication | None) -> bool:
    """统一设置 Qt 内部应用名和面向用户的显示名。

    ``applicationName`` 供 Qt 和桌面环境识别运行实例，
    ``applicationDisplayName`` 用于 macOS 菜单等面向用户的位置。
    """
    if application is None:
        return False
    application.setApplicationName(APPLICATION_NAME)
    application.setApplicationDisplayName(APPLICATION_NAME)
    return True
