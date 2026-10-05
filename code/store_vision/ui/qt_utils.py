"""Qt helper functions."""

from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtGui import QImage, QPixmap


def cv_to_qpixmap(bgr: np.ndarray, max_side: int | None = None) -> QPixmap:
    if bgr is None or bgr.size == 0:
        return QPixmap()
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if max_side is not None and max(rgb.shape[:2]) > max_side:
        scale = max_side / max(rgb.shape[:2])
        rgb = cv2.resize(rgb, (int(rgb.shape[1] * scale), int(rgb.shape[0] * scale)))
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())
