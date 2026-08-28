"""Generate a placeholder floor plan when footfallplan.png is missing.

The placeholder is computed from the calibration `mapPoints`, so each camera's
quad is drawn correctly on the canvas. This lets the rest of the pipeline run
even before a real plan PNG is dropped in.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from store_vision.data.loader import find_calibration_path, load_cali_txt

logger = logging.getLogger(__name__)


def ensure_floor_plan(
    folder: Path,
    width: int = 1600,
    height: int = 900,
    overwrite: bool = False,
) -> Path:
    """Return path to footfallplan.png, generating a placeholder if missing."""
    folder = Path(folder)
    out = folder / "footfallplan.png"
    if out.exists() and not overwrite:
        return out

    cali = find_calibration_path(folder)
    cameras: dict = {}
    if cali is not None:
        try:
            cameras = load_cali_txt(cali)
        except Exception as e:
            logger.warning("Cannot parse calibration file for placeholder: %s", e)

    canvas = np.full((height, width, 3), 245, dtype=np.uint8)
    # grid
    for x in range(0, width, 80):
        cv2.line(canvas, (x, 0), (x, height), (220, 220, 220), 1)
    for y in range(0, height, 80):
        cv2.line(canvas, (0, y), (width, y), (220, 220, 220), 1)

    cv2.putText(
        canvas,
        "Placeholder floor plan",
        (40, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (60, 60, 60),
        2,
    )
    cv2.putText(
        canvas,
        "Replace footfallplan.png with the real store layout.",
        (40, 90),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (110, 110, 110),
        1,
    )

    palette = [
        (255, 100, 100), (100, 200, 100), (100, 120, 255),
        (255, 200, 50), (200, 100, 255), (100, 255, 255),
        (255, 150, 150), (150, 255, 150), (150, 150, 255),
        (255, 255, 100),
    ]
    for i, (_, c) in enumerate(cameras.items()):
        col = palette[i % len(palette)]
        pts = np.array(
            [[p.x / 100 * width, p.y / 100 * height] for p in c.map_points],
            dtype=np.int32,
        )
        cv2.polylines(canvas, [pts], True, col, 2)
        cx = int(pts[:, 0].mean())
        cy = int(pts[:, 1].mean())
        cv2.putText(
            canvas, c.name[:14], (cx - 40, cy),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1,
        )
    cv2.imwrite(str(out), canvas)
    logger.info("Wrote placeholder floor plan -> %s", out)
    return out
