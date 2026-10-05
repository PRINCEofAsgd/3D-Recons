"""Tab 1 — calibration viewer.

Functionality
- Load store folder.
- Show floor plan with mapPoints (camera quads).
- Camera thumbnails overlaid at quad centroids; clicking a thumbnail opens a
  zoomable popup with that camera's full image.
- Draw polygon on floor plan -> nearest camera popup with the polygon
  back-projected to the camera image.
"""

from __future__ import annotations

from pathlib import Path

import cv2
from PyQt6.QtCore import QSize, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from store_vision.calibration.homography import floor_polygon_to_camera_polygon
from store_vision.config import StoreConfig
from store_vision.data import load_gui_dataset_folder
from store_vision.data.loader import find_calibration_path
from store_vision.data.models import CameraCalibration, StoreDataset
from store_vision.geometry.projection import (
    best_camera_for_floor_polygon,
    nearest_camera_to_floor_pt,
    polygon_centroid,
)
from store_vision.ui.qt_utils import cv_to_qpixmap
from store_vision.ui.widgets import PolygonImageView, ZoomImageDialog


_THUMB_COLORS = [
    QColor(255, 80, 80), QColor(80, 200, 80), QColor(80, 120, 255),
    QColor(255, 200, 0), QColor(255, 120, 200), QColor(120, 220, 220),
    QColor(220, 160, 80), QColor(160, 80, 220), QColor(80, 220, 160),
    QColor(200, 80, 100),
]


