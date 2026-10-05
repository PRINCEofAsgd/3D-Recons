"""Resizable, zoomable popup window for examining one camera image."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from store_vision.ui.widgets.image_view import ImageGraphicsView


class ZoomImageDialog(QDialog):
    """只允许 Command +/- 改变缩放比例的精细图片弹窗。"""

    def __init__(
        self,
        title: str,
        pixmap: QPixmap,
        polygons: list[tuple[list[tuple[float, float]], QColor, int]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1200, 800)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.lbl = QLabel("⌘ +/- 缩放 / 双指或鼠标拖动图片")
        self.btn_close = QPushButton("关闭")
        bar.addWidget(self.lbl, 1)
        bar.addWidget(self.btn_close)
        layout.addLayout(bar)

        # 弹窗关闭双击复位，避免出现 Command +/- 之外的缩放入口。
        self.view = ImageGraphicsView(
            allow_double_click_fit=False,
            command_only_zoom=True,
        )
        layout.addWidget(self.view, 1)
        self.view.set_pixmap(pixmap)
        if polygons:
            for pts, color, w in polygons:
                self.view.add_polygon(pts, color, w)

        self.btn_close.clicked.connect(self.accept)
        self._close_shortcuts: list[QShortcut] = []
        self._install_close_shortcuts()

    def _install_close_shortcuts(self) -> None:
        """注册平台标准关闭键；macOS 的标准绑定包含 Command+W。"""

        installed: set[str] = set()
        for sequence in QKeySequence.keyBindings(QKeySequence.StandardKey.Close):
            key = sequence.toString(QKeySequence.SequenceFormat.PortableText)
            if not key or key in installed:
                continue
            installed.add(key)
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(self.reject)
            self._close_shortcuts.append(shortcut)
