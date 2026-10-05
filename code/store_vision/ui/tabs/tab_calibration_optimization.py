"""“输入”页处理工作区：鱼眼联合标定、Sfm 诊断和报告。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QComboBox,
)

from store_vision.config import StoreConfig
from store_vision.data.loader import find_calibration_path
from store_vision.ui.calibration_workspace import (
    CalibrationWorkspaceSnapshot,
    create_unique_analysis_directory,
    create_unique_run_directory,
    find_project_root,
    load_calibration_workspace,
)
from store_vision.ui.widgets import ImageThumbnail

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationTaskRequest:
    """GUI 后台任务的稳定输入，避免在线程中读取可变控件。"""

    operation: str
    dataset_path: Path
    experiment_path: Path | None = None
    output_root: Path | None = None
    shared_result_path: Path | None = None
    config: StoreConfig | None = None


class CalibrationTaskWorker(QThread):
    """在线程中直接调用正式工作流，保持 GUI 响应。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, request: CalibrationTaskRequest, parent=None):
        """冻结本轮请求，避免后台线程读取变化中的 GUI 控件。"""

        super().__init__(parent)
        self.request = request

    @staticmethod
    def _image_root(dataset: Path) -> Path:
        """优先定位 GUI screenshots，并兼容标准分层目录和旧平铺目录。"""

        for candidate in (
            dataset / "screenshots",
            dataset / "cameras" / "images",
            dataset / "images",
            dataset,
        ):
            if candidate.is_dir() and any(
                path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                for path in candidate.rglob("*")
            ):
                return candidate
        raise FileNotFoundError(f"数据集中没有相机图片：{dataset}")

    @staticmethod
    def _calibration_config(dataset: Path) -> Path:
        """按正式优先级寻找人工标定配置。"""

        calibration = find_calibration_path(dataset)
        if calibration is not None:
            return calibration
        manual_pose = dataset / "cameras" / "manual_pose.json"
        if manual_pose.is_file():
            return manual_pose
        raise FileNotFoundError(
            f"数据集中没有 cali.txt、cali.json 或 manual_pose.json：{dataset}"
        )

    def run(self) -> None:
        """执行鱼眼联合标定、新 COLMAP 实验或只读匹配诊断。"""

        try:
            if self.request.operation == "shared_intrinsics":
                self._run_shared_intrinsics()
            elif self.request.operation == "new_experiment":
                self._run_new_experiment()
            elif self.request.operation == "available_analysis":
                self._run_available_analysis()
            else:
                raise ValueError(f"未知标定任务：{self.request.operation}")
        except Exception as exc:  # GUI 边界统一转为可读失败消息。
            logger.exception("calibration GUI task failed")
            self.failed.emit(str(exc))

    def _run_shared_intrinsics(self) -> None:
        """执行共享鱼眼标定，并把结果发布成版本化中间层数据包。"""

        if self.request.output_root is None:
            raise ValueError("请先选择结果输出根目录")
        from store_vision.calibration.shared_calibration import (
            SharedCalibrationConfig,
            run_shared_intrinsics_calibration,
        )

        run_path = create_unique_run_directory(self.request.output_root)
        project_root = find_project_root(self.request.dataset_path) or self.request.dataset_path.parent
        self.progress.emit(f"正在运行共享鱼眼 K/D、位姿拟合与 BA：{run_path.name}")
        report = run_shared_intrinsics_calibration(
            self.request.dataset_path,
            run_path,
            project_root=project_root,
            config=SharedCalibrationConfig(seed=0),
        )
        from store_vision.data.workspace import publish_intermediate_package

        publish_intermediate_package(
            self.request.dataset_path,
            run_path,
            report,
            cfg=self.request.config,
            shared_result_path=run_path,
        )
        self.done.emit(
            {
                "operation": self.request.operation,
                "dataset_path": self.request.dataset_path,
                "shared_result_path": run_path,
                "intermediate_path": run_path,
                "selected_model": report.get("selection", {}).get("selected_model"),
                "message": (
                    "鱼眼联合标定完成并已发布中间层，模型 "
                    f"{report.get('selection', {}).get('selected_model') or '无'}"
                ),
            }
        )

    def _run_new_experiment(self) -> None:
        """在唯一目录中运行完整 V0.2 COLMAP 对照实验。"""

        if self.request.output_root is None:
            raise ValueError("请先选择新实验输出根目录")
        from store_vision.calibration.workflow import run_calibration_workflow

        run_path = create_unique_run_directory(self.request.output_root)
        self.progress.emit(f"正在创建 COLMAP 实验：{run_path.name}")
        result = run_calibration_workflow(
            self.request.dataset_path,
            run_path,
            fisheye_calibration=self.request.shared_result_path,
        )
        derived_intermediate = None
        if self.request.shared_result_path is not None:
            # SfM 只形成新的证据派生包；父中间层保持不可变，也不在这里
            # 自动触发 2.5D 或拼接业务。
            from store_vision.data.workspace import publish_sfm_derived_package

            derived_intermediate = publish_sfm_derived_package(
                self.request.shared_result_path,
                run_path,
                self.request.shared_result_path.parent,
            )
        self.done.emit(
            {
                "operation": self.request.operation,
                "dataset_path": self.request.dataset_path,
                "experiment_path": run_path,
                "intermediate_path": derived_intermediate,
                "report_path": result.report_path,
                "message": f"COLMAP 实验完成，状态：{result.colmap_status}",
            }
        )

    def _run_available_analysis(self) -> None:
        """在唯一分析目录中运行只读匹配与稀疏几何诊断。"""

        experiment = self.request.experiment_path
        if experiment is None:
            raise ValueError("请先选择包含 database.db 的已有实验目录")
        database = experiment / "database.db"
        model_path = experiment / "model_txt"
        if not database.is_file():
            raise FileNotFoundError(f"实验数据库不存在：{database}")
        if not model_path.is_dir():
            raise FileNotFoundError(f"文本模型目录不存在：{model_path}")

        from store_vision.calibration.geometry_diagnostics import (
            GeometryDiagnosticsRequest,
            run_geometry_diagnostics,
        )
        from store_vision.calibration.pair_geometry import GeometryDiagnosticsConfig

        analysis_path = create_unique_analysis_directory(experiment)
        self.progress.emit("正在只读分析匹配覆盖、H/F 与当前稀疏模型…")
        geometry = run_geometry_diagnostics(
            GeometryDiagnosticsRequest(
                database_path=database.resolve(),
                image_path=self._image_root(self.request.dataset_path).resolve(),
                model_path=model_path.resolve(),
                output_path=analysis_path.resolve(),
                config=GeometryDiagnosticsConfig(),
            )
        )

        self.done.emit(
            {
                "operation": self.request.operation,
                "dataset_path": self.request.dataset_path,
                "experiment_path": experiment,
                "analysis_path": analysis_path,
                "geometry_status": geometry.get("geometry_diagnostics", {}).get("status"),
                "message": "匹配与稀疏几何诊断完成",
            }
        )


