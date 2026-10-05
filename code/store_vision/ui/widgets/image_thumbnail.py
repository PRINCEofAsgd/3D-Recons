"""小型诊断图入口；点击后在可缩放弹窗中查看原图。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QToolButton


class ImageThumbnail(QToolButton):
    """固定占用很小面积的原图弹窗入口。"""

    _BUTTON_SIZE = QSize(148, 100)
    _ICON_SIZE = QSize(132, 68)

    def __init__(self, placeholder: str = "尚无图片", parent=None):
        super().__init__(parent)
        self._source_path: Path | None = None
        self._source_pixmap = QPixmap()
        self._empty_text = placeholder
        self.setFixedSize(self._BUTTON_SIZE)
        self.setIconSize(self._ICON_SIZE)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setText("暂无图片")
        self.setToolTip(placeholder)
        self.setEnabled(False)
        self.setStyleSheet(
            "QToolButton { background:#f7f8fa; border:1px solid #c8cdd4; "
            "border-radius:6px; padding:4px; } "
            "QToolButton:hover { background:#eef5ff; border-color:#6da3e5; }"
        )
        self.clicked.connect(self._open_dialog)

    @property
    def source_path(self) -> Path | None:
        """返回当前入口对应的原图路径，便于界面状态和测试检查。"""

        return self._source_path

    def set_image(self, path: str | Path | None, empty_text: str) -> None:
        """加载原图，并只在固定小按钮内生成缩略图。"""

        self._empty_text = empty_text
        self._source_path = Path(path).expanduser().resolve() if path else None
        self._source_pixmap = (
            QPixmap(str(self._source_path))
            if self._source_path is not None and self._source_path.is_file()
            else QPixmap()
        )
        if self._source_pixmap.isNull():
            self.setIcon(QIcon())
            self.setText("暂无图片")
            self.setToolTip(
                f"无法读取图片：{self._source_path}"
                if self._source_path is not None and self._source_path.is_file()
                else empty_text
            )
            self.setEnabled(False)
            return

        preview = self._source_pixmap.scaled(
            self._ICON_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setIcon(QIcon(preview))
        self.setText("查看精细图")
        self.setToolTip("点击打开原分辨率图片")
        self.setEnabled(True)

    def clear_image(self, empty_text: str | None = None) -> None:
        """清除原图引用并恢复紧凑占位入口。"""

        self.set_image(None, empty_text or self._empty_text)

    def _open_dialog(self) -> None:
        """打开仅由 Command +/- 缩放、双指或鼠标平移的精细图弹窗。"""

        if self._source_pixmap.isNull():
            return
        from store_vision.ui.widgets.popup_zoom import ZoomImageDialog

        title = self._source_path.name if self._source_path else "图片详情"
        ZoomImageDialog(title, self._source_pixmap, parent=self).exec()
