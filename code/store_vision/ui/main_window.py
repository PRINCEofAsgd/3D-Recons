"""Store Vision 三层桌面工作流主窗口。"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMainWindow, QStatusBar, QTabWidget

from store_vision.config import StoreConfig
from store_vision.data.models import StoreDataset
from store_vision.data.workspace import (
    create_unique_run_directory,
    find_latest_intermediate_package,
    load_manifest,
    workspace_paths,
)
from store_vision.mapping.map25d_pipeline import run_map25d_from_intermediate
from store_vision.mapping.parameter_stitcher import (
    ParameterStitchConfig,
    run_parameter_stitching,
)
from store_vision.ui.app_icon import apply_application_icon
from store_vision.ui.app_identity import (
    MAIN_WINDOW_TITLE,
    apply_application_identity,
)
from store_vision.ui.tabs.tab_input import InputTab
from store_vision.ui.tabs.tab_map25d_workflow import Map25DWorkflowTab
from store_vision.ui.tabs.tab_stitching_workflow import StitchingWorkflowTab

logger = logging.getLogger(__name__)


def next_dataset_output_directory(
    output_root: str | Path,
    dataset_name: str,
) -> Path:
    """保留旧 API：为兼容命令和测试规划不覆盖的两位序号目录。"""

    root = Path(output_root).expanduser().resolve()
    candidate = root / dataset_name
    if not candidate.exists():
        return candidate
    for sequence in range(1, 100):
        candidate = root / f"{dataset_name}_{sequence:02d}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"输出目录序号已用尽：{root}/{dataset_name}_01.._99")


class _Map25DWorker(QThread):
    """在独立线程从不可变中间层生成 2.5D 业务输出。"""

    done = pyqtSignal(object)
    failed = pyqtSignal(object, str)

    def __init__(
        self,
        intermediate_path: str | Path,
        output_root: str | Path,
        candidate_name: str,
        mode: str,
        cfg: StoreConfig,
    ):
        super().__init__()
        self.intermediate_path = Path(intermediate_path).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.candidate_name = candidate_name
        self.mode = mode
        self.cfg = cfg

    def run(self) -> None:
        """创建唯一业务运行目录并执行 2.5D 流程。"""

        try:
            output = create_unique_run_directory(self.output_root)
            result = run_map25d_from_intermediate(
                self.intermediate_path,
                output,
                candidate_name=self.candidate_name,
                mode=self.mode,
                cfg=self.cfg,
            )
            self.done.emit(result)
        except Exception as exc:
            logger.exception("2.5D workflow failed")
            self.failed.emit(self.intermediate_path, str(exc))


class _StitchingWorker(QThread):
    """在独立线程运行精确参数式世界地面拼接。"""

    done = pyqtSignal(object)
    failed = pyqtSignal(object, str)

    def __init__(
        self,
        intermediate_path: str | Path,
        output_root: str | Path,
        candidate_name: str,
        cfg: StoreConfig,
    ):
        super().__init__()
        self.intermediate_path = Path(intermediate_path).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.candidate_name = candidate_name
        self.cfg = cfg

    def run(self) -> None:
        """创建唯一业务运行目录并执行参数驱动拼接。"""

        try:
            output = create_unique_run_directory(self.output_root)
            config = ParameterStitchConfig(
                pixels_per_metre=self.cfg.parameter_stitch_pixels_per_metre,
                fallback_radius_metres=(
                    self.cfg.parameter_stitch_fallback_radius_metres
                ),
                max_ground_distance_metres=(
                    self.cfg.parameter_stitch_max_ground_distance_metres
                ),
                feather_radius_pixels=max(
                    1, int(round(self.cfg.stitch_feather_px))
                ),
                max_canvas_long_edge=(
                    self.cfg.parameter_stitch_max_canvas_long_edge
                ),
                max_canvas_pixels=self.cfg.parameter_stitch_max_canvas_pixels,
            )
            result = run_parameter_stitching(
                self.intermediate_path,
                output,
                candidate_name=self.candidate_name,
                config=config,
            )
            self.done.emit(result)
        except Exception as exc:
            logger.exception("parameter stitching workflow failed")
            self.failed.emit(self.intermediate_path, str(exc))


class MainWindow(QMainWindow):
    """连接输入优化、中间层能力和两条相互独立的业务输出。"""

    def __init__(self, cfg: StoreConfig | None = None):
        super().__init__()
        self.cfg = cfg or StoreConfig()
        application = QApplication.instance()
        # 兼容嵌入式与测试入口，不依赖唯一 CLI 预先设置应用身份。
        apply_application_identity(application)
        self.setWindowTitle(MAIN_WINDOW_TITLE)
        apply_application_icon(application, self)
        self.resize(1500, 950)

        self.tabs = QTabWidget()
        self.tab_input = InputTab(self.cfg)
        self.tab_calibration_optimization = self.tab_input.optimization
        self.tab_map25d = Map25DWorkflowTab(self.cfg)
        self.tab_stitching = StitchingWorkflowTab(self.cfg)
        self.tabs.addTab(self.tab_input, "1. 输入优化")
        self.tabs.addTab(self.tab_map25d, "2. 2.5D 建图")
        self.tabs.addTab(self.tab_stitching, "3. 图像拼接")
        self.setCentralWidget(self.tabs)

        # 兼容只读展示调用的旧属性名；不再创建旧多相机拼接子页面。
        self.tab_calib = self.tab_map25d.calibration_view
        self.tab_map = self.tab_map25d.map_view
        self.tab_stitch = self.tab_stitching

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("空闲")
        self._map25d_worker: _Map25DWorker | None = None
        self._stitching_worker: _StitchingWorker | None = None
        self._current_dataset: StoreDataset | None = None
        self._current_intermediate: Path | None = None

        self.tab_input.dataset_changed.connect(self._on_dataset_changed)
        self.tab_calibration_optimization.status_message.connect(
            self.statusBar().showMessage
        )
        self.tab_calibration_optimization.intermediate_published.connect(
            self._on_intermediate_published
        )
        self.tab_map25d.run_requested.connect(self._run_map25d)
        self.tab_stitching.run_requested.connect(self._run_stitching)

    def load_folder(self, folder: str) -> None:
        """加载当前格式输入；统一工作区路径由数据集名称自动确定。"""

        self.tab_input.load_folder(folder)

    def _on_dataset_changed(self, dataset: StoreDataset) -> None:
        """切换数据集时清除旧中间层引用并原子同步三个页面。"""

        self._current_dataset = dataset
        self._current_intermediate = None
        paths = workspace_paths(dataset.root, self.cfg.workspace_data_root)
        self.tab_input.plan_output_directory(paths.intermediate_root)
        self.tab_calibration_optimization.set_session_context(
            dataset.root,
            paths.intermediate_root,
        )
        self.tab_map25d.set_dataset(dataset)
        latest = find_latest_intermediate_package(
            paths.intermediate_root,
            dataset.root,
        )
        self._current_intermediate = latest
        self.tab_map25d.set_intermediate(latest)
        self.tab_stitching.set_intermediate(latest)
        if latest is None:
            self.statusBar().showMessage(
                f"已加载 {paths.dataset_id}；请运行输入优化发布中间层"
            )
        else:
            self.statusBar().showMessage(
                f"已加载 {paths.dataset_id} 的最新中间层：{latest.name}"
            )

    def _on_intermediate_published(self, path: str | Path) -> None:
        """把本轮中间层一次分发给两个业务页的能力解析器。"""

        intermediate = Path(path).expanduser().resolve()
        self._current_intermediate = intermediate
        self.tab_map25d.set_intermediate(intermediate)
        self.tab_stitching.set_intermediate(intermediate)
        self.statusBar().showMessage(f"中间层已发布：{intermediate}")

    def _business_output_root(
        self,
        intermediate_path: str | Path,
        *,
        stitching: bool,
    ) -> Path:
        """按所选中间层身份返回独立业务输出根，不依赖输入页状态。"""

        manifest = load_manifest(intermediate_path)
        dataset_id = str(manifest["dataset_id"])
        data_root = Path(self.cfg.workspace_data_root).expanduser().resolve()
        return (
            data_root / "stitching_output" / dataset_id
            if stitching
            else data_root / "map25d_output" / dataset_id
        )

    def _run_map25d(
        self,
        intermediate_path: Path,
        candidate_name: str,
        mode: str,
    ) -> None:
        """启动只读取中间层的 2.5D 后台任务。"""

        if (
            self._map25d_worker is not None
            and self._map25d_worker.isRunning()
        ):
            self.statusBar().showMessage("2.5D 建图仍在运行…")
            return
        try:
            output_root = self._business_output_root(
                intermediate_path, stitching=False
            )
        except ValueError as exc:
            self.tab_map25d.set_failed(str(exc))
            return
        self.tab_map25d.set_running(True)
        self.statusBar().showMessage("正在从中间层生成 2.5D 业务输出…")
        self._map25d_worker = _Map25DWorker(
            intermediate_path,
            output_root,
            candidate_name,
            mode,
            self.cfg,
        )
        self._map25d_worker.done.connect(self._on_map25d_done)
        self._map25d_worker.failed.connect(self._on_map25d_failed)
        self._map25d_worker.start()

    def _on_map25d_done(self, result) -> None:
        """展示本轮 2.5D 输出并保持中间层候选不变。"""

        if result.intermediate_path != self.tab_map25d.intermediate_path:
            self.tab_map25d.set_running(False)
            self.statusBar().showMessage(
                "2.5D 结果属于上一中间层，未载入当前页面"
            )
            return
        self.tab_map25d.set_result(result)
        self.statusBar().showMessage(
            f"2.5D 建图完成：{len(result.objects)} 个对象 → "
            f"{result.output_dir}"
        )

    def _on_map25d_failed(self, source: Path, message: str) -> None:
        """恢复页面运行入口并显示 2.5D 失败原因。"""

        if source != self.tab_map25d.intermediate_path:
            self.tab_map25d.set_running(False)
            self.statusBar().showMessage("上一中间层的 2.5D 任务失败")
            return
        self.tab_map25d.set_failed(message)
        self.statusBar().showMessage(f"2.5D 建图失败：{message}")

    def _run_stitching(
        self,
        intermediate_path: Path,
        candidate_name: str,
    ) -> None:
        """启动只读取 K/D/R/t 与地面合同的拼接后台任务。"""

        if (
            self._stitching_worker is not None
            and self._stitching_worker.isRunning()
        ):
            self.statusBar().showMessage("图像拼接仍在运行…")
            return
        try:
            output_root = self._business_output_root(
                intermediate_path, stitching=True
            )
        except ValueError as exc:
            self.tab_stitching.set_failed(str(exc))
            return
        self.tab_stitching.set_running(True)
        self.statusBar().showMessage("正在执行参数驱动图像拼接…")
        self._stitching_worker = _StitchingWorker(
            intermediate_path,
            output_root,
            candidate_name,
            self.cfg,
        )
        self._stitching_worker.done.connect(self._on_stitching_done)
        self._stitching_worker.failed.connect(self._on_stitching_failed)
        self._stitching_worker.start()

    def _on_stitching_done(self, result) -> None:
        """展示融合、透明叠加、覆盖和重叠诊断结果。"""

        if result.intermediate_path != self.tab_stitching.intermediate_path:
            self.tab_stitching.set_running(False)
            self.statusBar().showMessage(
                "拼接结果属于上一中间层，未载入当前页面"
            )
            return
        self.tab_stitching.set_result(result)
        self.statusBar().showMessage(
            f"图像拼接完成：{result.candidate_name} → {result.output_dir}"
        )

    def _on_stitching_failed(self, source: Path, message: str) -> None:
        """恢复页面运行入口并显示拼接失败原因。"""

        if source != self.tab_stitching.intermediate_path:
            self.tab_stitching.set_running(False)
            self.statusBar().showMessage("上一中间层的拼接任务失败")
            return
        self.tab_stitching.set_failed(message)
        self.statusBar().showMessage(f"图像拼接失败：{message}")

    def rerun_pipeline(self) -> None:
        """兼容旧入口：改为提示用户在目标业务页单独运行。"""

        self.statusBar().showMessage(
            "完整处理已拆分；请在 2.5D 建图或图像拼接页运行目标业务"
        )


__all__ = ["MainWindow", "next_dataset_output_directory"]
