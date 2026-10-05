"""Optional pycolmap sparse reconstruction for unknown intrinsics."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from store_vision.data.models import StoreDataset

logger = logging.getLogger(__name__)


def run_colmap_if_available(dataset: StoreDataset, out_dir: Path) -> dict | None:
    try:
        import pycolmap  # type: ignore
    except ImportError:
        logger.info("pycolmap not installed; skipping COLMAP refinement.")
        return None

    out_dir = out_dir / "colmap"
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = out_dir / "images"
    image_dir.mkdir(exist_ok=True)
    db_path = out_dir / "database.db"
    if db_path.exists():
        db_path.unlink()

    paths = []
    for c in dataset.camera_list():
        if not c.image_path:
            continue
        dst = image_dir / f"{c.device_serial}.jpg"
        if not dst.exists():
            shutil.copy(c.image_path, dst)
        paths.append(dst)
    if len(paths) < 2:
        logger.warning("Need at least 2 images for COLMAP.")
        return None

    try:
        opts = pycolmap.ImageReaderOptions()
        opts.camera_model = "OPENCV"
        pycolmap.extract_features(
            database_path=str(db_path),
            image_path=str(image_dir),
            reader_options=opts,
        )
        pycolmap.match_exhaustive(database_path=str(db_path))
        sparse = out_dir / "sparse"
        sparse.mkdir(exist_ok=True)
        maps = pycolmap.incremental_mapping(
            database_path=str(db_path),
            image_path=str(image_dir),
            output_path=str(sparse),
        )
    except Exception as e:
        logger.warning("COLMAP failed: %s", e)
        return None

    summary: dict = {"num_reconstructions": len(maps) if maps else 0, "cameras": {}}
    if maps:
        rec = maps[0]
        for im in rec.images.values():
            cam = rec.cameras[im.camera_id]
            summary["cameras"][im.name] = {
                "model": cam.model.name,
                "width": cam.width,
                "height": cam.height,
                "params": list(cam.params),
            }
        with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    return summary
