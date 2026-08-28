"""业务页共用的中间层来源选择器。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from store_vision.config import StoreConfig
from store_vision.data.workspace import (
    IntermediatePackageSummary,
    discover_intermediate_packages,
    load_manifest,
)


class IntermediateSourceSelector(QWidget):
    """在“跟随本次输出”和“自由选择已有包”之间安全切换。"""

    path_changed = pyqtSignal(object)

    def __init__(self, cfg: StoreConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._follow_path: Path | None = None
        self._free_path: Path | None = None
        self._packages: dict[str, list[IntermediatePackageSummary]] = {}
        self._build_ui()
        self.refresh_packages()

    def _build_ui(self) -> None:
        """构建两条来源路线及数据集、版本选择控件。"""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("跟随本次输入优化输出", "follow")
        self.mode_combo.addItem("自由选择已有中间层", "free")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.dataset_combo = QComboBox()
        self.dataset_combo.currentTextChanged.connect(self._on_dataset_changed)
        self.run_combo = QComboBox()
        self.run_combo.currentIndexChanged.connect(self._on_run_changed)
        self.btn_refresh = QPushButton("重新扫描")
        self.btn_refresh.clicked.connect(self.refresh_packages)
        self.btn_browse = QPushButton("选择中间层目录…")
        self.btn_browse.clicked.connect(self._browse)
        layout.addWidget(QLabel("中间层来源："))
        layout.addWidget(self.mode_combo)
        layout.addWidget(self.dataset_combo)
        layout.addWidget(self.run_combo)
        layout.addWidget(self.btn_refresh)
        layout.addWidget(self.btn_browse)
        layout.addStretch(1)
        self._update_enabled_state()

    def is_follow_mode(self) -> bool:
        """返回当前是否跟随本次输入优化输出。"""

        return self.mode_combo.currentData() == "follow"

    def current_path(self) -> Path | None:
        """返回当前路线解析出的中间层路径。"""

        return self._follow_path if self.is_follow_mode() else self._free_path

    def set_follow_path(self, path: str | Path | None) -> None:
        """更新本会话输入优化输出；自由选择状态不会被覆盖。"""

        self._follow_path = Path(path).expanduser().resolve() if path else None
        if self.is_follow_mode():
            self.path_changed.emit(self._follow_path)

    def refresh_packages(self) -> None:
        """重新发现工作区中全部合法 ``run_*/manifest.json``。"""

        root = Path(self.cfg.workspace_data_root).expanduser().resolve()
        grouped: dict[str, list[IntermediatePackageSummary]] = defaultdict(list)
        for summary in discover_intermediate_packages(root / "intermediate"):
            grouped[summary.dataset_id].append(summary)
        previous_dataset = self.dataset_combo.currentText()
        self._packages = dict(grouped)
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        self.dataset_combo.addItems(sorted(self._packages))
        if previous_dataset in self._packages:
            self.dataset_combo.setCurrentText(previous_dataset)
        self.dataset_combo.blockSignals(False)
        self._populate_runs(self.dataset_combo.currentText())

    def _populate_runs(self, dataset_id: str) -> None:
        """列出指定数据集的不可变中间层版本。"""

        self.run_combo.blockSignals(True)
        self.run_combo.clear()
        for summary in self._packages.get(dataset_id, []):
            label = (
                f"{summary.package_path.name}｜{summary.producer}｜"
                f"schema {summary.schema_version}"
            )
            self.run_combo.addItem(label, summary.package_path)
        self.run_combo.blockSignals(False)
        self._on_run_changed(self.run_combo.currentIndex())

    def _on_mode_changed(self) -> None:
        """切换来源路线时更新控件和业务页实际路径。"""

        self._update_enabled_state()
        self.path_changed.emit(self.current_path())

    def _update_enabled_state(self) -> None:
        """跟随模式不允许旧包选择器误改当前来源。"""

        enabled = not self.is_follow_mode()
        for widget in (
            self.dataset_combo,
            self.run_combo,
            self.btn_refresh,
            self.btn_browse,
        ):
            widget.setEnabled(enabled)

    def _on_dataset_changed(self, dataset_id: str) -> None:
        """数据集变化后自动选择其最新有效版本。"""

        self._populate_runs(dataset_id)

    def _on_run_changed(self, index: int) -> None:
        """保存自由选择路径，并只在自由模式通知业务页。"""

        value = self.run_combo.itemData(index) if index >= 0 else None
        self._free_path = Path(value).resolve() if value else None
        if not self.is_follow_mode():
            self.path_changed.emit(self._free_path)

    def _browse(self) -> None:
        """允许选择工作区外的合法中间层包，不扫描任意文件名。"""

        selected = QFileDialog.getExistingDirectory(
            self,
            "选择包含 manifest.json 的中间层目录",
            str(Path(self.cfg.workspace_data_root).expanduser().resolve()),
        )
        if not selected:
            return
        path = Path(selected).expanduser().resolve()
        try:
            load_manifest(path)
        except ValueError as exc:
            QMessageBox.information(self, "中间层无效", str(exc))
            return
        self._free_path = path
        self.path_changed.emit(path)


__all__ = ["IntermediateSourceSelector"]
