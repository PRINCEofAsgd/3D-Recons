"""一级“输入优化”页：统一选择原始输入并发布中间层。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from store_vision.config import StoreConfig
from store_vision.data import load_gui_dataset_folder
from store_vision.data.loader import find_calibration_path
from store_vision.data.models import StoreDataset
from store_vision.ui.tabs.tab_calibration_optimization import (
    CalibrationOptimizationTab,
)


class InputTab(QWidget):
    """把原始输入选择和估计/拟合/优化收敛到第一个一级页面。"""

    dataset_changed = pyqtSignal(object)  # StoreDataset

    def __init__(self, cfg: StoreConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.dataset: StoreDataset | None = None
        self.output_dir: Path | None = None
        self.cali_path: Path | None = None
        self.scale_path: Path | None = None
        self.optimization = CalibrationOptimizationTab(cfg)
        self._build_ui()

    def _build_ui(self) -> None:
        """构建公共输入顶栏，并把标定优化工作区放在其下。"""

        layout = QVBoxLayout(self)
        controls = QGridLayout()
        self.btn_folder = QPushButton("选择数据集")
        self.btn_folder.clicked.connect(self._pick_folder)
        self.summary_label = QLabel(
            "请选择 data/input/<数据集> 或兼容的旧数据集目录：支持 "
            "cali.txt/cali.json、scale.txt/scale.json、"
            "floorplan.png/jpg/jpeg 和 screenshots/。"
        )
        self.summary_label.setWordWrap(True)
        controls.addWidget(self.btn_folder, 0, 0)
        controls.addWidget(self.summary_label, 1, 0, 1, 2)
        layout.addLayout(controls)
        layout.addWidget(self.optimization, 1)

    def _pick_folder(self) -> None:
        """选择完整数据集目录并交给固定结构加载器校验。"""

        folder = QFileDialog.getExistingDirectory(self, "选择数据集")
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder: str | Path) -> None:
        """读取数据集并原子通知输出展示页和标定工作区。"""

        try:
            selected = Path(folder).expanduser().resolve()
            rejected_roots = (
                Path(self.cfg.output_root).expanduser().resolve(),
                Path(self.cfg.workspace_data_root).expanduser().resolve()
                / "intermediate",
                Path(self.cfg.workspace_data_root).expanduser().resolve()
                / "map25d_output",
                Path(self.cfg.workspace_data_root).expanduser().resolve()
                / "stitching_output",
            )
            if any(
                selected == root or selected.is_relative_to(root)
                for root in rejected_roots
            ):
                raise ValueError(
                    "不能把中间层或业务输出目录作为输入。请选择 "
                    "data/input 下包含标定文件、平面图和 screenshots/ "
                    "的原始数据集目录。"
                )
            dataset = load_gui_dataset_folder(selected)
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", str(exc))
            return

        self.dataset = dataset
        self.cali_path = find_calibration_path(
            selected, include_calibration_subdir=False
        )
        self.scale_path = Path(dataset.scale_path) if dataset.scale_path else None
        self.output_dir = None
        self._update_summary()
        self.dataset_changed.emit(dataset)

    def plan_output_directory(self, output_dir: str | Path) -> None:
        """兼容旧调用名：记录当前数据集的中间层根目录。"""

        self.output_dir = Path(output_dir).expanduser().resolve()
        self._update_summary()

    def mark_output_directory_created(self, output_dir: str | Path) -> None:
        """兼容旧调用名：记录已创建的中间层根目录。"""

        self.output_dir = Path(output_dir).expanduser().resolve()
        self._update_summary()

    def _update_summary(self) -> None:
        """显示实际采用的输入文件，明确 JSON/TXT 兼容结果。"""

        name = Path(self.dataset.root).name if self.dataset else "未选择"
        cameras = len(self.dataset.cameras) if self.dataset else 0
        floor = (
            Path(self.dataset.floor_plan_path).name
            if self.dataset and self.dataset.floor_plan_path
            else "缺失"
        )
        output = str(self.output_dir) if self.output_dir else "选择数据集后自动确定"
        errors = self.dataset.resolution_errors() if self.dataset else []
        resolution_state = (
            f"输入预检：阻止运行（{len(errors)} 项坐标/分辨率错误）"
            if errors
            else "输入预检：可运行"
        )
        self.summary_label.setText(
            f"数据集：{name} ｜ 标定："
            f"{self.cali_path.name if self.cali_path else '缺失'} + "
            f"{self.scale_path.name if self.scale_path else '缺失'} ｜ "
            f"平面图：{floor} ｜ 相机：{cameras} 台 ｜ {resolution_state} ｜ "
            f"中间层根目录：{output}"
        )
