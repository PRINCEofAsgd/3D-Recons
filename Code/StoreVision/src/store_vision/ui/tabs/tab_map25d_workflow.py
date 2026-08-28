"""一级“2.5D 建图”页：能力检查、独立运行和结果复核。"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from store_vision.config import StoreConfig
from store_vision.data.models import StoreDataset
from store_vision.data.workspace import (
    candidate_names,
    resolve_capabilities,
)
from store_vision.mapping.map25d_pipeline import Map25DWorkflowResult
from store_vision.ui.tabs.tab_calibration import CalibrationTab
from store_vision.ui.tabs.tab_map25d import Map25DTab
from store_vision.ui.widgets.intermediate_source import IntermediateSourceSelector


class Map25DWorkflowTab(QWidget):
    """只消费已发布中间层的 2.5D 业务页面。"""

    run_requested = pyqtSignal(object, str, str)  # 中间层、候选、运行模式
    status_message = pyqtSignal(str)

    def __init__(self, cfg: StoreConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.dataset: StoreDataset | None = None
        self.intermediate_path: Path | None = None
        self.output_path: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建依赖栏和标定映射、2.5D、质量报告三个子页面。"""

        layout = QVBoxLayout(self)
        self.source_selector = IntermediateSourceSelector(self.cfg)
        self.source_selector.path_changed.connect(self._apply_intermediate)
        layout.addWidget(self.source_selector)
        bar = QHBoxLayout()
        self.btn_run = QPushButton("运行 2.5D 建图")
        self.btn_run.clicked.connect(self._request_run)
        self.candidate_combo = QComboBox()
        self.candidate_combo.currentTextChanged.connect(
            self._on_candidate_changed
        )
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("基础建图（全部相机）", "baseline")
        self.mode_combo.addItem("SfM 辅助（相机支撑加权）", "sfm_assisted")
        self.mode_combo.addItem(
            "SfM 注册相机对照（仅真实三维观测相机）",
            "sfm_registered_only",
        )
        self.mode_combo.currentIndexChanged.connect(self._refresh_capability)
        self.btn_open = QPushButton("打开本轮输出")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_output)
        bar.addWidget(self.btn_run)
        bar.addWidget(QLabel("标定候选："))
        bar.addWidget(self.candidate_combo)
        bar.addWidget(QLabel("建图模式："))
        bar.addWidget(self.mode_combo)
        bar.addWidget(self.btn_open)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.capability_label = QLabel(
            "请先在“输入优化”发布中间层数据包。"
        )
        self.capability_label.setWordWrap(True)
        self.capability_label.setStyleSheet(
            "QLabel { background:#f7f8fa; border:1px solid #c8cdd4; "
            "border-radius:6px; padding:7px; }"
        )
        layout.addWidget(self.capability_label)

        self.result_tabs = QTabWidget()
        self.calibration_view = CalibrationTab(
            self.cfg, show_input_controls=False
        )
        self.map_view = Map25DTab(self.cfg)
        self.quality_view = QTextBrowser()
        self.quality_view.setPlaceholderText(
            "运行完成后显示中间层来源、对象数量和跨相机一致性摘要。"
        )
        self.result_tabs.addTab(
            self.calibration_view, "标定与平面图"
        )
        self.result_tabs.addTab(self.map_view, "2.5D 地图")
        self.result_tabs.addTab(self.quality_view, "对象一致性")
        layout.addWidget(self.result_tabs, 1)
        self.btn_run.setEnabled(False)

    def set_dataset(self, dataset: StoreDataset) -> None:
        """同步当前输入，只用于结果展示，不在本页重新选择数据集。"""

        self.dataset = dataset
        self.calibration_view.apply_dataset(dataset, notify=False)
        self.map_view.set_geojson(
            {"type": "FeatureCollection", "features": []},
            floor_plan_path=dataset.floor_plan_path,
        )

    def set_intermediate(self, path: str | Path | None) -> None:
        """兼容旧调用：更新“跟随本次输入优化输出”的路径。"""

        self.source_selector.set_follow_path(path)

    def _apply_intermediate(self, path: str | Path | None) -> None:
        """加载来源选择器当前解析出的中间层并刷新候选能力。"""

        self.intermediate_path = (
            Path(path).expanduser().resolve() if path else None
        )
        self.output_path = None
        self.btn_open.setEnabled(False)
        self.quality_view.clear()
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        if self.intermediate_path is not None:
            try:
                self.candidate_combo.addItems(
                    candidate_names(self.intermediate_path)
                )
            except ValueError as exc:
                self.capability_label.setText(str(exc))
        self.candidate_combo.blockSignals(False)
        self._refresh_capability()

    def selected_candidate(self) -> str:
        """返回本页当前选择的标定候选。"""

        return self.candidate_combo.currentText().strip()

    def selected_mode(self) -> str:
        """返回基础、SfM 辅助或注册相机对照模式。"""

        return str(self.mode_combo.currentData() or "baseline")

    def _on_candidate_changed(self, candidate_text: str) -> None:
        """切换候选时清除旧候选输出入口并重新判断能力。"""

        self.output_path = None
        self.btn_open.setEnabled(False)
        self.quality_view.clear()
        self._refresh_capability(candidate_text)

    def _refresh_capability(self, _candidate_text: str = "") -> None:
        """统一显示 2.5D 能力和缺失资源。"""

        candidate = self.selected_candidate()
        if self.intermediate_path is None or not candidate:
            self.btn_run.setEnabled(False)
            self.capability_label.setText(
                "请先在“输入优化”运行标定并发布中间层数据包。"
            )
            return
        try:
            report = resolve_capabilities(
                self.intermediate_path,
                candidate_name=candidate,
            )
        except ValueError as exc:
            self.btn_run.setEnabled(False)
            self.capability_label.setText(str(exc))
            return
        mode = self.selected_mode()
        mode_ready = mode == "baseline" or report.sfm_assisted.ready
        self.btn_run.setEnabled(report.map25d.ready and mode_ready)
        if report.map25d.ready and mode_ready:
            warning = "；".join(report.map25d.warnings) or "无"
            self.capability_label.setText(
                f"2.5D 数据完整｜候选：{candidate}｜模式：{mode}｜"
                f"可信等级：{report.map25d.trust_level}｜告警：{warning}｜"
                "SfM 只约束辅助模式，不阻塞基础建图。"
            )
        elif report.map25d.ready:
            self.capability_label.setText(
                "当前中间层没有与候选匹配的真实三维观测，"
                "请改用基础建图或选择带 SfM 证据的派生中间层。"
            )
        else:
            self.capability_label.setText(
                "2.5D 数据不完整："
                + "、".join(report.map25d.missing)
            )

    def _request_run(self) -> None:
        """把已解析中间层和候选交给主窗口后台任务。"""

        if self.intermediate_path is None:
            return
        self.run_requested.emit(
            self.intermediate_path,
            self.selected_candidate(),
            self.selected_mode(),
        )

    def set_running(self, running: bool) -> None:
        """业务运行期间禁用重复请求。"""

        if running:
            self.btn_run.setEnabled(False)
            self.capability_label.setText("正在运行 2.5D 建图…")
        else:
            self._refresh_capability()

    def set_result(self, result: Map25DWorkflowResult) -> None:
        """载入 GeoJSON、平面图和跨相机质量摘要。"""

        self.output_path = result.output_dir
        self.btn_open.setEnabled(result.output_dir.is_dir())
        self.set_dataset(result.dataset)
        self.map_view.set_geojson(
            result.map25d,
            floor_plan_path=result.dataset.floor_plan_path,
            preview_path=result.preview_path,
            review_geojson=result.review_map25d,
            output_dir=result.output_dir,
        )
        self.quality_view.setPlainText(
            "2.5D 独立输出\n\n"
            f"标定候选：{result.candidate_name}\n"
            f"建图模式：{result.mode}\n"
            f"对象数量：{len(result.objects)}\n"
            f"待复核候选数量：{len(result.review_map25d.get('features', []))}\n"
            f"输出目录：{result.output_dir}\n\n"
            "跨相机对象一致性：\n"
            + json.dumps(
                result.alignment_summary,
                ensure_ascii=False,
                indent=2,
            )
        )
        self.result_tabs.setCurrentWidget(self.map_view)
        self.set_running(False)

    def set_failed(self, message: str) -> None:
        """保持旧结果可见并展示本轮失败。"""

        self.set_running(False)
        self.capability_label.setText(f"2.5D 建图失败：{message}")

    def _open_output(self) -> None:
        """使用系统文件管理器打开本轮 2.5D 输出。"""

        if self.output_path is not None and self.output_path.is_dir():
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.output_path))
            )


__all__ = ["Map25DWorkflowTab"]
