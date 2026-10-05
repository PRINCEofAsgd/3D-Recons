"""一级“图像拼接”页：参数驱动映射、覆盖诊断与融合结果。"""

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

from store_vision.data.workspace import candidate_names, resolve_capabilities
from store_vision.config import StoreConfig
from store_vision.mapping.parameter_stitcher import StitchingWorkflowResult
from store_vision.ui.widgets import ImageGraphicsView
from store_vision.ui.widgets.intermediate_source import IntermediateSourceSelector


class StitchingWorkflowTab(QWidget):
    """只消费中间层 K/D/R/t 和地面合同的独立图像拼接页面。"""

    run_requested = pyqtSignal(object, str)  # 中间层 Path、候选名
    status_message = pyqtSignal(str)

    def __init__(self, cfg: StoreConfig | None = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg or StoreConfig()
        self.intermediate_path: Path | None = None
        self.output_path: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """按精确参数项目流程构建运行入口与四类可视化。"""

        layout = QVBoxLayout(self)
        self.source_selector = IntermediateSourceSelector(self.cfg)
        self.source_selector.path_changed.connect(self._apply_intermediate)
        layout.addWidget(self.source_selector)
        bar = QHBoxLayout()
        self.btn_run = QPushButton("运行参数驱动拼接")
        self.btn_run.clicked.connect(self._request_run)
        self.candidate_combo = QComboBox()
        self.candidate_combo.currentTextChanged.connect(
            self._on_candidate_changed
        )
        self.btn_open = QPushButton("打开本轮输出")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_output)
        bar.addWidget(self.btn_run)
        bar.addWidget(QLabel("标定候选："))
        bar.addWidget(self.candidate_combo)
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
        self.views: dict[str, ImageGraphicsView] = {}
        for key, title in (
            ("fused", "融合大图"),
            ("alpha", "透明叠加"),
            ("overlap", "重叠区域"),
            ("coverage", "相机覆盖"),
        ):
            view = ImageGraphicsView()
            self.views[key] = view
            self.result_tabs.addTab(view, title)
        self.report_view = QTextBrowser()
        self.report_view.setPlaceholderText(
            "运行完成后显示画布、覆盖、候选来源和可信状态。"
        )
        self.result_tabs.addTab(self.report_view, "报告")
        layout.addWidget(self.result_tabs, 1)
        self.btn_run.setEnabled(False)

    def set_intermediate(self, path: str | Path | None) -> None:
        """兼容旧调用：更新“跟随本次输入优化输出”的路径。"""

        self.source_selector.set_follow_path(path)

    def _apply_intermediate(self, path: str | Path | None) -> None:
        """加载来源选择器当前解析出的中间层并列出候选。"""

        from PyQt6.QtGui import QPixmap

        self.intermediate_path = (
            Path(path).expanduser().resolve() if path else None
        )
        self.output_path = None
        self.btn_open.setEnabled(False)
        self.report_view.clear()
        for view in self.views.values():
            view.set_pixmap(QPixmap())
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
        """返回本页当前候选。"""

        return self.candidate_combo.currentText().strip()

    def _on_candidate_changed(self, candidate_text: str) -> None:
        """切换候选时清除旧输出入口并重新判断能力。"""

        from PyQt6.QtGui import QPixmap

        self.output_path = None
        self.btn_open.setEnabled(False)
        self.report_view.clear()
        for view in self.views.values():
            view.set_pixmap(QPixmap())
        self._refresh_capability(candidate_text)

    def _refresh_capability(self, _candidate_text: str = "") -> None:
        """显示参数驱动拼接能力和精确缺失字段。"""

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
        self.btn_run.setEnabled(report.stitching.ready)
        if report.stitching.ready:
            warning = "；".join(report.stitching.warnings) or "无"
            self.capability_label.setText(
                f"拼接数据完整｜候选：{candidate}｜"
                f"可信等级：{report.stitching.trust_level}｜告警：{warning}｜"
                "将由 K/D/R/t 推导世界地面映射，不读取 2.5D 输出。"
            )
        else:
            self.capability_label.setText(
                "拼接数据不完整："
                + "、".join(report.stitching.missing)
            )

    def _request_run(self) -> None:
        """把中间层和候选交给主窗口后台执行。"""

        if self.intermediate_path is None:
            return
        self.run_requested.emit(
            self.intermediate_path,
            self.selected_candidate(),
        )

    def set_running(self, running: bool) -> None:
        """运行期间阻止重复启动。"""

        if running:
            self.btn_run.setEnabled(False)
            self.capability_label.setText("正在执行参数驱动拼接…")
        else:
            self._refresh_capability()

    def set_result(self, result: StitchingWorkflowResult) -> None:
        """载入四类图片与结构化报告。"""

        from PyQt6.QtGui import QPixmap

        self.output_path = result.output_dir
        self.btn_open.setEnabled(result.output_dir.is_dir())
        for key, path in (
            ("fused", result.fused_path),
            ("alpha", result.alpha_path),
            ("coverage", result.coverage_path),
            ("overlap", result.overlap_path),
        ):
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.views[key].set_pixmap(pixmap)
        self.report_view.setPlainText(
            "参数驱动图像拼接\n\n"
            f"标定候选：{result.candidate_name}\n"
            f"输出目录：{result.output_dir}\n\n"
            + json.dumps(result.summary, ensure_ascii=False, indent=2)
        )
        self.result_tabs.setCurrentWidget(self.views["fused"])
        self.set_running(False)

    def set_failed(self, message: str) -> None:
        """保留旧结果并展示本轮失败。"""

        self.set_running(False)
        self.capability_label.setText(f"图像拼接失败：{message}")

    def _open_output(self) -> None:
        """用系统文件管理器打开本轮拼接结果。"""

        if self.output_path is not None and self.output_path.is_dir():
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.output_path))
            )


__all__ = ["StitchingWorkflowTab"]
