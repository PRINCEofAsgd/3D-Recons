"""The single command-line and desktop entry point for Store Vision."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from store_vision.calibration.workflow import run_calibration_workflow
from store_vision.config import StoreConfig
from store_vision.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store multi-camera vision system")
    parser.add_argument(
        "--dataset",
        "--folder",
        dest="dataset",
        help="门店数据目录（--folder 是兼容别名）",
    )
    parser.add_argument("--output", help="结果输出目录")
    parser.add_argument("--headless", action="store_true", help="仅运行现有完整流水线")
    parser.add_argument("--colmap", action="store_true", help="启用现有可选 pycolmap 步骤")

    subparsers = parser.add_subparsers(dest="command")
    calibration = subparsers.add_parser(
        "calibration-demo",
        help="运行或规划 COLMAP 标定优化 Demo",
    )
    calibration.add_argument("--dataset", required=True, help="门店数据集目录")
    calibration.add_argument("--output", required=True, help="Demo 输出目录")
    calibration.add_argument("--dry-run", action="store_true", help="只输出 COLMAP 命令计划")
    calibration.add_argument("--skip-colmap", action="store_true", help="仅加载人工标定并输出诊断")
    calibration.add_argument(
        "--fisheye-calibration",
        help="鱼眼联合标定运行目录或主报告；未指定时尝试发现同门店最新结果",
    )
    geometry = subparsers.add_parser(
        "geometry-diagnostics",
        help="只读分析已有 COLMAP 数据库、图像和文本模型",
    )
    geometry.add_argument("--database-path", required=True, help="已有 COLMAP database.db")
    geometry.add_argument("--image-path", required=True, help="数据库图像名称对应的根目录")
    geometry.add_argument("--model-path", required=True, help="已有 COLMAP 文本模型目录")
    geometry.add_argument("--output-path", required=True, help="诊断报告输出目录（通常为 reports）")
    geometry.add_argument("--pair", nargs=2, action="append", default=[], metavar=("IMAGE_A", "IMAGE_B"), help="增加一个显式诊断图像对，可重复")
    geometry.add_argument("--all-pairs", action="store_true", help="为全部数据库图像对生成详细可视化")
    geometry.add_argument("--max-visualized-matches", type=int, default=200, help="每张图最多显示的匹配数")
    geometry.add_argument("--grid-cols", type=int, default=8, help="空间覆盖网格列数")
    geometry.add_argument("--grid-rows", type=int, default=5, help="空间覆盖网格行数")
    geometry.add_argument("--seed", type=int, default=0, help="RANSAC 与可视化抽样固定随机种子")
    geometry.add_argument("--intrinsics-config", help="可选的真实相机内参 JSON")
    geometry.add_argument("--dry-run", action="store_true", help="只返回计划，不写任何文件")
    geometry.add_argument("--overwrite", action="store_true", help="允许重新生成已有诊断报告")
    sfm_map = subparsers.add_parser(
        "sfm-assisted-map25d",
        help="使用已验收 SfM 相机集合试算桌子 2.5D 地图",
    )
    sfm_map.add_argument("--dataset-path", required=True, help="当前门店数据集目录")
    sfm_map.add_argument(
        "--experiment-path",
        required=True,
        help="包含 reports/sfm_intrinsics.json 的 SfM 运行目录",
    )
    sfm_map.add_argument("--output-path", required=True, help="试算 GeoJSON、预览和报告输出目录")
    external_import = subparsers.add_parser(
        "import-external-calibration",
        help="把图片与精确 K/D/R/t 导入为可独立消费的中间层",
    )
    external_import.add_argument(
        "--source-path", required=True, help="包含图片和 intrinsic/extrinsic 的目录"
    )
    external_import.add_argument(
        "--output-path", required=True, help="新的中间层 run_* 目录"
    )
    for command, help_text in (
        (
            "fisheye-calibration",
            "同型号鱼眼镜头共享 K/D，联合拟合位姿并执行 BA",
        ),
        (
            "shared-intrinsics-calibration",
            "鱼眼联合标定的兼容命令别名",
        ),
    ):
        shared = subparsers.add_parser(command, help=help_text)
        shared.add_argument("--dataset-path", required=True, help="包含图片、cali.txt/cali.json 和 scale.txt/scale.json 的数据目录")
        shared.add_argument("--output-path", required=True, help="标定报告与可视化输出目录")
        shared.add_argument("--project-root", help="可选项目根目录，用于发现辅助诊断输入")
        shared.add_argument("--seed", type=int, default=0, help="固定随机种子")
        shared.add_argument("--dry-run", action="store_true", help="只验证输入并列出计划产物")
    return parser


def _default_dataset() -> Path | None:
    """只自动发现中性示例数据，正式数据应通过 ``--dataset`` 显式传入。"""

    # 单层 code/ 项目中，CLI 模块上两级即仓库根目录。
    repository = Path(__file__).resolve().parents[2]
    candidates = (
        Path.cwd() / "sample_store",
        Path.cwd() / "data" / "input" / "sample_store",
        repository / "data" / "input" / "sample_store",
    )
    return next((path for path in candidates if path.is_dir()), None)


def _dataset_or_exit(value: str | None) -> Path:
    path = Path(value).expanduser() if value else _default_dataset()
    if path is None or not path.exists():
        print("Dataset not found; pass --dataset PATH.", file=sys.stderr)
        raise SystemExit(2)
    return path.resolve()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)

    if args.command == "calibration-demo":
        run_calibration_workflow(
            args.dataset,
            args.output,
            dry_run=args.dry_run,
            skip_colmap=args.skip_colmap,
            fisheye_calibration=args.fisheye_calibration,
        )
        return

    if args.command == "geometry-diagnostics":
        # 延迟导入，避免普通 GUI/headless 启动加载诊断专用模块。
        import json

        from store_vision.calibration.geometry_diagnostics import (
            GeometryDiagnosticsRequest,
            run_geometry_diagnostics,
        )
        from store_vision.calibration.pair_geometry import GeometryDiagnosticsConfig

        result = run_geometry_diagnostics(
            GeometryDiagnosticsRequest(
                database_path=Path(args.database_path).expanduser().resolve(),
                image_path=Path(args.image_path).expanduser().resolve(),
                model_path=Path(args.model_path).expanduser().resolve(),
                output_path=Path(args.output_path).expanduser().resolve(),
                config=GeometryDiagnosticsConfig(
                    grid_cols=args.grid_cols,
                    grid_rows=args.grid_rows,
                    seed=args.seed,
                    max_visualized_matches=args.max_visualized_matches,
                ),
                extra_pairs=tuple(tuple(pair) for pair in args.pair),
                all_pairs=args.all_pairs,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
                intrinsics_config=Path(args.intrinsics_config).expanduser().resolve()
                if args.intrinsics_config else None,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "sfm-assisted-map25d":
        # 延迟导入，普通完整处理不需要加载试算预览绘图模块。
        import json

        from store_vision.mapping.sfm_assisted import (
            run_sfm_assisted_map25d,
        )

        result = run_sfm_assisted_map25d(
            args.dataset_path,
            args.experiment_path,
            args.output_path,
        )
        print(json.dumps(result.summary, ensure_ascii=False, indent=2))
        return

    if args.command == "import-external-calibration":
        # 导入器只做格式归一化和可信等级声明，不重新拟合精确参数。
        import json

        from store_vision.data.workspace import (
            import_external_calibration_package,
        )

        result = import_external_calibration_package(
            args.source_path,
            args.output_path,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command in {"fisheye-calibration", "shared-intrinsics-calibration"}:
        # 延迟导入，避免普通 GUI/headless 路径加载鱼眼联合标定模块。
        import json

        from store_vision.calibration.shared_calibration import (
            SharedCalibrationConfig,
            run_shared_intrinsics_calibration,
        )

        result = run_shared_intrinsics_calibration(
            args.dataset_path,
            args.output_path,
            project_root=args.project_root,
            config=SharedCalibrationConfig(seed=args.seed),
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    cfg = StoreConfig()
    cfg.use_colmap = args.colmap
    if args.headless:
        dataset = _dataset_or_exit(args.dataset)
        result = run_pipeline(dataset, cfg, output_dir=args.output)
        print(f"Pipeline done -> {result.output_dir}")
        return

    # Import Qt only in desktop mode so headless and calibration diagnostics
    # remain usable on servers without a graphical session.
    import matplotlib

    matplotlib.use("QtAgg")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from store_vision.ui import MainWindow
    from store_vision.ui.app_identity import apply_application_identity
    from store_vision.ui.app_icon import apply_application_icon

    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    # 在创建主窗口前设置名称，确保 macOS 菜单和桌面运行实例统一显示品牌名。
    apply_application_identity(app)
    # 必须在主窗口创建前设置，确保 macOS Dock 和应用切换器尽早使用品牌图标。
    apply_application_icon(app)
    window = MainWindow(cfg)
    window.show()
    window.raise_()
    window.activateWindow()
    if args.dataset:
        dataset = Path(args.dataset).expanduser()
        if dataset.exists():
            QTimer.singleShot(120, lambda: window.load_folder(str(dataset.resolve())))
        else:
            window.statusBar().showMessage(f"Dataset not found: {args.dataset}")
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
