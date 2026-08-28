"""Image view that lets the user draw a polygon: left = vertex, right = finish."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QPointF
from PyQt6.QtGui import QBrush, QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QGraphicsItem

from store_vision.ui.widgets.image_view import ImageGraphicsView


class PolygonImageView(ImageGraphicsView):
    polygon_finished = pyqtSignal(list)  # list[(x, y)]
    polygon_changed = pyqtSignal(list)  # list[(x, y)] in-progress

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragMode(ImageGraphicsView.DragMode.NoDrag)
        self._poly_pts: list[tuple[float, float]] = []
        self._poly_items: list[QGraphicsItem] = []

    def reset_polygon(self):
        self._poly_pts.clear()
        self._redraw()
        self.polygon_changed.emit([])

    def current_polygon(self) -> list[tuple[float, float]]:
        return list(self._poly_pts)

    def _redraw(self):
        for it in self._poly_items:
            self.scene().removeItem(it)
        self._poly_items.clear()
        if not self._poly_pts:
            return
        pen_line = QPen(QColor(0, 200, 255), 2)
        pen_dot = QPen(QColor(255, 60, 60), 1)
        brush_dot = QBrush(QColor(255, 60, 60))
        n = len(self._poly_pts)
        for i in range(n):
            x, y = self._poly_pts[i]
            d = self.scene().addEllipse(x - 5, y - 5, 10, 10, pen_dot, brush_dot)
            d.setZValue(20)
            self._poly_items.append(d)
            if i + 1 < n:
                x2, y2 = self._poly_pts[i + 1]
                ln = self.scene().addLine(x, y, x2, y2, pen_line)
                ln.setZValue(19)
                self._poly_items.append(ln)
        if n >= 3:
            poly = QPolygonF([QPointF(x, y) for x, y in self._poly_pts])
            outline = self.scene().addPolygon(poly, pen_line, QBrush(QColor(0, 200, 255, 40)))
            outline.setZValue(18)
            self._poly_items.append(outline)

    def mousePressEvent(self, event):
        if self._pix_item is not None:
            sp = self.mapToScene(event.pos())
            x, y = float(sp.x()), float(sp.y())
            if event.button() == Qt.MouseButton.LeftButton:
                self._poly_pts.append((x, y))
                self._redraw()
                self.polygon_changed.emit(list(self._poly_pts))
                return
            if event.button() == Qt.MouseButton.RightButton:
                if len(self._poly_pts) >= 3:
                    poly = list(self._poly_pts)
                    self._poly_pts.clear()
                    self._redraw()
                    self.polygon_finished.emit(poly)
                else:
                    self._poly_pts.clear()
                    self._redraw()
                    self.polygon_changed.emit([])
                return
        super().mousePressEvent(event)