class CalibrationTab(QWidget):
    # 将已校验的数据集交给主窗口，由主窗口在后台线程执行完整流水线。
    run_requested = pyqtSignal(object)  # StoreDataset
    # 输入变化时同步给 COLMAP 标定优化页，避免用户重复选择整合目录。
    dataset_changed = pyqtSignal(object)  # StoreDataset

    def __init__(
        self,
        cfg: StoreConfig,
        parent=None,
        *,
        show_input_controls: bool = True,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self.show_input_controls = show_input_controls
        self.dataset: StoreDataset | None = None
        self.floor_plan_path: Path | None = None
        self.camera_image_paths: list[Path] = []
        self.cali_path: Path | None = None
        self.scale_path: Path | None = None
        self.output_dir: Path | None = None
        self.output_dir_used = False
        self._image_sizes: dict[str, tuple[int, int]] = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        inputs = QGridLayout()
        self.btn_folder = QPushButton("选择数据集")
        self.btn_folder.clicked.connect(self._pick_folder)
        self.btn_open_output = QPushButton("打开输出目录")
        self.btn_open_output.clicked.connect(self._open_output_dir)
        self.btn_open_output.setEnabled(False)
        self.btn_run = QPushButton("运行完整处理")
        self.btn_run.clicked.connect(self._run_calibration)
        self.lbl = QLabel(
            "请选择数据集：支持 cali.txt/cali.json、scale.txt/scale.json、"
            "floorplan.png/jpg/jpeg 和 screenshots/"
        )
        self.lbl.setWordWrap(True)
        inputs.addWidget(self.btn_folder, 0, 0)
        inputs.addWidget(self.btn_run, 0, 1)
        inputs.addWidget(self.btn_open_output, 0, 2)
        inputs.addWidget(self.lbl, 1, 0, 1, 3)
        if self.show_input_controls:
            layout.addLayout(inputs)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.floor_view = PolygonImageView()
        self.floor_view.polygon_finished.connect(self._on_polygon_finished)
        split.addWidget(self.floor_view)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("相机缩略图（点击查看精细图）"))
        self.cam_list = QListWidget()
        self.cam_list.setIconSize(QSize(160, 90))
        self.cam_list.itemClicked.connect(self._open_camera_popup)
        rl.addWidget(self.cam_list, 1)
        rl.addWidget(QLabel("最近一次状态"))
        self.status_lbl = QLabel("—")
        self.status_lbl.setWordWrap(True)
        rl.addWidget(self.status_lbl)
        split.addWidget(right)
        split.setSizes([900, 300])
        layout.addWidget(split, 1)

    # ------------------------------------------------------------------ load
    def _pick_folder(self):
        """选择符合固定结构的完整数据集目录。"""
        folder = QFileDialog.getExistingDirectory(self, "选择数据集")
        if not folder:
            return
        self.load_folder(folder)

    def _open_output_dir(self):
        """完整处理创建输出目录后，通过系统文件管理器打开结果。"""
        if self.output_dir is not None and self.output_dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def load_folder(self, folder: str):
        """按固定数据集结构读取输入，并将其显示在前端。"""
        try:
            selected = Path(folder).expanduser().resolve()
            output_root = Path(self.cfg.output_root).expanduser().resolve()
            if selected == output_root or selected.is_relative_to(output_root):
                raise ValueError(
                    "不能把 outputs 结果目录作为数据集。"
                    "请选择包含 cali.txt/cali.json、scale.txt/scale.json、"
                    "floorplan 和 screenshots/ 的原始数据集目录。"
                )
            dataset = load_gui_dataset_folder(selected)
        except Exception as e:
            QMessageBox.critical(self, "加载失败", str(e))
            return
        self.floor_plan_path = Path(dataset.floor_plan_path) if dataset.floor_plan_path else None
        self.camera_image_paths = [
            Path(camera.image_path)
            for camera in dataset.camera_list()
            if camera.image_path
        ]
        self.cali_path = find_calibration_path(
            dataset.root, include_calibration_subdir=False
        )
        self.scale_path = Path(dataset.scale_path) if dataset.scale_path else None
        self.output_dir = None
        self.output_dir_used = False
        self.btn_open_output.setEnabled(False)
        self.apply_dataset(dataset)
        self._update_input_summary()

    def apply_dataset(self, dataset: StoreDataset, *, notify: bool = True):
        """将已解析的数据集应用到标定视图。"""
        self.dataset = dataset
        self._refresh()
        if notify:
            self.dataset_changed.emit(dataset)

    def plan_output_directory(self, output_dir: str | Path):
        """记录本数据集的固定输出目录，但在完整处理创建前保持打开按钮灰化。"""

        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir_used = False
        self.btn_open_output.setEnabled(False)
        self._update_input_summary()

    def mark_output_directory_created(self, output_dir: str | Path):
        """完整处理已创建目录后开放系统文件管理器入口。"""

        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir_used = True
        self.btn_open_output.setEnabled(self.output_dir.is_dir())
        self._update_input_summary()

    def _update_input_summary(self):
        """展示固定输入的识别结果和自动规划的输出目录。"""
        dataset_name = Path(self.dataset.root).name if self.dataset else "未选择"
        plan = self.floor_plan_path.name if self.floor_plan_path else "缺失"
        cali = self.cali_path.name if self.cali_path else "缺失"
        scale = self.scale_path.name if self.scale_path else "缺失"
        cameras = f"{len(self.camera_image_paths)} 张" if self.camera_image_paths else "未选择"
        output = str(self.output_dir) if self.output_dir else "选择数据集后自动确定"
        output_state = "已创建" if self.output_dir_used else "运行时创建"
        self.lbl.setText(
            f"数据集：{dataset_name} ｜ 平面图：{plan} ｜ screenshots：{cameras} ｜ "
            f"标定：{cali} + {scale} ｜ 输出（{output_state}）：{output}"
        )

    def _refresh(self):
        if self.dataset is None:
            return
        if not self.dataset.floor_plan_path:
            self.floor_view.set_pixmap(None)
            self.status_lbl.setText(
                "未找到 floorplan.png/jpg/jpeg，请检查所选数据集固定目录结构"
            )
            return
        img = cv2.imread(self.dataset.floor_plan_path)
        if img is None:
            return
        pm = cv_to_qpixmap(img)
        self.floor_view.set_pixmap(pm)
        fw, fh = self.dataset.floor_plan_size or (pm.width(), pm.height())

        for i, calib in enumerate(self.dataset.camera_list()):
            color = _THUMB_COLORS[i % len(_THUMB_COLORS)]
            pts = [(p.x / 100.0 * fw, p.y / 100.0 * fh) for p in calib.map_points]
            self.floor_view.add_polygon(pts, color, 3)
            for x, y in pts:
                self.floor_view.add_point(x, y, color, 6)
            if calib.image_path and Path(calib.image_path).exists():
                thumb = cv2.imread(calib.image_path)
                if thumb is not None:
                    cx, cy = polygon_centroid(pts)
                    th = self.cfg.thumb_max_px
                    pmt = cv_to_qpixmap(thumb, th)
                    self.floor_view.add_pixmap_overlay(
                        cx - pmt.width() / 2, cy - pmt.height() / 2, pmt
                    )

        self.cam_list.clear()
        self._image_sizes.clear()
        for c in self.dataset.camera_list():
            mark = "✓" if c.image_path else "✗"
            it = QListWidgetItem(f"{mark} {c.name} ({c.device_serial})")
            if c.image_path and Path(c.image_path).exists():
                im = cv2.imread(c.image_path)
                if im is not None:
                    it.setIcon(QIcon(cv_to_qpixmap(im, 160)))
                    self._image_sizes[c.device_serial] = (im.shape[1], im.shape[0])
            self.cam_list.addItem(it)

        self.status_lbl.setText(
            f"已加载 {len(self.dataset.cameras)} 个相机，平面图 {fw}×{fh}px"
        )

    # ------------------------------------------------------------------ run
    def _run_calibration(self):
        """校验数据集后，请主窗口创建固定输出目录并执行完整处理。"""
        if self.dataset is None:
            QMessageBox.information(self, "提示", "请先选择数据集")
            return
        self.status_lbl.setText("正在后台执行标定、检测、2.5D 与拼接…")
        self.run_requested.emit(self.dataset)

    # ------------------------------------------------------------- popup
    def _open_camera_popup(self, item: QListWidgetItem):
        if self.dataset is None:
            return
        idx = self.cam_list.row(item)
        cams = self.dataset.camera_list()
        if not (0 <= idx < len(cams)):
            return
        self._show_camera_popup(cams[idx])

    def _show_camera_popup(
        self,
        calib: CameraCalibration,
        floor_polygon: list[tuple[float, float]] | None = None,
    ):
        if not calib.image_path:
            QMessageBox.information(self, "缺图", f"{calib.name} 没有对应图像")
            return
        img = cv2.imread(calib.image_path)
        if img is None:
            return
        pm = cv_to_qpixmap(img)
        polys = [
            (
                [(p.x, p.y) for p in calib.camera_points],
                QColor(255, 80, 80),
                2,
            )
        ]
        if floor_polygon is not None:
            cam_poly = floor_polygon_to_camera_polygon(calib, floor_polygon)
            if cam_poly is not None and len(cam_poly) >= 3:
                polys.append(
                    (
                        [(float(p[0]), float(p[1])) for p in cam_poly],
                        QColor(0, 255, 128),
                        3,
                    )
                )
        title = f"{calib.name}  ({calib.device_serial})"
        dlg = ZoomImageDialog(title, pm, polys, self)
        dlg.exec()

    def _on_polygon_finished(self, poly: list):
        if self.dataset is None or len(poly) < 3:
            return
        cx, cy = polygon_centroid(poly)
        # Prefer the camera whose back-projected image polygon is closest to
        # the camera image center (least distortion / best resolution).
        best = best_camera_for_floor_polygon(
            self.dataset, poly, image_sizes=self._image_sizes
        )
        if best is not None:
            calib, score = best
            reason = f"图像质心偏离中心 {score*100:.1f}% 对角线"
        else:
            calib = nearest_camera_to_floor_pt(self.dataset, cx, cy)
            score = None
            reason = "回退：平面距离最近"
        if calib is None:
            self.status_lbl.setText("找不到匹配相机")
            return
        idx = self.dataset.camera_list().index(calib)
        self.cam_list.setCurrentRow(idx)
        self.status_lbl.setText(
            f"多边形质心 ({cx:.0f},{cy:.0f}) → {calib.name}（{reason}）"
        )
        self._show_camera_popup(calib, floor_polygon=poly)
