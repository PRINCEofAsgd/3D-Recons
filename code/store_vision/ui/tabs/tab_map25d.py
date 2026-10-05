"""Tab 2 — 2.5D map viewer with view presets, wheel zoom, mode-toggle drag."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from store_vision.config import StoreConfig
from store_vision.mapping.map25d_review import (
    default_included_feature_ids,
    write_review_outputs,
)

VIEW_PRESETS = {
    "top": (90, -90, "俯视图"),
    "front": (10, -90, "正视图"),
    "side": (10, 0, "侧视图"),
}


class _Map3DCanvas(FigureCanvasQTAgg):
    def __init__(self, fig: Figure):
        super().__init__(fig)
        self.mode = "rotate"  # or "pan"
        self._dragging = False
        self._last = (0, 0)
        self.mpl_connect("scroll_event", self._on_scroll)
        self.mpl_connect("button_press_event", self._on_press)
        self.mpl_connect("button_release_event", self._on_release)
        self.mpl_connect("motion_notify_event", self._on_move)

    def _ax(self):
        return self.figure.axes[0] if self.figure.axes else None

    def set_view(self, elev: float, azim: float):
        ax = self._ax()
        if ax is None:
            return
        ax.view_init(elev=elev, azim=azim)
        self.draw_idle()

    def _on_scroll(self, event):
        ax = self._ax()
        if ax is None or event.inaxes is None:
            return
        factor = 0.85 if event.button == "up" else 1.18
        for getter, setter in (
            (ax.get_xlim, ax.set_xlim),
            (ax.get_ylim, ax.set_ylim),
            (ax.get_zlim, ax.set_zlim),
        ):
            lo, hi = getter()
            mid = (lo + hi) / 2
            half = (hi - lo) / 2 * factor
            setter(mid - half, mid + half)
        self.draw_idle()

    def _on_press(self, event):
        if event.button == 1 and event.inaxes:
            self._dragging = True
            self._last = (event.x, event.y)

    def _on_release(self, event):
        self._dragging = False

    def _on_move(self, event):
        if not self._dragging or event.inaxes is None or event.x is None:
            return
        ax = self._ax()
        if ax is None:
            return
        dx = event.x - self._last[0]
        dy = event.y - self._last[1]
        self._last = (event.x, event.y)
        if self.mode == "rotate":
            elev = float(ax.elev - dy * 0.4)
            azim = float(ax.azim + dx * 0.5)
            ax.view_init(elev=np.clip(elev, -90, 90), azim=azim)
        else:  # pan
            xl = ax.get_xlim()
            yl = ax.get_ylim()
            sx = (xl[1] - xl[0]) / max(self.figure.bbox.width, 1)
            sy = (yl[1] - yl[0]) / max(self.figure.bbox.height, 1)
            ax.set_xlim(xl[0] - dx * sx, xl[1] - dx * sx)
            ax.set_ylim(yl[0] + dy * sy, yl[1] + dy * sy)
        self.draw_idle()


class Map25DTab(QWidget):
    def __init__(self, cfg: StoreConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._geojson: dict | None = None
        self._floor_rgb: np.ndarray | None = None
        self._preview_path: Path | None = None
        self._floor_plan_path: Path | None = None
        self._output_dir: Path | None = None
        self._selected_feature_ids: set[str] = set()
        self._feature_checks: dict[str, QCheckBox] = {}
        self._syncing_checks = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_load = QPushButton("加载 GeoJSON…")
        self.btn_load.clicked.connect(self._load_geojson)
        bar.addWidget(self.btn_load)
        self.btn_open_preview = QPushButton("打开静态试算预览")
        self.btn_open_preview.setEnabled(False)
        self.btn_open_preview.clicked.connect(self._open_preview)
        bar.addWidget(self.btn_open_preview)
        for k, (e, a, label) in VIEW_PRESETS.items():
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, e=e, a=a: self.canvas.set_view(e, a))
            bar.addWidget(b)
        # mode toggle
        bar.addWidget(QLabel("模式:"))
        self.mode_group = QButtonGroup(self)
        self.rb_rotate = QRadioButton("旋转视角")
        self.rb_pan = QRadioButton("平移视图")
        self.rb_rotate.setChecked(True)
        self.mode_group.addButton(self.rb_rotate)
        self.mode_group.addButton(self.rb_pan)
        bar.addWidget(self.rb_rotate)
        bar.addWidget(self.rb_pan)
        self.rb_rotate.toggled.connect(self._sync_mode)
        bar.addStretch(1)
        self.lbl = QLabel("滚轮缩放 | 鼠标拖拽 (按上方按钮切换模式)")
        bar.addWidget(self.lbl)
        layout.addLayout(bar)

        self.summary_label = QLabel(
            "XY 来自人工平面对应；Z 为固定业务高度。SfM 只作相机观测支撑时会明确标注。"
        )
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            "QLabel { background:#fff7d6; border:1px solid #d7bd66; "
            "border-radius:6px; padding:7px; }"
        )
        layout.addWidget(self.summary_label)

        body = QHBoxLayout()
        self.fig = Figure(figsize=(8, 6))
        self.ax = self.fig.add_subplot(111, projection="3d")
        self.canvas = _Map3DCanvas(self.fig)
        body.addWidget(self.canvas, 1)

        # 右侧对象池既可取消自动结果，也可纳入未通过 0.70 单相机门禁的候选。
        review_panel = QWidget()
        review_panel.setMinimumWidth(275)
        review_panel.setMaximumWidth(360)
        review_layout = QVBoxLayout(review_panel)
        review_layout.addWidget(QLabel("对象人工复核（勾选即实时显示）"))
        self.review_status = QLabel("尚未加载候选")
        self.review_status.setWordWrap(True)
        review_layout.addWidget(self.review_status)
        review_buttons = QHBoxLayout()
        self.btn_select_all = QPushButton("全选")
        self.btn_select_none = QPushButton("全不选")
        self.btn_select_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_select_none.clicked.connect(lambda: self._set_all_checked(False))
        review_buttons.addWidget(self.btn_select_all)
        review_buttons.addWidget(self.btn_select_none)
        review_layout.addLayout(review_buttons)

        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_items = QWidget()
        self.review_items_layout = QVBoxLayout(self.review_items)
        self.review_items_layout.addStretch(1)
        self.review_scroll.setWidget(self.review_items)
        review_layout.addWidget(self.review_scroll, 1)
        self.btn_save_review = QPushButton("保存复核结果与静态预览")
        self.btn_save_review.setEnabled(False)
        self.btn_save_review.clicked.connect(self._save_review_preview)
        review_layout.addWidget(self.btn_save_review)
        body.addWidget(review_panel)
        layout.addLayout(body, 1)

        self._draw_empty()

    def _sync_mode(self):
        self.canvas.mode = "rotate" if self.rb_rotate.isChecked() else "pan"
        self.lbl.setText(f"当前模式: {self.canvas.mode}")

    def _draw_empty(self):
        self.ax.clear()
        self.ax.set_xlabel("X (cm)")
        self.ax.set_ylabel("Y (cm)")
        self.ax.set_zlabel("Z (cm)")
        self.ax.set_title("2.5D map (load a geojson to begin)")
        self.canvas.draw_idle()

    def set_geojson(
        self,
        geojson: dict,
        *,
        floor_plan_path: str | Path | None = None,
        preview_path: str | Path | None = None,
        review_geojson: dict | None = None,
        output_dir: str | Path | None = None,
    ):
        """加载 2.5D 数据及平面图上下文，避免把挤出轮廓误解为三维重建。"""

        self._geojson = review_geojson or geojson
        self._output_dir = (
            Path(output_dir).expanduser().resolve() if output_dir else None
        )
        if floor_plan_path:
            self._floor_plan_path = Path(floor_plan_path).expanduser().resolve()
            floor = cv2.imread(str(floor_plan_path))
            self._floor_rgb = (
                cv2.cvtColor(floor, cv2.COLOR_BGR2RGB)
                if floor is not None
                else None
            )
        else:
            self._floor_rgb = None
            self._floor_plan_path = None
        self._preview_path = (
            Path(preview_path).expanduser().resolve()
            if preview_path
            else None
        )
        self.btn_open_preview.setEnabled(
            self._preview_path is not None and self._preview_path.is_file()
        )
        self._selected_feature_ids = default_included_feature_ids(self._geojson)
        self._rebuild_review_items()
        self.btn_save_review.setEnabled(
            bool(self._geojson.get("features", []))
            and self._output_dir is not None
        )
        self._render()

    def _rebuild_review_items(self) -> None:
        """按候选池重建复选框，默认状态由自动门禁结果决定。"""

        while self.review_items_layout.count() > 1:
            item = self.review_items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._feature_checks.clear()
        if not self._geojson:
            return
        for feature in self._geojson.get("features", []):
            feature_id = str(feature.get("id", "未命名"))
            props = feature.get("properties", {})
            score = float(props.get("score", 0.0))
            cameras = ",".join(props.get("observed_by", [])) or str(
                props.get("source_camera", "未知相机")
            )
            suffix = "" if props.get("review_default_included", True) else " [弱候选]"
            checkbox = QCheckBox(
                f"{feature_id}{suffix}\n分数 {score:.3f}｜{cameras}"
            )
            checkbox.setChecked(feature_id in self._selected_feature_ids)
            checkbox.toggled.connect(
                lambda checked, object_id=feature_id: self._toggle_feature(
                    object_id, checked
                )
            )
            self._feature_checks[feature_id] = checkbox
            self.review_items_layout.insertWidget(
                self.review_items_layout.count() - 1,
                checkbox,
            )

    def _toggle_feature(self, feature_id: str, checked: bool) -> None:
        """处理单个对象选择，立即重绘并持久化轻量复核结果。"""

        if self._syncing_checks:
            return
        if checked:
            self._selected_feature_ids.add(feature_id)
        else:
            self._selected_feature_ids.discard(feature_id)
        self._render()
        self._persist_review(render_preview=False)

    def _set_all_checked(self, checked: bool) -> None:
        """批量选择时只重绘和落盘一次，避免连续 UI 抖动。"""

        self._syncing_checks = True
        for feature_id, checkbox in self._feature_checks.items():
            checkbox.setChecked(checked)
            if checked:
                self._selected_feature_ids.add(feature_id)
            else:
                self._selected_feature_ids.discard(feature_id)
        self._syncing_checks = False
        self._render()
        self._persist_review(render_preview=False)

    def _persist_review(self, *, render_preview: bool) -> None:
        """保存派生复核结果；自动输出与候选池始终保持不变。"""

        if self._geojson is None or self._output_dir is None:
            return
        geojson_path, preview_path = write_review_outputs(
            self._geojson,
            self._selected_feature_ids,
            self._output_dir,
            floor_plan_path=self._floor_plan_path,
            render_preview=render_preview,
        )
        if preview_path is not None:
            self._preview_path = preview_path
            self.btn_open_preview.setEnabled(True)
        self.review_status.setText(
            f"已保存 {len(self._selected_feature_ids)} 个对象：{geojson_path.name}"
        )

    def _save_review_preview(self) -> None:
        """显式生成当前选择的静态双视图预览。"""

        self._persist_review(render_preview=True)

    def _load_geojson(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open GeoJSON", str(self.cfg.output_root), "GeoJSON (*.geojson *.json)"
        )
        if not path:
            return
        with open(path, encoding="utf-8") as f:
            self.set_geojson(json.load(f), output_dir=Path(path).parent)

    def _open_preview(self) -> None:
        """用系统查看器打开本轮静态试算图。"""

        if self._preview_path is not None and self._preview_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._preview_path)))

    def _render(self):
        self.ax.clear()
        if not self._geojson:
            self._draw_empty()
            return
        all_feats = self._geojson.get("features", [])
        feats = [
            feature
            for feature in all_feats
            if str(feature.get("id", "")) in self._selected_feature_ids
        ]
        metadata = self._geojson.get("metadata", {})
        trust = metadata.get("trust_level", "未提供")
        position_source = metadata.get(
            "position_source", "人工 cali 平面单应"
        )
        height_source = metadata.get(
            "height_source", f"固定 {self.cfg.table_height_cm:.0f} cm"
        )
        sfm_status = metadata.get("sfm_status", "未接入")
        self.summary_label.setText(
            f"当前为 2.5D 布局，不是稠密三维重建｜可信度：{trust}｜"
            f"XY：{position_source}｜Z：{height_source}｜SfM：{sfm_status}｜"
            f"当前显示：{len(feats)}/{len(all_feats)}"
        )
        weak_count = sum(
            not feature.get("properties", {}).get(
                "review_default_included", True
            )
            for feature in all_feats
        )
        self.review_status.setText(
            f"当前显示 {len(feats)}/{len(all_feats)}；待人工判断弱候选 {weak_count} 个。"
        )
        colors = _palette(max(len(feats), 1))
        all_x: list[float] = []
        all_y: list[float] = []
        plan_width = float(
            metadata.get("plan_width_cm", self.cfg.floor_plan_width_cm)
        )
        plan_height = float(
            metadata.get("plan_height_cm", self.cfg.floor_plan_height_cm)
        )
        if self._floor_rgb is not None:
            # 平面图降采样为轻量纹理，保持交互旋转和缩放流畅。
            stride_y = max(1, self._floor_rgb.shape[0] // 70)
            stride_x = max(1, self._floor_rgb.shape[1] // 110)
            texture = self._floor_rgb[::stride_y, ::stride_x] / 255.0
            grid_x = np.linspace(0.0, plan_width, texture.shape[1])
            grid_y = np.linspace(0.0, plan_height, texture.shape[0])
            xx, yy = np.meshgrid(grid_x, grid_y)
            self.ax.plot_surface(
                xx,
                yy,
                np.zeros_like(xx),
                facecolors=texture,
                shade=False,
                alpha=0.60,
                linewidth=0,
            )
        for i, ft in enumerate(feats):
            geom = ft.get("geometry", {})
            props = ft.get("properties", {})
            if geom.get("type") != "Polygon":
                continue
            ring = geom["coordinates"][0]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            h = float(props.get("height_cm", 70))
            label = str(ft.get("id", props.get("label", "obj")))
            color = colors[i]
            all_x += xs
            all_y += ys
            self.ax.plot(xs + [xs[0]], ys + [ys[0]], [0] * (len(xs) + 1), color=color, lw=1)
            for j in range(len(xs)):
                self.ax.plot([xs[j], xs[j]], [ys[j], ys[j]], [0, h], color=color, lw=0.8)
            self.ax.plot(
                xs + [xs[0]], ys + [ys[0]], [h] * (len(xs) + 1),
                color=color, lw=1.5, label=label,
            )
        if self._floor_rgb is not None:
            self.ax.set_xlim(0.0, plan_width)
            self.ax.set_ylim(plan_height, 0.0)
        elif all_x:
            mx, Mx = min(all_x), max(all_x)
            my, My = min(all_y), max(all_y)
            pad = max((Mx - mx), (My - my)) * 0.2 + 50
            self.ax.set_xlim(mx - pad, Mx + pad)
            self.ax.set_ylim(my - pad, My + pad)
        self.ax.set_zlim(
            0,
            max(self.cfg.table_height_cm, self.cfg.shelf_height_cm) * 1.5,
        )
        self.ax.set_box_aspect(
            (
                max(plan_width, 1.0),
                max(plan_height, 1.0),
                max(self.cfg.table_height_cm * 6.0, 1.0),
            )
        )
        self.ax.set_xlabel("X (cm)")
        self.ax.set_ylabel("Y (cm)")
        self.ax.set_zlabel("Z (cm)")
        self.ax.set_title(
            f"门店桌子 2.5D 试算（显示 {len(feats)}/{len(all_feats)}）"
        )
        if feats:
            self.ax.legend(loc="upper left", fontsize=8)
        # 业务目标是平面布局，默认严格俯视以便直接对照平面图；需要观察
        # 固定高度时，用户仍可切换正视/侧视或手动旋转。
        self.canvas.set_view(*VIEW_PRESETS["top"][:2])
        self.canvas.draw_idle()


def _palette(n: int):
    import matplotlib.pyplot as plt
    cmap = plt.get_cmap("tab20").resampled(max(n, 1))
    return [cmap(i) for i in range(n)]
