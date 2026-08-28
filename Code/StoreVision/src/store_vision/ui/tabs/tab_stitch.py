"""Tab 3 — top floor plan + bottom stitched mosaic, polygon link."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from store_vision.config import StoreConfig
from store_vision.mapping import stitch_cameras_to_floor
from store_vision.pipeline import PipelineResult
from store_vision.ui.qt_utils import cv_to_qpixmap
from store_vision.ui.widgets import ImageGraphicsView, PolygonImageView


class StitchTab(QWidget):
    def __init__(self, cfg: StoreConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._result: PipelineResult | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_stitch = QPushButton("生成 / 刷新拼接图")
        self.btn_stitch.clicked.connect(self._run_stitch)
        self.btn_open = QPushButton("打开拼接图…")
        self.btn_open.clicked.connect(self._open_saved)
        self.btn_clear_poly = QPushButton("清除多边形")
        self.btn_clear_poly.clicked.connect(self._clear_polys)
        self.lbl = QLabel("上方平面图：左键加点 / 右键完成 — 下方拼接图自动同步")
        bar.addWidget(self.btn_stitch)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_clear_poly)
        bar.addWidget(self.lbl, 1)
        layout.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        # Top: floor plan with polygon tool
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("平面图（在此绘制多边形）"))
        self.floor_view = PolygonImageView()
        self.floor_view.polygon_finished.connect(self._on_polygon_finished)
        self.floor_view.polygon_changed.connect(self._on_polygon_changed)
        tl.addWidget(self.floor_view, 1)
        split.addWidget(top)

        # Bottom: mosaic
        bot = QWidget()
        bl = QVBoxLayout(bot)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QLabel("多相机拼接图"))
        self.mosaic_view = ImageGraphicsView()
        bl.addWidget(self.mosaic_view, 1)
        split.addWidget(bot)
        split.setSizes([350, 650])
        layout.addWidget(split, 1)

    def set_pipeline_result(self, result: PipelineResult):
        self._result = result
        if result.floor_image is not None:
            self.floor_view.set_pixmap(cv_to_qpixmap(result.floor_image))
        if result.stitch_image is not None:
            self.mosaic_view.set_pixmap(cv_to_qpixmap(result.stitch_image))
        if result.floor_image is not None and result.stitch_image is not None:
            self.lbl.setText(
                f"floor {result.floor_image.shape[1]}×{result.floor_image.shape[0]} | "
                f"stitch {result.stitch_image.shape[1]}×{result.stitch_image.shape[0]}"
            )

    def _run_stitch(self):
        if not self._result or not self._result.dataset:
            QMessageBox.information(self, "提示", "请先在标定 Tab 加载并运行 pipeline")
            return
        try:
            floor, mosaic = stitch_cameras_to_floor(
                self._result.dataset,
                self.cfg,
                (self._result.output_dir or Path("output")) / "stitch",
            )
            self.floor_view.set_pixmap(cv_to_qpixmap(floor))
            self.mosaic_view.set_pixmap(cv_to_qpixmap(mosaic))
            self.lbl.setText(
                f"floor {floor.shape[1]}×{floor.shape[0]} | "
                f"stitch {mosaic.shape[1]}×{mosaic.shape[0]}"
            )
        except Exception as e:
            QMessageBox.critical(self, "拼接失败", str(e))

    def _open_saved(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开拼接图", str(self.cfg.output_root), "Images (*.jpg *.png)"
        )
        if not path:
            return
        img = cv2.imread(path)
        if img is not None:
            self.mosaic_view.set_pixmap(cv_to_qpixmap(img))

    def _clear_polys(self):
        self.floor_view.reset_polygon()
        self.floor_view.clear_overlays()
        self.mosaic_view.clear_overlays()

    def _on_polygon_changed(self, pts: list):
        # Live sync: redraw partial polygon on mosaic.
        self.mosaic_view.clear_overlays()
        if len(pts) >= 1:
            color = QColor(0, 200, 255)
            for x, y in pts:
                self.mosaic_view.add_point(x, y, color, 5)
            if len(pts) >= 2:
                # Open polyline as poly with no fill
                self.mosaic_view.add_polygon(pts, color, 2)

    def _on_polygon_finished(self, pts: list):
        self.mosaic_view.clear_overlays()
        if len(pts) >= 3:
            self.mosaic_view.add_polygon(pts, QColor(0, 255, 128), 3, fill=QColor(0, 255, 128, 40))
            for x, y in pts:
                self.mosaic_view.add_point(x, y, QColor(255, 80, 80), 6)