class CalibrationOptimizationTab(QWidget):
    """按业务步骤组织标定优化、Sfm 和报告结果。"""

    status_message = pyqtSignal(str)
    sfm_completed = pyqtSignal(object)
    intermediate_published = pyqtSignal(object)

    _STATE_COLORS = {
        "good": QColor("#d7f5df"),
        "warning": QColor("#fff0c2"),
        "blocked": QColor("#ffe2b8"),
        "failed": QColor("#ffd6d6"),
        "neutral": QColor("#edf0f4"),
    }
    _VIEW_LABELS = {
        "raw": "Raw 匹配",
        "verified": "Verified 匹配",
        "grid": "覆盖网格",
        "displacement": "位移向量",
    }

    def __init__(self, cfg: StoreConfig, parent=None):
        """创建只使用当前“输入”页会话数据集和输出目录的空工作区。"""

        super().__init__(parent)
        self.cfg = cfg
        self._dataset_path: Path | None = None
        self._output_root: Path | None = None
        self._current_shared_result: Path | None = None
        self._current_experiment: Path | None = None
        self.snapshot: CalibrationWorkspaceSnapshot | None = None
        self._worker: CalibrationTaskWorker | None = None
        self._selected_artifact: Path | None = None
        self._pose_rows: list[dict[str, Any]] = []
        self._build_ui()
        self._update_action_availability()

    def _build_ui(self) -> None:
        """组装公共状态区和三个职责单一的步骤子页面。"""

        layout = QVBoxLayout(self)
        self.run_status = QLabel(
            "请先在“输入”页选择数据集；结果只来自当前会话运行。"
        )
        self.run_status.setWordWrap(True)
        self.run_status.setStyleSheet(
            "QLabel { background:#f7f8fa; border:1px solid #d0d5dc; "
            "border-radius:6px; padding:7px; }"
        )
        layout.addWidget(self.run_status)
        layout.addWidget(self._build_stage_box())

        self.result_tabs = QTabWidget()
        self.result_tabs.addTab(self._build_calibration_step_page(), "标定优化")
        self.result_tabs.addTab(self._build_sfm_step_page(), "Sfm")
        self.result_tabs.addTab(self._build_report_step_page(), "报告")
        layout.addWidget(self.result_tabs, 1)

    def _build_calibration_step_page(self) -> QWidget:
        """标定优化提供同型号鱼眼共享 K/D、位姿拟合和 BA 主路线。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.btn_run_shared = QPushButton("运行鱼眼联合标定")
        self.btn_run_shared.setToolTip(
            "全部镜头按同一型号处理，共享鱼眼 K/D，联合拟合位姿并自动执行 BA"
        )
        self.btn_run_shared.setStyleSheet(
            "QPushButton { background:#1f6feb; color:white; font-weight:600; padding:6px 12px; }"
        )
        self.btn_run_shared.clicked.connect(self._run_shared_intrinsics)
        actions.addWidget(self.btn_run_shared)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.calibration_result_tabs = QTabWidget()
        self.calibration_result_tabs.addTab(
            self._build_intrinsics_tab(), "共享 K/D"
        )
        self.calibration_result_tabs.addTab(self._build_pose_tab(), "相机位姿")
        self.calibration_result_tabs.addTab(
            self._build_baseline_tab(), "拟合与 BA"
        )
        layout.addWidget(self.calibration_result_tabs, 1)
        return page

    def _build_sfm_step_page(self) -> QWidget:
        """Sfm 页面只保留匹配与稀疏几何辅助诊断，不再控制 BA。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.btn_run_colmap = QPushButton("运行 Sfm")
        self.btn_run_colmap.setToolTip(
            "先验证拟合 K/D，再比较全局、增量和拟合位姿辅助候选，"
            "最终按逐相机三维观测验收"
        )
        self.btn_run_colmap.clicked.connect(self._run_colmap_experiment)
        self.btn_run_analysis = QPushButton("运行匹配诊断")
        self.btn_run_analysis.clicked.connect(self._run_available_analysis)
        actions.addWidget(self.btn_run_colmap)
        actions.addWidget(self.btn_run_analysis)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.sfm_result_tabs = QTabWidget()
        self.sfm_result_tabs.addTab(
            self._build_sfm_acceptance_tab(), "候选与验收"
        )
        self.sfm_result_tabs.addTab(self._build_geometry_tab(), "匹配与几何")
        layout.addWidget(self.sfm_result_tabs, 1)
        return page

    def _build_sfm_acceptance_tab(self) -> QWidget:
        """展示内参路由、候选选择和逐相机三维观测验收摘要。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        self.sfm_acceptance_view = QTextBrowser()
        self.sfm_acceptance_view.setPlaceholderText(
            "运行 Sfm 后显示全局/增量/拟合位姿候选和真实三维观测门禁。"
        )
        self._configure_fixed_width_text(self.sfm_acceptance_view)
        layout.addWidget(self.sfm_acceptance_view)
        return page

    def _build_report_step_page(self) -> QWidget:
        """报告页面只提供输出目录入口、概览和全部产物查看。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.btn_open_directory = QPushButton("打开当前中间层目录")
        self.btn_open_directory.clicked.connect(self._open_result_directory)
        actions.addWidget(self.btn_open_directory)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.report_result_tabs = QTabWidget()
        self.report_result_tabs.addTab(self._build_overview_tab(), "概览")
        self.report_result_tabs.addTab(self._build_artifact_tab(), "产物")
        layout.addWidget(self.report_result_tabs, 1)
        return page

    def _build_stage_box(self) -> QGroupBox:
        """构建从共享鱼眼拟合到全相机视觉验收的六阶段状态卡片。"""

        box = QGroupBox("共享鱼眼与全相机 SfM 阶段")
        row = QHBoxLayout(box)
        self.stage_labels: dict[str, QLabel] = {}
        for key, title in (
            ("input_scale", "① 输入与尺度"),
            ("fit_pose", "② K/D 与位姿拟合"),
            ("intrinsics_gate", "③ 内参可信度门禁"),
            ("sfm_candidates", "④ 多候选 SfM"),
            ("visual_acceptance", "⑤ 三维证据验收"),
            ("output", "⑥ 全相机输出"),
        ):
            label = QLabel(f"{title}\n未生成")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(58)
            label.setStyleSheet("QLabel { background:#edf0f4; border:1px solid #c8cdd4; border-radius:6px; padding:6px; }")
            label.setToolTip("尚未加载对应报告")
            row.addWidget(label, 1)
            self.stage_labels[key] = label
        return box

    @staticmethod
    def _configure_fixed_width_table(table: QTableWidget) -> None:
        """固定表格可见宽度，长文本换行并按内容自动抬高行高。"""

        table.setWordWrap(True)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

    @staticmethod
    def _configure_fixed_width_text(view: QTextBrowser) -> None:
        """让说明窗口只纵向滚动，所有长行都在固定宽度内自动换行。"""

        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)

    def _build_overview_tab(self) -> QWidget:
        """以不可拖动的 1:1 左右布局构建指标与关键结论概览。"""

        page = QWidget()
        self.overview_content_layout = QHBoxLayout(page)
        self.metrics_table = QTableWidget(0, 3)
        self.metrics_table.setHorizontalHeaderLabels(["指标", "当前值", "说明"])
        self.metrics_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.metrics_table.verticalHeader().setVisible(False)
        self._configure_fixed_width_table(self.metrics_table)
        self.findings_view = QTextBrowser()
        self.findings_view.setPlaceholderText("加载报告后在这里显示关键结论、限制和阻塞原因。")
        self._configure_fixed_width_text(self.findings_view)
        self.overview_content_layout.addWidget(self.metrics_table, 1)
        self.overview_content_layout.addWidget(self.findings_view, 1)
        return page

    def _build_intrinsics_tab(self) -> QWidget:
        """以固定 3:1 左右布局展示共享鱼眼 K/D 与拟合诊断。"""

        page = QWidget()
        self.intrinsics_content_layout = QHBoxLayout(page)
        self.intrinsics_model_table = QTableWidget(0, 6)
        self.intrinsics_model_table.setHorizontalHeaderLabels(
            ["模型", "共享 K", "共享 D", "拟合 RMSE(px)", "BA RMSE(px)", "结论"]
        )
        self.intrinsics_model_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.intrinsics_model_table.verticalHeader().setVisible(False)
        self._configure_fixed_width_table(self.intrinsics_model_table)
        self.intrinsics_content_layout.addWidget(self.intrinsics_model_table, 3)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.intrinsics_image_label = ImageThumbnail(
            "加载共享标定结果后显示平面映射"
        )
        detail_layout.addWidget(
            self.intrinsics_image_label, 0, Qt.AlignmentFlag.AlignTop
        )
        self.intrinsics_detail_view = QTextBrowser()
        self.intrinsics_detail_view.setPlaceholderText(
            "这里显示同型号鱼眼假设、共享 K/D 和联合 BA 数值。"
        )
        self._configure_fixed_width_text(self.intrinsics_detail_view)
        detail_layout.addWidget(self.intrinsics_detail_view, 1)
        self.intrinsics_content_layout.addWidget(detail_panel, 1)
        return page

    def _build_pose_tab(self) -> QWidget:
        """以固定 3:1 左右布局展示逐机位姿表与完整位姿详情。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("可视化"))
        self.pose_view_combo = QComboBox()
        self.pose_view_combo.addItem("相机中心与朝向", "camera_pose")
        self.pose_view_combo.addItem("高度分布", "height")
        self.pose_view_combo.addItem("Homography 映射", "homography")
        self.pose_view_combo.currentIndexChanged.connect(self._refresh_pose_visualization)
        controls.addWidget(self.pose_view_combo)
        controls.addStretch(1)
        self.pose_image_label = ImageThumbnail(
            "加载共享标定结果后显示相机位姿"
        )
        controls.addWidget(self.pose_image_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(controls)

        content = QWidget()
        self.pose_content_layout = QHBoxLayout(content)
        self.pose_content_layout.setContentsMargins(0, 0, 0, 0)
        self.pose_table = QTableWidget(0, 6)
        self.pose_table.setHorizontalHeaderLabels(
            ["物理相机", "高度(m)", "向下分量", "重投影 RMSE(px)", "分类", "物理检查"]
        )
        self.pose_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pose_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pose_table.verticalHeader().setVisible(False)
        self._configure_fixed_width_table(self.pose_table)
        self.pose_table.itemSelectionChanged.connect(self._refresh_pose_detail)
        self.pose_content_layout.addWidget(self.pose_table, 3)
        self.pose_detail_view = QTextBrowser()
        self.pose_detail_view.setPlaceholderText("选择相机后显示 world_to_camera R/T 与相机中心。")
        self._configure_fixed_width_text(self.pose_detail_view)
        self.pose_content_layout.addWidget(self.pose_detail_view, 1)
        layout.addWidget(content, 1)
        return page

    def _build_geometry_tab(self) -> QWidget:
        """构建图像对选择、诊断图和原始指标查看区。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("图像对"))
        self.pair_combo = QComboBox()
        self.pair_combo.currentIndexChanged.connect(self._refresh_pair_view)
        controls.addWidget(self.pair_combo, 2)
        controls.addWidget(QLabel("视图"))
        self.pair_view_combo = QComboBox()
        for key, label in self._VIEW_LABELS.items():
            self.pair_view_combo.addItem(label, key)
        self.pair_view_combo.currentIndexChanged.connect(self._refresh_pair_view)
        controls.addWidget(self.pair_view_combo)
        self.btn_show_match_graph = QPushButton("显示相机匹配图")
        self.btn_show_match_graph.clicked.connect(self._show_match_graph)
        controls.addWidget(self.btn_show_match_graph)
        controls.addStretch(1)
        layout.addLayout(controls)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.pair_image_label = ImageThumbnail("尚未加载图像对可视化")
        content_layout.addWidget(
            self.pair_image_label, 0, Qt.AlignmentFlag.AlignTop
        )
        self.pair_metrics_view = QTextBrowser()
        content_layout.addWidget(self.pair_metrics_view, 1)
        layout.addWidget(content, 1)
        return page

    def _build_baseline_tab(self) -> QWidget:
        """以固定 3:1 左右布局展示拟合初始化基线与联合 BA。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        readiness = QHBoxLayout()
        self.ba_readiness_label = QLabel(
            "联合 BA 将由鱼眼拟合结果自动启动，并允许复用拟合控制观测。"
        )
        self.ba_readiness_label.setWordWrap(True)
        readiness.addWidget(self.ba_readiness_label, 1)
        layout.addLayout(readiness)
        content = QWidget()
        self.baseline_content_layout = QHBoxLayout(content)
        self.baseline_content_layout.setContentsMargins(0, 0, 0, 0)
        self.camera_table = QTableWidget(0, 6)
        self.camera_table.setHorizontalHeaderLabels(
            ["物理相机", "图片", "共享 K", "共享 D", "R/t", "BA 重投影"]
        )
        self.camera_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.camera_table.verticalHeader().setVisible(False)
        self._configure_fixed_width_table(self.camera_table)
        self.baseline_content_layout.addWidget(self.camera_table, 3)
        self.baseline_view = QTextBrowser()
        self.baseline_view.setPlaceholderText(
            "加载后显示拟合初始化与联合 BA 的误差、收敛和观测复用策略。"
        )
        self._configure_fixed_width_text(self.baseline_view)
        self.baseline_content_layout.addWidget(self.baseline_view, 1)
        layout.addWidget(content, 1)
        return page

    def _build_artifact_tab(self) -> QWidget:
        """以不可拖动的 1:1 左右布局构建产物目录与预览区。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.btn_open_artifact = QPushButton("用系统程序打开选中文件")
        self.btn_open_artifact.clicked.connect(self._open_selected_artifact)
        self.btn_open_artifact.setEnabled(False)
        controls.addWidget(self.btn_open_artifact)
        controls.addStretch(1)
        layout.addLayout(controls)

        content = QWidget()
        self.artifact_content_layout = QHBoxLayout(content)
        self.artifact_content_layout.setContentsMargins(0, 0, 0, 0)
        self.artifact_tree = QTreeWidget()
        self.artifact_tree.setHeaderLabels(["产物", "类型"])
        self.artifact_tree.setWordWrap(True)
        self.artifact_tree.setUniformRowHeights(False)
        self.artifact_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.artifact_tree.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.artifact_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.artifact_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.artifact_tree.itemSelectionChanged.connect(self._preview_artifact)
        self.artifact_content_layout.addWidget(self.artifact_tree, 1)

        self.artifact_stack = QStackedWidget()
        self.artifact_text = QTextBrowser()
        self._configure_fixed_width_text(self.artifact_text)
        self.artifact_image_label = ImageThumbnail("选择图片产物后预览")
        self.artifact_stack.addWidget(self.artifact_text)
        self.artifact_stack.addWidget(self.artifact_image_label)
        self.artifact_content_layout.addWidget(self.artifact_stack, 1)
        layout.addWidget(content, 1)
        return page

    # --------------------------------------------------------------- 会话路径
    def set_dataset_path(self, dataset_path: str | Path) -> None:
        """接收公共顶栏数据集；切换数据集时丢弃上一会话结果引用。"""

        dataset = Path(dataset_path).expanduser().resolve()
        if dataset != self._dataset_path:
            self._dataset_path = dataset
            self._reset_current_results()
        self._update_action_availability()

    def set_output_root(self, output_root: str | Path) -> None:
        """接收公共顶栏规划的中间层根；切换后只认新会话结果。"""

        resolved = Path(output_root).expanduser().resolve()
        if resolved != self._output_root:
            self._output_root = resolved
            self._reset_current_results()
        self._update_action_availability()

    def set_session_context(
        self,
        dataset_path: str | Path,
        output_root: str | Path,
    ) -> None:
        """原子接收当前数据集与中间层根目录，避免路径串用。"""

        dataset = Path(dataset_path).expanduser().resolve()
        output = Path(output_root).expanduser().resolve()
        if dataset != self._dataset_path or output != self._output_root:
            self._dataset_path = dataset
            self._output_root = output
            self._reset_current_results()
        self._update_action_availability()

    def refresh_action_availability(self) -> None:
        """输出目录由完整处理创建后，刷新报告页目录入口状态。"""

        self._update_action_availability()

    def _reset_current_results(self) -> None:
        """清除上一数据集或上一输出轮次的结果引用。"""

        self._current_shared_result = None
        self._current_experiment = None
        self.snapshot = None
        self.run_status.setText("当前会话尚未运行标定优化或 Sfm。")

    # --------------------------------------------------------------- 报告展示
    def load_current_results(self) -> None:
        """只加载本会话刚运行产生的共享标定、COLMAP 和分析结果。"""

        if self._dataset_path is None:
            return
        try:
            self.snapshot = load_calibration_workspace(
                self._dataset_path,
                self._current_experiment,
                None,
                self._current_shared_result,
                auto_discover_shared=False,
                include_legacy_reports=False,
            )
            self._apply_snapshot(self.snapshot)
            message = (
                f"当前会话：{len(self.snapshot.artifacts)} 个产物、"
                f"{len(self.snapshot.pairs)} 组图像对、"
                f"{len(self.snapshot.shared.poses) if self.snapshot.shared else 0} 台共享位姿"
            )
            self.run_status.setText(message)
            self.status_message.emit(message)
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", str(exc))

    def _apply_snapshot(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """把归一化快照一次分发给三个步骤页中的结果区域。"""

        self._apply_stages(snapshot)
        self._apply_metrics(snapshot)
        self._apply_shared_intrinsics(snapshot)
        self._apply_shared_poses(snapshot)
        self._apply_sfm_acceptance(snapshot)
        self._apply_pairs(snapshot)
        self._apply_baseline(snapshot)
        self._apply_artifacts(snapshot)

    def _apply_stages(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """根据阶段状态更新文字、提示和颜色。"""

        for stage in snapshot.stages:
            label = self.stage_labels[stage.key]
            label.setText(f"{stage.title}\n{stage.status}")
            label.setToolTip(stage.detail)
            state = self._status_state(stage.status)
            color = self._STATE_COLORS[state].name()
            label.setStyleSheet(
                f"QLabel {{ background:{color}; border:1px solid #b9bec5; border-radius:6px; padding:6px; }}"
            )

    @staticmethod
    def _status_state(status: str) -> str:
        """把工作流状态映射为 GUI 通用颜色类别。"""

        value = status.lower()
        if value in {"complete", "completed", "ready"}:
            return "good"
        if value == "blocked" or "missing" in value:
            return "blocked"
        if value in {"failed", "unavailable"}:
            return "failed"
        if value in {"partial", "warning"}:
            return "warning"
        return "neutral"

    def _apply_metrics(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """填充概览指标表与去重后的关键结论。"""

        self.metrics_table.setRowCount(len(snapshot.metrics))
        for row, metric in enumerate(snapshot.metrics):
            items = [
                QTableWidgetItem(metric.label),
                QTableWidgetItem(metric.value),
                QTableWidgetItem(metric.detail),
            ]
            color = self._STATE_COLORS.get(metric.state, self._STATE_COLORS["neutral"])
            for column, item in enumerate(items):
                item.setBackground(color)
                self.metrics_table.setItem(row, column, item)
        self.metrics_table.resizeRowsToContents()
        lines = [f"• {finding}" for finding in snapshot.findings]
        lines.extend(f"• 报告读取告警：{warning}" for warning in snapshot.warnings)
        self.findings_view.setPlainText("\n\n".join(lines) if lines else "当前报告没有附加结论。")

    @staticmethod
    def _format_matrix(matrix: Any, digits: int = 3) -> str:
        """把 K/R/T 等小矩阵格式化为紧凑且可复制的多行文本。"""

        if not isinstance(matrix, list):
            return "—"
        rows = []
        for row in matrix:
            if isinstance(row, list):
                rows.append("[" + ", ".join(f"{float(value):.{digits}f}" for value in row) + "]")
            else:
                rows.append(f"{float(row):.{digits}f}")
        return "\n".join(rows)

    def _apply_shared_intrinsics(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """填充共享鱼眼 K/D、拟合初始化和 BA 摘要。"""

        shared = snapshot.shared
        available_models = list(shared.models) if shared else []
        self.intrinsics_model_table.setRowCount(len(available_models))
        if shared is None:
            self.intrinsics_detail_view.setPlainText("尚未生成或加载鱼眼联合标定结果。")
            self._set_preview_pixmap(
                self.intrinsics_image_label,
                None,
                "尚未生成 Homography 平面映射",
            )
            return

        for row_index, model_name in enumerate(available_models):
            model = shared.models.get(model_name)
            if not isinstance(model, dict):
                continue
            k = model.get("K")
            d = model.get("D")
            k_summary = (
                f"fx={k[0][0]:.2f}, fy={k[1][1]:.2f}, "
                f"cx={k[0][2]:.2f}, cy={k[1][2]:.2f}"
                if k
                else "—"
            )
            fitted = model.get("initialization", {}).get("fit", {})
            bundle = model.get("bundle_adjustment", {})
            validation = (
                model.get("intrinsics_validation", {})
                or snapshot.sfm_intrinsics.get("intrinsics_validation", {})
            )
            conclusion = (
                "可信，可进入 SfM"
                if validation.get("usable_for_sfm")
                else "门禁拒绝，仅可试算"
            )
            values = [
                model_name,
                k_summary,
                ", ".join(f"{float(value):.4g}" for value in d) if d else "—",
                self._format_number(fitted.get("reprojection_rmse_px")),
                self._format_number(bundle.get("reprojection_rmse_px")),
                conclusion,
            ]
            color = self._STATE_COLORS[
                "good" if validation.get("usable_for_sfm") else "warning"
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setBackground(color)
                self.intrinsics_model_table.setItem(row_index, column, item)
        self.intrinsics_model_table.resizeRowsToContents()
        selection = shared.report.get("selection", {})
        summary = shared.report.get("summary", {})
        scale = shared.report.get("scale", {})
        validation = (
            selection.get("intrinsics_validation", {})
            or snapshot.sfm_intrinsics.get("intrinsics_validation", {})
        )
        self.intrinsics_detail_view.setPlainText(
            "同型号鱼眼联合标定\n\n"
            f"模型：{shared.selected_model or '无'}\n"
            f"K：\n{self._format_matrix(shared.selected_K)}\n\n"
            f"D：{selection.get('selected_D', selection.get('distortion_D', '—'))}\n"
            f"尺度单位：{scale.get('metric_unit', '—')}\n"
            f"尺度拟合 RMSE：{self._format_number(scale.get('rmse_metres'))} m\n"
            f"拟合初始化：{'完成' if summary.get('fitted_initialization_available') else '未完成'}\n"
            f"联合 BA：{'完成' if summary.get('bundle_adjustment_success') else '需检查'}\n"
            f"内参可信度门禁：{validation.get('status', '未生成')}\n"
            f"SfM 可直接使用：{'是' if validation.get('usable_for_sfm') else '否'}\n"
            f"硬失败：{validation.get('hard_failures') or '无'}\n"
            f"逐机物理检查通过率：{self._format_percent(summary.get('physical_plausibility_pass_rate'))}\n\n"
            f"路线：{selection.get('reason', '—')}\n\n"
            f"畸变：{selection.get('distortion_observability', '—')}"
        )
        self._set_preview_pixmap(
            self.intrinsics_image_label,
            shared.visualizations.get("homography"),
            "尚未生成 Homography 平面映射",
        )

    @staticmethod
    def _format_number(value: Any) -> str:
        """为条件数、误差等数值选择紧凑显示格式。"""

        return f"{value:.4g}" if isinstance(value, (int, float)) else "—"

    @staticmethod
    def _format_percent(value: Any) -> str:
        """将 0～1 的比率显示为百分比。"""

        return f"{value:.1%}" if isinstance(value, (int, float)) else "—"

    def _apply_shared_poses(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """填充逐机位姿表并默认显示相机中心与朝向图。"""

        self._pose_rows = list(snapshot.shared.poses) if snapshot.shared else []
        self.pose_table.setRowCount(len(self._pose_rows))
        for row_index, pose in enumerate(self._pose_rows):
            reasons = pose.get("physical_plausibility_reasons", [])
            values = [
                pose.get("physical_camera_id", "—"),
                self._format_number(pose.get("height_metres")),
                self._format_number(pose.get("downward_optical_axis_component")),
                self._format_number(pose.get("pose_reprojection_rmse_px")),
                pose.get("classification", "—"),
                "通过" if pose.get("physical_plausibility_passed") else ("；".join(map(str, reasons)) or "异常"),
            ]
            color = self._STATE_COLORS[
                "good" if pose.get("physical_plausibility_passed") else "warning"
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setBackground(color)
                self.pose_table.setItem(row_index, column, item)
        self.pose_table.resizeRowsToContents()
        self._refresh_pose_visualization()
        if self._pose_rows:
            self.pose_table.selectRow(0)
        else:
            self.pose_detail_view.setPlainText("尚未生成或加载逐机 R/T。")

    def _refresh_pose_visualization(self) -> None:
        """按下拉选择切换相机分布、高度和 Homography 图。"""

        shared = self.snapshot.shared if self.snapshot else None
        key = self.pose_view_combo.currentData() or "camera_pose"
        path = shared.visualizations.get(key) if shared else None
        self._set_preview_pixmap(self.pose_image_label, path, "尚未生成该位姿可视化")

    def _refresh_pose_detail(self) -> None:
        """展示选中相机的 world_to_camera 约定及完整 R/T/C。"""

        row = self.pose_table.currentRow()
        if row < 0 or row >= len(self._pose_rows):
            return
        pose = self._pose_rows[row]
        self.pose_detail_view.setPlainText(
            f"相机：{pose.get('physical_camera_id', '—')}\n"
            f"图片：{pose.get('image_name', '—')}\n"
            f"约定：{pose.get('convention', 'world_to_camera')}\n\n"
            f"R：\n{self._format_matrix(pose.get('R'))}\n\n"
            f"T (m)：\n{self._format_matrix(pose.get('T_metres'))}\n\n"
            f"C_world (m)：\n{self._format_matrix(pose.get('camera_center_world_metres'))}\n\n"
            f"Euler xyz (deg)：{pose.get('euler_xyz_degrees_world_to_camera', '—')}\n"
            f"光轴(world)：{pose.get('optical_axis_world', '—')}\n"
            f"高度：{self._format_number(pose.get('height_metres'))} m\n"
            f"物理检查：{'通过' if pose.get('physical_plausibility_passed') else '疑似异常'}\n"
            f"原因：{pose.get('physical_plausibility_reasons') or '—'}"
        )

    def _apply_pairs(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """更新图像对下拉框并显示第一组真实诊断。"""

        self.pair_combo.blockSignals(True)
        self.pair_combo.clear()
        for pair in snapshot.pairs:
            self.pair_combo.addItem(pair.name, pair)
        self.pair_combo.blockSignals(False)
        self._refresh_pair_view()

    def _apply_sfm_acceptance(
        self, snapshot: CalibrationWorkspaceSnapshot
    ) -> None:
        """用明确分类展示视觉注册、弱/先验位姿和未注册相机。"""

        routing = snapshot.sfm_intrinsics
        registration = snapshot.sfm_registration
        if not routing and not registration:
            self.sfm_acceptance_view.setPlainText("尚未运行当前会话的 Sfm。")
            return
        self.sfm_acceptance_view.setPlainText(
            "SfM 内参与候选验收\n\n"
            f"相机模型：{routing.get('camera_model', '—')}\n"
            f"内参路由：{routing.get('routing_source', '—')}\n"
            f"匹配参数来源：{routing.get('matching_intrinsics_source', '—')}\n"
            f"重建参数来源：{routing.get('mapping_intrinsics_source', '—')}\n"
            f"可信标定：{'是' if routing.get('credible_calibration') else '否/试算'}\n"
            f"畸变缩放：{routing.get('distortion_scale', '—')}\n"
            f"拟合门禁：{routing.get('intrinsics_validation', {}).get('status', '—')}\n\n"
            f"候选数量：{registration.get('candidate_count', 0)}\n"
            f"最终候选：{registration.get('selected_candidate', '无')}\n"
            f"验收状态：{registration.get('status', '未运行')}\n"
            f"全相机视觉注册："
            f"{'通过' if registration.get('full_visual_registration') else '未通过'}\n"
            f"视觉注册图片：{registration.get('visually_registered_images', [])}\n"
            f"弱观测/先验位姿：{registration.get('weak_or_prior_only_images', [])}\n"
            f"未注册图片：{registration.get('unregistered_images', [])}\n"
            f"稀疏点：{registration.get('sparse_points', 0)}\n"
            f"三维观测总数：{registration.get('total_point3d_observations', 0)}\n\n"
            f"平均轨迹长度：{registration.get('mean_track_length', '—')}\n"
            f"逐相机三维观测：\n"
            f"{json.dumps(registration.get('per_image_point3d_observations', {}), ensure_ascii=False, indent=2)}\n\n"
            f"完整候选比较：{registration.get('comparison_report', '—')}"
        )

    def _refresh_pair_view(self) -> None:
        """同步切换图像对、可视化类型和 JSON 指标。"""

        pair = self.pair_combo.currentData()
        if pair is None:
            self.pair_image_label.clear_image("没有可显示的图像对诊断")
            self.pair_metrics_view.clear()
            return
        view_key = self.pair_view_combo.currentData() or "verified"
        image_path = pair.images.get(view_key)
        self._set_preview_pixmap(self.pair_image_label, image_path, "当前图像对没有该类可视化")
        self.pair_metrics_view.setPlainText(json.dumps(pair.summary, ensure_ascii=False, indent=2))

    def _show_match_graph(self) -> None:
        """在几何主视图中显示全局相机匹配图。"""

        if self.snapshot is None:
            return
        graph = next((path for path in self.snapshot.artifacts if path.name == "camera_match_graph.png"), None)
        self._set_preview_pixmap(self.pair_image_label, graph, "尚未生成相机匹配图")

    @staticmethod
    def _set_preview_pixmap(
        label: ImageThumbnail, path: Path | None, empty_text: str
    ) -> None:
        """显示缩略图，并由统一组件提供点击查看原分辨率图片。"""

        label.set_image(path, empty_text)

    def _apply_baseline(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """填充拟合初始化基线、共享 K/D 和自动联合 BA 结果。"""

        shared = snapshot.shared
        rows = shared.poses if shared else []
        self.camera_table.setRowCount(len(rows))
        for row_index, camera in enumerate(rows):
            values = [
                camera.get("physical_camera_id", "—"),
                camera.get("image_name", "—"),
                "✓ 共享",
                "✓ 共享鱼眼 D",
                "✓ world_to_camera",
                f"{self._format_number(camera.get('pose_reprojection_rmse_px'))} px",
            ]
            for column, value in enumerate(values):
                self.camera_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.camera_table.resizeRowsToContents()
        optimization = shared.report.get("nonlinear_optimization", {}) if shared else {}
        initialization = optimization.get("initialization", {}).get("fit", {})
        bundle = optimization.get("bundle_adjustment", {})
        self.ba_readiness_label.setText(
            (
                "联合 BA 已完成：从拟合初始化基线启动，并复用拟合控制观测。"
                if bundle.get("success")
                else (
                    "联合 BA 已执行但需要检查收敛告警。"
                    if bundle.get("performed")
                    else "尚未运行鱼眼联合标定。"
                )
            )
        )
        self.baseline_view.setPlainText(
            "拟合初始化基线\n\n"
            f"状态：{'完成' if initialization.get('success') else '—'}\n"
            f"RMSE：{self._format_number(initialization.get('reprojection_rmse_px'))} px\n"
            f"最大误差：{self._format_number(initialization.get('reprojection_max_px'))} px\n"
            f"求值次数：{initialization.get('function_evaluations', '—')}\n\n"
            "联合 BA\n\n"
            f"状态：{'完成' if bundle.get('success') else ('已执行/需检查' if bundle.get('performed') else '—')}\n"
            f"RMSE：{self._format_number(bundle.get('reprojection_rmse_px'))} px\n"
            f"最大误差：{self._format_number(bundle.get('reprojection_max_px'))} px\n"
            f"损失函数：{bundle.get('loss', '—')}\n"
            f"复用拟合观测：{'是' if bundle.get('uses_fitted_calibration_observations') else '—'}\n\n"
            "B0 不再是 BA 启用条件；Sfm 将比较多类候选并以真实三维观测验收，"
            "拟合位姿只作为失败恢复候选。"
        )

    @staticmethod
    def _availability(value: Any) -> str:
        """把不同报告结构的可用性转换为统一勾选标记。"""

        if isinstance(value, dict):
            return "✓" if value.get("available") else "—"
        return "✓" if value else "—"

    def _apply_artifacts(self, snapshot: CalibrationWorkspaceSnapshot) -> None:
        """按报告根目录组织全部可预览产物。"""

        self.artifact_tree.clear()
        groups: dict[Path, QTreeWidgetItem] = {}
        for artifact in snapshot.artifacts:
            root = next((path for path in snapshot.report_roots if artifact.is_relative_to(path)), artifact.parent)
            group = groups.get(root)
            if group is None:
                group = QTreeWidgetItem([str(root), "目录"])
                groups[root] = group
                self.artifact_tree.addTopLevelItem(group)
            try:
                label = str(artifact.relative_to(root))
            except ValueError:
                label = artifact.name
            item = QTreeWidgetItem([label, artifact.suffix.lower().lstrip(".")])
            item.setData(0, Qt.ItemDataRole.UserRole, str(artifact))
            group.addChild(item)
        self.artifact_tree.expandToDepth(0)

    def _preview_artifact(self) -> None:
        """根据选中文件类型切换文本或图片预览。"""

        items = self.artifact_tree.selectedItems()
        if not items:
            return
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not value:
            return
        path = Path(value)
        self._selected_artifact = path
        self.btn_open_artifact.setEnabled(path.is_file())
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            self._set_preview_pixmap(self.artifact_image_label, path, "无法预览图片")
            self.artifact_stack.setCurrentIndex(1)
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = str(exc)
        if len(text) > 1_000_000:
            text = text[:1_000_000] + "\n\n[内容过长，界面仅显示前 1,000,000 字符]"
        self.artifact_text.setPlainText(text)
        self.artifact_stack.setCurrentIndex(0)

    # --------------------------------------------------------------- 后台运行
    def _session_paths(self) -> tuple[Path, Path]:
        """返回公共顶栏共享的数据集和输出根目录，并给出明确缺失提示。"""

        if self._dataset_path is None:
            raise ValueError("请先在“输入”页选择数据集")
        if self._output_root is None:
            raise ValueError("“输入优化”页尚未确定中间层目录")
        if not self._dataset_path.is_dir():
            raise FileNotFoundError(f"当前数据集目录不存在：{self._dataset_path}")
        return self._dataset_path, self._output_root

    def _update_action_availability(self, *, ignore_worker: bool = False) -> None:
        """根据当前会话和本轮 COLMAP 结果更新按钮状态。"""

        session_ready = (
            self._dataset_path is not None
            and self._dataset_path.is_dir()
            and self._output_root is not None
        )
        task_running = (
            not ignore_worker
            and self._worker is not None
            and self._worker.isRunning()
        )
        self.btn_run_shared.setEnabled(session_ready and not task_running)
        self.btn_run_colmap.setEnabled(
            session_ready
            and self._current_shared_result is not None
            and not task_running
        )
        self.btn_run_analysis.setEnabled(
            session_ready
            and self._current_experiment is not None
            and (self._current_experiment / "database.db").is_file()
            and (self._current_experiment / "model_txt").is_dir()
            and not task_running
        )
        self.btn_open_directory.setEnabled(
            self._output_root is not None and self._output_root.is_dir()
        )

    def _run_shared_intrinsics(self) -> None:
        """以当前数据集运行鱼眼联合标定，并写入本轮专用输出目录。"""

        try:
            dataset, output_root = self._session_paths()
            request = CalibrationTaskRequest(
                operation="shared_intrinsics",
                dataset_path=dataset,
                output_root=output_root,
                config=self.cfg,
            )
        except Exception as exc:
            QMessageBox.information(self, "输入不完整", str(exc))
            return
        self._start_task(request)

    def _run_available_analysis(self) -> None:
        """自动使用当前会话刚产生的 COLMAP 目录运行只读匹配诊断。"""

        try:
            dataset, _ = self._session_paths()
            if self._current_experiment is None:
                raise ValueError("请先运行当前会话的 Sfm")
            request = CalibrationTaskRequest(
                operation="available_analysis",
                dataset_path=dataset,
                experiment_path=self._current_experiment,
            )
        except Exception as exc:
            QMessageBox.information(self, "输入不完整", str(exc))
            return
        self._start_task(request)

    def _run_colmap_experiment(self) -> None:
        """确认高成本操作后，在当前会话输出目录运行 Sfm。"""

        try:
            dataset, output_root = self._session_paths()
            request = CalibrationTaskRequest(
                operation="new_experiment",
                dataset_path=dataset,
                output_root=output_root / "calibration_demo",
                shared_result_path=self._current_shared_result,
            )
        except Exception as exc:
            QMessageBox.information(self, "输入不完整", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "运行 Sfm",
            "将使用当前鱼眼联合标定的拟合 K/D，创建新的 run_时间戳目录并运行 "
            "CPU 特征提取、匹配和 Mapper。\n"
            "已有 database.db 不会被覆盖。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_task(request)

    def _start_task(self, request: CalibrationTaskRequest) -> None:
        """阻止重复任务并连接后台线程的进度与结果信号。"""

        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "任务运行中", "请等待当前标定任务完成")
            return
        self._set_task_buttons_enabled(False)
        self._worker = CalibrationTaskWorker(request, self)
        self._worker.progress.connect(self._on_task_progress)
        self._worker.done.connect(self._on_task_done)
        self._worker.failed.connect(self._on_task_failed)
        self._worker.start()

    def _set_task_buttons_enabled(self, enabled: bool) -> None:
        """运行期间禁用会产生新任务的按钮。"""

        if not enabled:
            self.btn_run_shared.setEnabled(False)
            self.btn_run_colmap.setEnabled(False)
            self.btn_run_analysis.setEnabled(False)
            return
        self._update_action_availability(ignore_worker=True)

    def _on_task_progress(self, message: str) -> None:
        """把阶段进度同步到页面和主窗口状态栏。"""

        self.run_status.setText(message)
        self.status_message.emit(message)

    def _on_task_done(self, result: dict[str, Any]) -> None:
        """更新新目录并自动重新加载刚生成的报告。"""

        result_dataset = result.get("dataset_path")
        if (
            result_dataset is not None
            and self._dataset_path is not None
            and Path(result_dataset).expanduser().resolve() != self._dataset_path
        ):
            self._set_task_buttons_enabled(True)
            message = "任务属于上一数据集，结果未载入当前会话。"
            self.run_status.setText(message)
            self.status_message.emit(message)
            return
        produced_path = result.get("shared_result_path") or result.get("experiment_path")
        if (
            produced_path is not None
            and self._output_root is not None
            and not Path(produced_path).expanduser().resolve().is_relative_to(
                self._output_root
            )
        ):
            self._set_task_buttons_enabled(True)
            message = "任务属于上一输出轮次，结果未载入当前会话。"
            self.run_status.setText(message)
            self.status_message.emit(message)
            return
        experiment = result.get("experiment_path")
        if experiment:
            self._current_experiment = Path(experiment).expanduser().resolve()
        shared_result = result.get("shared_result_path")
        if shared_result:
            self._current_shared_result = Path(shared_result).expanduser().resolve()
        intermediate = result.get("intermediate_path")
        if intermediate:
            self.intermediate_published.emit(
                Path(intermediate).expanduser().resolve()
            )
        self._set_task_buttons_enabled(True)
        message = str(result.get("message", "标定任务完成"))
        self.run_status.setText(message)
        self.status_message.emit(message)
        self.load_current_results()
        if result.get("operation") == "new_experiment":
            # 主窗口据此构建只使用本轮视觉注册相机的 2.5D 试算，不直接
            # 把 SfM 任意尺度坐标写成门店坐标。
            self.sfm_completed.emit(result)

    def _on_task_failed(self, message: str) -> None:
        """恢复按钮并以执行失败而非 blocked 的方式提示异常。"""

        self._set_task_buttons_enabled(True)
        self.run_status.setText(f"任务失败：{message}")
        self.status_message.emit(f"标定任务失败：{message}")
        QMessageBox.critical(self, "标定任务失败", message)

    # --------------------------------------------------------------- 系统打开
    def _open_result_directory(self) -> None:
        """优先打开本轮中间层数据包，尚未发布时打开数据集根。"""

        target = self._current_shared_result or self._output_root
        if target is not None and target.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_selected_artifact(self) -> None:
        """用系统默认程序打开当前选中的产物。"""

        if self._selected_artifact is not None and self._selected_artifact.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected_artifact)))
