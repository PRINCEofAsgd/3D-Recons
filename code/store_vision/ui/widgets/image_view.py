"""Zoomable / pannable image viewer using QGraphicsView."""

from __future__ import annotations

import math

from PyQt6.QtCore import QEvent, Qt, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import (
    QColor,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QWheelEvent,
    QPolygonF,
    QBrush,
)
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)


class ImageGraphicsView(QGraphicsView):
    """支持按使用场景切换导航方式的图片与图形叠加视图。"""

    clicked_image_pt = pyqtSignal(float, float)

    def __init__(
        self,
        parent=None,
        *,
        allow_double_click_fit: bool = True,
        command_only_zoom: bool = False,
    ):
        """创建图片视图，并允许精细图弹窗启用 Command-only 缩放模式。"""

        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pix_item: QGraphicsPixmapItem | None = None
        self._overlay_items: list[QGraphicsItem] = []
        self._fit_scale = 1.0
        self._zoom_shortcuts: list[QShortcut] = []
        self._allow_double_click_fit = allow_double_click_fit
        self._command_only_zoom = command_only_zoom
        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.Antialiasing
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumSize(360, 240)
        self._fit_pending = False
        self._install_zoom_shortcuts()

    def _install_zoom_shortcuts(self) -> None:
        """使用 Qt 平台标准绑定，让 macOS 正确映射到 Command +/-。"""

        zoom_in_sequences = list(
            QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomIn)
        )
        zoom_out_sequences = list(
            QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomOut)
        )
        # Qt 在 macOS 会把 Ctrl 显示并解释为 Command；补充等号键避免必须按 Shift。
        zoom_in_sequences.append(QKeySequence("Ctrl+="))
        installed: set[str] = set()
        for sequence, callback in (
            *((sequence, self.zoom_in) for sequence in zoom_in_sequences),
            *((sequence, self.zoom_out) for sequence in zoom_out_sequences),
        ):
            key = sequence.toString(QKeySequence.SequenceFormat.PortableText)
            if not key or key in installed:
                continue
            installed.add(key)
            shortcut = QShortcut(sequence, self)
            # 弹窗关闭按钮获得焦点时，Command +/- 仍应作用于同一窗口中的图片。
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._zoom_shortcuts.append(shortcut)

    def set_pixmap(self, pixmap: QPixmap):
        self._clear_overlays()
        self._scene.clear()
        if pixmap is None or pixmap.isNull():
            self._pix_item = None
            return
        self._pix_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        # Defer fit until widget is laid out and shown.
        self._fit_pending = True
        self.fit()

    def fit(self):
        if self._pix_item is None:
            return
        # If the viewport has no real size yet, defer the fit to showEvent/resizeEvent.
        vp = self.viewport()
        if vp.width() < 10 or vp.height() < 10:
            self._fit_pending = True
            return
        self.resetTransform()
        self.fitInView(self._pix_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = max(abs(self.transform().m11()), 1e-9)
        self._fit_pending = False

    def zoom_by(self, factor: float) -> None:
        """按连续倍率缩放，并限制到适应窗口比例的 10%～50 倍。"""

        if self._pix_item is None or not math.isfinite(factor) or factor <= 0:
            return
        current = max(abs(self.transform().m11()), 1e-12)
        minimum = self._fit_scale * 0.10
        maximum = self._fit_scale * 50.0
        target = min(max(current * factor, minimum), maximum)
        applied = target / current
        if abs(applied - 1.0) > 1e-6:
            self.scale(applied, applied)

    def zoom_in(self) -> None:
        """使用低敏感度固定步长放大，供 Command + 调用。"""

        self.zoom_by(1.12)

    def zoom_out(self) -> None:
        """使用低敏感度固定步长缩小，供 Command - 调用。"""

        self.zoom_by(1.0 / 1.12)

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_pending:
            self.fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_pending and self._pix_item is not None:
            self.fit()

    def add_polygon(
        self,
        pts: list[tuple[float, float]],
        color: QColor,
        width: int = 2,
        fill: QColor | None = None,
    ):
        if not pts or self._pix_item is None:
            return
        poly = QPolygonF([QPointF(x, y) for x, y in pts])
        item = self._scene.addPolygon(poly, QPen(color, width), QBrush(fill) if fill else QBrush())
        item.setZValue(10)
        self._overlay_items.append(item)

    def add_point(self, x: float, y: float, color: QColor, radius: float = 5.0):
        if self._pix_item is None:
            return
        item = self._scene.addEllipse(
            x - radius, y - radius, radius * 2, radius * 2,
            QPen(color, 1),
            QBrush(color),
        )
        item.setZValue(11)
        self._overlay_items.append(item)

    def add_pixmap_overlay(self, x: float, y: float, pm: QPixmap, scale: float = 1.0):
        if self._pix_item is None:
            return
        item = self._scene.addPixmap(pm)
        item.setOffset(x, y)
        item.setScale(scale)
        item.setZValue(5)
        self._overlay_items.append(item)
        return item

    def _clear_overlays(self):
        for it in self._overlay_items:
            self._scene.removeItem(it)
        self._overlay_items.clear()

    def clear_overlays(self):
        self._clear_overlays()

    def wheelEvent(self, event: QWheelEvent):
        """弹窗模式只平移；其他画布保持原有滚轮连续缩放。"""

        if self._pix_item is None:
            event.ignore()
            return
        if not self._command_only_zoom:
            pixel_delta_y = event.pixelDelta().y()
            angle_delta_y = event.angleDelta().y()
            if pixel_delta_y:
                factor = math.exp(max(-60, min(60, pixel_delta_y)) * 0.0025)
            elif angle_delta_y:
                factor = math.pow(1.12, angle_delta_y / 120.0)
            else:
                event.ignore()
                return
            self.zoom_by(max(0.86, min(1.16, factor)))
            event.accept()
            return

        # 精细图弹窗中，macOS 双指滚动只改变两个滚动条的位置。
        pixel_delta = event.pixelDelta()
        angle_delta = event.angleDelta()
        delta_x = pixel_delta.x() if not pixel_delta.isNull() else angle_delta.x() / 8
        delta_y = pixel_delta.y() if not pixel_delta.isNull() else angle_delta.y() / 8
        if not delta_x and not delta_y:
            event.ignore()
            return
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(horizontal.value() - int(delta_x))
        vertical.setValue(vertical.value() - int(delta_y))
        event.accept()

    def _handle_native_gesture(self, event) -> bool:
        """弹窗吞掉捏合；其他画布保留原有 macOS 原生缩放。"""

        if (
            event.type() != QEvent.Type.NativeGesture
            or event.gestureType() != Qt.NativeGestureType.ZoomNativeGesture
        ):
            return False
        if not self._command_only_zoom:
            value = max(-0.18, min(0.18, float(event.value())))
            self.zoom_by(math.exp(value))
        event.accept()
        return True

    def viewportEvent(self, event):
        """原生手势通常发送到 QGraphicsView 的 viewport。"""

        if self._handle_native_gesture(event):
            return True
        return super().viewportEvent(event)

    def event(self, event):
        """兼容由平台直接发送到视图本身的原生手势。"""

        if self._handle_native_gesture(event):
            return True
        return super().event(event)

    def mouseDoubleClickEvent(self, event):
        if (
            self._allow_double_click_fit
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.fit()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if self._pix_item is not None and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.pos())
            self.clicked_image_pt.emit(float(sp.x()), float(sp.y()))
        super().mousePressEvent(event)
