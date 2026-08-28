"""UI smoke tests, kept light to avoid Qt event-loop issues."""

from __future__ import annotations

import json


def test_image_view_smoke(qapp):
    from PyQt6.QtCore import QEvent, QPoint, Qt
    from PyQt6.QtGui import QKeySequence, QPixmap
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton

    from store_vision.ui.widgets import ImageGraphicsView, ZoomImageDialog

    v = ImageGraphicsView(
        allow_double_click_fit=False,
        command_only_zoom=True,
    )
    v.resize(640, 480)
    v.show()
    v.fit()
    v.set_pixmap(QPixmap(1200, 800))
    qapp.processEvents()
    fitted_scale = v.transform().m11()
    v.zoom_in()
    assert v.transform().m11() > fitted_scale
    v.zoom_out()
    assert abs(v.transform().m11() - fitted_scale) < 1e-9
    assert len(v._zoom_shortcuts) >= 2
    assert all(
        shortcut.context() == Qt.ShortcutContext.WindowShortcut
        for shortcut in v._zoom_shortcuts
    )

    class NativeZoomGesture:
        accepted = False

        @staticmethod
        def type():
            return QEvent.Type.NativeGesture

        @staticmethod
        def gestureType():
            return Qt.NativeGestureType.ZoomNativeGesture

        @staticmethod
        def value():
            return 0.05

        def accept(self):
            self.accepted = True

    gesture = NativeZoomGesture()
    before_gesture = v.transform().m11()
    assert v._handle_native_gesture(gesture)
    assert gesture.accepted
    assert v.transform().m11() == before_gesture

    class TwoFingerScroll:
        """模拟 macOS 触控板发送给 Qt 的像素滚动事件。"""

        accepted = False

        @staticmethod
        def pixelDelta():
            return QPoint(12, 18)

        @staticmethod
        def angleDelta():
            return QPoint()

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    v.zoom_by(3)
    qapp.processEvents()
    horizontal = v.horizontalScrollBar()
    vertical = v.verticalScrollBar()
    horizontal.setValue(horizontal.maximum() // 2)
    vertical.setValue(vertical.maximum() // 2)
    scroll = TwoFingerScroll()
    before_scroll_scale = v.transform().m11()
    before_scroll_position = (horizontal.value(), vertical.value())
    v.wheelEvent(scroll)
    assert scroll.accepted
    assert v.transform().m11() == before_scroll_scale
    assert (horizontal.value(), vertical.value()) != before_scroll_position

    dialog = ZoomImageDialog("缩放测试", QPixmap(1200, 800))
    assert "双指" in dialog.lbl.text()
    assert "⌘ +/-" in dialog.lbl.text()
    assert not hasattr(dialog, "btn_zoom_in")
    assert not hasattr(dialog, "btn_zoom_out")
    assert not hasattr(dialog, "btn_fit")
    assert {button.text() for button in dialog.findChildren(QPushButton)} == {"关闭"}
    assert any(
        sequence.toString(QKeySequence.SequenceFormat.NativeText) == "⌘W"
        for shortcut in dialog._close_shortcuts
        for sequence in (shortcut.key(),)
    )
    dialog.show()
    dialog.activateWindow()
    dialog.view.setFocus()
    qapp.processEvents()
    dialog_scale = dialog.view.transform().m11()
    QTest.keyClick(
        dialog.view,
        Qt.Key.Key_Equal,
        Qt.KeyboardModifier.ControlModifier,
    )
    qapp.processEvents()
    assert dialog.view.transform().m11() > dialog_scale
    QTest.keyClick(
        dialog.view,
        Qt.Key.Key_Minus,
        Qt.KeyboardModifier.ControlModifier,
    )
    qapp.processEvents()
    assert abs(dialog.view.transform().m11() - dialog_scale) < 1e-9
    QTest.keyClick(
        dialog.view,
        Qt.Key.Key_Plus,
        Qt.KeyboardModifier.ControlModifier,
    )
    qapp.processEvents()
    assert dialog.view.transform().m11() > dialog_scale
    QTest.keyClick(
        dialog,
        Qt.Key.Key_W,
        Qt.KeyboardModifier.ControlModifier,
    )
    qapp.processEvents()
    assert not dialog.isVisible()
    v.close()


def test_polygon_view_smoke(qapp):
    from store_vision.ui.widgets import PolygonImageView
    v = PolygonImageView()
    v.show()
    v.reset_polygon()
    v.close()


def test_calibration_optimization_tab_smoke(qapp, tmp_path):
    """输入工作区按标定优化、Sfm、报告三个步骤组织。"""

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QHeaderView, QLineEdit, QPushButton, QSplitter

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationOptimizationTab,
    )
    from store_vision.ui.widgets import ImageThumbnail

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    output = tmp_path / "outputs" / "dataset"
    tab = CalibrationOptimizationTab(StoreConfig(output_root=tmp_path / "outputs"))
    tab.set_session_context(dataset, output)
    tab.load_current_results()
    assert tab.result_tabs.count() == 3
    assert len(tab.stage_labels) == 6
    assert [tab.result_tabs.tabText(index) for index in range(3)] == [
        "标定优化",
        "Sfm",
        "报告",
    ]
    assert tab.calibration_result_tabs.tabText(0) == "共享 K/D"
    assert tab.calibration_result_tabs.tabText(1) == "相机位姿"
    assert tab.calibration_result_tabs.tabText(2) == "拟合与 BA"
    assert tab.sfm_result_tabs.tabText(0) == "候选与验收"
    assert tab.sfm_result_tabs.tabText(1) == "匹配与几何"
    assert tab.sfm_result_tabs.count() == 2
    assert tab.report_result_tabs.tabText(0) == "概览"
    assert tab.report_result_tabs.tabText(1) == "产物"
    assert all(
        isinstance(label, ImageThumbnail)
        for label in (
            tab.intrinsics_image_label,
            tab.pose_image_label,
            tab.pair_image_label,
            tab.artifact_image_label,
        )
    )
    assert all(
        label.width() == 148 and label.height() == 100
        for label in (
            tab.intrinsics_image_label,
            tab.pose_image_label,
            tab.pair_image_label,
            tab.artifact_image_label,
        )
    )
    assert [
        (layout.stretch(0), layout.stretch(1))
        for layout in (
            tab.intrinsics_content_layout,
            tab.pose_content_layout,
            tab.baseline_content_layout,
        )
    ] == [(3, 1), (3, 1), (3, 1)]
    assert [
        (layout.stretch(0), layout.stretch(1))
        for layout in (
            tab.overview_content_layout,
            tab.artifact_content_layout,
        )
    ] == [(1, 1), (1, 1)]
    assert not tab.findChildren(QSplitter)
    for table in (
        tab.intrinsics_model_table,
        tab.pose_table,
        tab.camera_table,
        tab.metrics_table,
    ):
        assert table.wordWrap()
        assert (
            table.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert (
            table.horizontalHeader().sectionResizeMode(0)
            == QHeaderView.ResizeMode.Stretch
        )
    for text_view in (
        tab.intrinsics_detail_view,
        tab.pose_detail_view,
        tab.baseline_view,
        tab.findings_view,
        tab.artifact_text,
    ):
        assert (
            text_view.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
    assert not tab.findChildren(QLineEdit)
    button_texts = {button.text() for button in tab.findChildren(QPushButton)}
    assert "加载已有结果" not in button_texts
    assert "新建 COLMAP 实验" not in button_texts
    assert "运行 Sfm" in button_texts
    assert not any(button.text() == "选择…" for button in tab.findChildren(QPushButton))
    assert not tab.btn_run_analysis.isEnabled()
    tab.close()


def test_main_window_uses_three_business_layers(qapp):
    """主窗口按输入优化、2.5D 和拼接形成三个一级页面。"""

    from store_vision.ui import MainWindow

    window = MainWindow()
    assert window.tabs.count() == 3
    assert [window.tabs.tabText(index) for index in range(3)] == [
        "1. 输入优化",
        "2. 2.5D 建图",
        "3. 图像拼接",
    ]
    assert window.tab_map25d.calibration_view is window.tab_calib
    assert window.tab_map25d.map_view is window.tab_map
    window.close()


def test_business_pages_offer_follow_and_free_intermediate_routes(qapp, tmp_path):
    """2.5D 与拼接页必须各自拥有跟随和自由选择两条中间层路线。"""

    from store_vision.config import StoreConfig
    from store_vision.ui import MainWindow

    window = MainWindow(StoreConfig(workspace_data_root=tmp_path / "data"))
    for page in (window.tab_map25d, window.tab_stitching):
        selector = page.source_selector
        assert selector.mode_combo.count() == 2
        assert selector.mode_combo.itemData(0) == "follow"
        assert selector.mode_combo.itemData(1) == "free"
        assert selector.dataset_combo is not window.tab_input
    assert window.tab_map25d.source_selector is not window.tab_stitching.source_selector
    assert window.tab_map25d.mode_combo.count() == 3
    window.close()


def test_map25d_page_explains_trial_provenance(qapp, fixture_dir):
    """2.5D 页面必须展示平面背景和 XY/Z/SfM 来源，不能伪装成三维重建。"""

    from store_vision.config import StoreConfig
    from store_vision.data import load_store_folder
    from store_vision.ui.tabs.tab_map25d import Map25DTab

    dataset = load_store_folder(fixture_dir)
    tab = Map25DTab(StoreConfig())
    tab.set_geojson(
        {
            "type": "FeatureCollection",
            "features": [],
            "metadata": {
                "trust_level": "provisional_observation_only",
                "position_source": "cali planar mapping",
                "height_source": "fixed 70 cm",
                "sfm_status": "all_camera_visual",
            },
        },
        floor_plan_path=dataset.floor_plan_path,
    )

    assert tab._floor_rgb is not None
    assert "不是稠密三维重建" in tab.summary_label.text()
    assert "provisional_observation_only" in tab.summary_label.text()
    assert "all_camera_visual" in tab.summary_label.text()
    tab.close()


def test_map25d_page_realtime_selection_writes_review_result(
    qapp, tmp_path
):
    """复选框应立即改变可见对象，并写出独立人工复核结果。"""

    from store_vision.config import StoreConfig
    from store_vision.data.models import MapObject25D
    from store_vision.mapping import build_map25d
    from store_vision.ui.tabs.tab_map25d import Map25DTab

    automatic = MapObject25D(
        id="table_000",
        label="table",
        polygon_cm=[(0, 0), (100, 0), (100, 50), (0, 50)],
        height_cm=70,
        meta={"review_default_included": True},
    )
    weak = MapObject25D(
        id="review_weak_000",
        label="table",
        polygon_cm=[(200, 0), (300, 0), (300, 50), (200, 50)],
        height_cm=70,
        meta={"review_default_included": False},
    )
    candidates = build_map25d([automatic, weak])
    tab = Map25DTab(StoreConfig())
    tab.set_geojson(
        build_map25d([automatic]),
        review_geojson=candidates,
        output_dir=tmp_path,
    )

    assert tab._selected_feature_ids == {"table_000"}
    tab._feature_checks["review_weak_000"].setChecked(True)
    assert tab._selected_feature_ids == {"table_000", "review_weak_000"}
    reviewed = json.loads(
        (tmp_path / "map25d_reviewed.geojson").read_text(encoding="utf-8")
    )
    assert len(reviewed["features"]) == 2
    assert "2/2" in tab.summary_label.text()
    tab.close()


def test_application_icon_is_packaged_and_applied(qapp):
    """桌面进程和主窗口应使用随包安装的用户提供图标。"""
    from store_vision.ui import MainWindow
    from store_vision.ui.app_icon import (
        application_icon_path,
        apply_application_icon,
        load_application_icon,
    )

    path = application_icon_path()
    icon = load_application_icon()
    assert path.is_file()
    assert path.name == "app_icon.png"
    assert not icon.isNull()
    assert apply_application_icon(qapp)
    assert not qapp.windowIcon().isNull()

    window = MainWindow()
    assert not window.windowIcon().isNull()
    assert window.windowIcon().cacheKey() == qapp.windowIcon().cacheKey()
    window.close()


def test_desktop_instance_uses_vion_recons_name(qapp):
    """Qt 运行实例、显示名和主窗口标题应统一使用新品牌名称。"""
    from store_vision.ui import MainWindow
    from store_vision.ui.app_identity import (
        APPLICATION_NAME,
        MAIN_WINDOW_TITLE,
        apply_application_identity,
    )

    qapp.setApplicationName("旧名称")
    qapp.setApplicationDisplayName("旧显示名称")
    assert apply_application_identity(qapp)
    assert APPLICATION_NAME == "Vion Recons"
    assert qapp.applicationName() == APPLICATION_NAME
    assert qapp.applicationDisplayName() == APPLICATION_NAME

    window = MainWindow()
    assert window.windowTitle() == MAIN_WINDOW_TITLE
    assert window.windowTitle().startswith("Vion Recons")
    window.close()


def test_input_page_only_owns_dataset_and_optimization_entries(qapp, tmp_path):
    """输入优化页不再提供跨业务的完整处理入口。"""

    from PyQt6.QtWidgets import QPushButton

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_input import InputTab

    tab = InputTab(StoreConfig(output_root=tmp_path / "outputs"))
    button_texts = {button.text() for button in tab.findChildren(QPushButton)}
    assert "选择数据集" in button_texts
    assert "运行完整处理" not in button_texts
    assert "打开输出目录" not in button_texts
    assert {"运行鱼眼联合标定", "运行 Sfm", "运行匹配诊断"} <= button_texts
    assert "打开当前中间层目录" in button_texts
    assert not tab.optimization.btn_open_directory.isEnabled()
    output = tmp_path / "outputs" / "dataset"
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    tab.optimization.set_session_context(dataset, output)
    output.mkdir(parents=True)
    tab.optimization.refresh_action_availability()
    assert tab.optimization.btn_open_directory.isEnabled()
    tab.close()


def test_home_page_only_keeps_dataset_run_and_open_entries(qapp, tmp_path):
    """首页不再暴露平面图、相机图、标定文件或输出目录的独立选择入口。"""

    from PyQt6.QtWidgets import QPushButton

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_calibration import CalibrationTab

    tab = CalibrationTab(StoreConfig(output_root=tmp_path / "outputs"))
    button_texts = {
        button.text() for button in tab.findChildren(QPushButton)
    }
    assert {"选择数据集", "运行完整处理", "打开输出目录"} <= button_texts
    assert not any("平面图" in text for text in button_texts)
    assert not any("相机图片" in text for text in button_texts)
    assert not any("标定文件" in text for text in button_texts)
    assert not any("选择输出目录" in text for text in button_texts)
    assert not tab.btn_open_output.isEnabled()
    tab.close()


def test_output_directory_uses_two_digit_suffix_and_opens_only_after_creation(
    qapp, tmp_path
):
    """重名目录按两位序号递增，首页打开入口只在目录创建后启用。"""

    from store_vision.config import StoreConfig
    from store_vision.ui.main_window import next_dataset_output_directory
    from store_vision.ui.tabs.tab_calibration import CalibrationTab

    output_root = tmp_path / "outputs"
    (output_root / "store").mkdir(parents=True)
    (output_root / "store_01").mkdir()
    planned = next_dataset_output_directory(output_root, "store")
    assert planned == output_root / "store_02"

    tab = CalibrationTab(StoreConfig(output_root=output_root))
    tab.plan_output_directory(planned)
    assert not tab.btn_open_output.isEnabled()
    planned.mkdir()
    tab.mark_output_directory_created(planned)
    assert tab.btn_open_output.isEnabled()
    tab.close()


def test_calibration_tab_loads_shared_models_and_poses(qapp, tmp_path):
    """第 4 页能把共享主报告分发到模型表、位姿表和 BA 状态。"""

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationOptimizationTab,
    )

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    result = tmp_path / "run_001"
    result.mkdir()
    (result / "shared_calibration_report.json").write_text(
        json.dumps(
            {
                "input": {"dataset_path": str(dataset.resolve())},
                "scale": {"metric_unit": "metre", "rmse_metres": 0.01},
                "homographies": [{}, {}],
                "intrinsics_models": {
                    "FISHEYE": {
                        "definition": "opencv_fisheye_shared_K_D_joint_poses",
                        "K": [[1500, 0, 1920], [0, 1500, 1080], [0, 0, 1]],
                        "D": [0.1, -0.01, 0.001, 0.0],
                        "initialization": {
                            "fit": {"success": True, "reprojection_rmse_px": 2.0}
                        },
                        "bundle_adjustment": {
                            "performed": True,
                            "success": True,
                            "reprojection_rmse_px": 1.0,
                            "uses_fitted_calibration_observations": True,
                        },
                    }
                },
                "selection": {
                    "selected_model": "FISHEYE",
                    "selected_K": [[1500, 0, 1920], [0, 1500, 1080], [0, 0, 1]],
                    "selected_D": [0.1, -0.01, 0.001, 0.0],
                },
                "poses": [
                    {
                        "physical_camera_id": "camera-a",
                        "image_name": "camera-a.jpg",
                        "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                        "T_metres": [0, 0, 4],
                        "camera_center_world_metres": [0, 0, 4],
                        "height_metres": 4,
                        "downward_optical_axis_component": 1,
                        "pose_reprojection_rmse_px": 1,
                        "physical_plausibility_passed": True,
                    }
                ],
                "summary": {
                    "selected_model_stable": True,
                    "physical_plausibility_pass_rate": 1,
                    "fitted_initialization_available": True,
                    "bundle_adjustment_performed": True,
                    "bundle_adjustment_success": True,
                    "safe_as_ba_initialization": True,
                },
                "nonlinear_optimization": {
                    "initialization": {
                        "fit": {"success": True, "reprojection_rmse_px": 2.0}
                    },
                    "bundle_adjustment": {
                        "performed": True,
                        "success": True,
                        "reprojection_rmse_px": 1.0,
                        "uses_fitted_calibration_observations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    tab = CalibrationOptimizationTab(StoreConfig(output_root=tmp_path / "outputs"))
    tab.set_session_context(dataset, tmp_path / "outputs" / "dataset")
    tab._current_shared_result = result
    tab.load_current_results()

    assert tab.intrinsics_model_table.rowCount() == 1
    assert tab.pose_table.rowCount() == 1
    assert "已完成" in tab.ba_readiness_label.text()
    tab.close()


def test_calibration_page_uses_only_current_session_paths(qapp, tmp_path, monkeypatch):
    """鱼眼标定、COLMAP 和匹配诊断都使用第 1 页原子同步的当前路径。"""

    from PyQt6.QtWidgets import QMessageBox

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationOptimizationTab,
    )

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    output = tmp_path / "outputs" / "dataset_01"
    tab = CalibrationOptimizationTab(StoreConfig(output_root=tmp_path / "outputs"))
    tab.set_session_context(dataset, output)
    requests = []
    monkeypatch.setattr(tab, "_start_task", requests.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    tab._run_shared_intrinsics()
    tab._run_colmap_experiment()

    assert requests[0].dataset_path == dataset.resolve()
    assert requests[0].output_root == output.resolve()
    assert requests[0].config is tab.cfg
    assert requests[1].dataset_path == dataset.resolve()
    assert requests[1].output_root == output.resolve() / "calibration_demo"

    experiment = output / "calibration_demo" / "run_current"
    experiment.mkdir(parents=True)
    (experiment / "database.db").write_bytes(b"database")
    (experiment / "model_txt").mkdir()
    loaded = []
    monkeypatch.setattr(tab, "load_current_results", lambda: loaded.append(True))
    tab._on_task_done(
        {
            "experiment_path": experiment,
            "message": "COLMAP 完成",
        }
    )
    assert loaded == [True]
    assert tab._current_experiment == experiment.resolve()
    assert tab.btn_run_analysis.isEnabled()
    tab._run_available_analysis()
    assert requests[2].dataset_path == dataset.resolve()
    assert requests[2].experiment_path == experiment.resolve()
    tab.close()


def test_home_page_rejects_output_directory_as_dataset(qapp, tmp_path, monkeypatch):
    """首页明确拒绝把 outputs 中的历史结果误当作原始数据集。"""

    from PyQt6.QtWidgets import QMessageBox

    from store_vision.config import StoreConfig
    from store_vision.ui.tabs.tab_calibration import CalibrationTab

    output_root = tmp_path / "outputs"
    selected = output_root / "store"
    selected.mkdir(parents=True)
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )
    tab = CalibrationTab(StoreConfig(output_root=output_root))
    tab.load_folder(str(selected))

    assert tab.dataset is None
    assert messages
    assert "不能把 outputs 结果目录作为数据集" in messages[0][1]
    tab.close()


def test_calibration_worker_new_experiment_uses_unique_directory(qapp, tmp_path, monkeypatch):
    """GUI 新实验直接复用工作流，并把用户选择解释为运行目录父级。"""

    from types import SimpleNamespace

    from store_vision.calibration import workflow
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationTaskRequest,
        CalibrationTaskWorker,
    )

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    calls = []

    def fake_workflow(dataset_path, run_path, **kwargs):
        calls.append((dataset_path, run_path, kwargs))
        return SimpleNamespace(report_path=run_path / "reports" / "calibration_report.json", colmap_status="partial")

    monkeypatch.setattr(workflow, "run_calibration_workflow", fake_workflow)
    worker = CalibrationTaskWorker(
        CalibrationTaskRequest("new_experiment", dataset, output_root=tmp_path / "runs")
    )
    results = []
    worker.done.connect(results.append)
    worker.run()

    assert len(calls) == 1
    assert calls[0][0] == dataset
    assert calls[0][1].parent == (tmp_path / "runs").resolve()
    assert calls[0][2]["fisheye_calibration"] is None
    assert results[0]["experiment_path"] == calls[0][1]


def test_calibration_worker_available_analysis_only_runs_geometry(qapp, tmp_path, monkeypatch):
    """Sfm 辅助入口只运行只读几何诊断，不再触发盘点或 B0。"""

    from store_vision.calibration import geometry_diagnostics
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationTaskRequest,
        CalibrationTaskWorker,
    )

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "camera-a.jpg").write_bytes(b"image")
    (dataset / "cali.txt").write_text("{}", encoding="utf-8")
    experiment = tmp_path / "run_001"
    experiment.mkdir()
    (experiment / "database.db").write_bytes(b"database")
    (experiment / "model_txt").mkdir()
    calls = []

    def fake_geometry(request):
        calls.append("geometry")
        return {"geometry_diagnostics": {"status": "complete"}}

    monkeypatch.setattr(geometry_diagnostics, "run_geometry_diagnostics", fake_geometry)
    worker = CalibrationTaskWorker(
        CalibrationTaskRequest("available_analysis", dataset, experiment_path=experiment)
    )
    results = []
    worker.done.connect(results.append)
    worker.run()

    assert calls == ["geometry"]
    assert results[0]["geometry_status"] == "complete"
    assert results[0]["analysis_path"].parent == experiment / "analysis"


def test_calibration_worker_runs_shared_intrinsics_in_unique_directory(
    qapp, tmp_path, monkeypatch
):
    """共享标定后台任务复用正式函数并为每次运行分配唯一目录。"""

    from store_vision.calibration import shared_calibration
    from store_vision.data import workspace
    from store_vision.ui.tabs.tab_calibration_optimization import (
        CalibrationTaskRequest,
        CalibrationTaskWorker,
    )

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    calls = []

    def fake_shared(dataset_path, run_path, **kwargs):
        calls.append((dataset_path, run_path, kwargs))
        return {"selection": {"selected_model": "A"}}

    monkeypatch.setattr(
        shared_calibration,
        "run_shared_intrinsics_calibration",
        fake_shared,
    )
    published = []
    monkeypatch.setattr(
        workspace,
        "publish_intermediate_package",
        lambda *args, **kwargs: published.append((args, kwargs)) or {},
    )
    worker = CalibrationTaskWorker(
        CalibrationTaskRequest(
            "shared_intrinsics",
            dataset,
            output_root=tmp_path / "shared_intrinsics",
        )
    )
    results = []
    worker.done.connect(results.append)
    worker.run()

    assert len(calls) == 1
    assert calls[0][0] == dataset
    assert calls[0][1].parent == (tmp_path / "shared_intrinsics").resolve()
    assert results[0]["shared_result_path"] == calls[0][1]
    assert results[0]["intermediate_path"] == calls[0][1]
    assert results[0]["selected_model"] == "A"
    assert published
