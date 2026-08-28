"""Generate synthetic floor-plan and camera-image test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from store_vision.data.loader import load_cali_txt


def generate(target: Path, floor_w: int = 1600, floor_h: int = 900) -> None:
    target.mkdir(parents=True, exist_ok=True)
    cali = target / "cali.txt"
    if not cali.exists():
        raise FileNotFoundError(cali)

    cameras = load_cali_txt(cali)

    # ---- floor plan
    floor = np.full((floor_h, floor_w, 3), 240, dtype=np.uint8)
    for x in range(0, floor_w, 80):
        cv2.line(floor, (x, 0), (x, floor_h), (220, 220, 220), 1)
    for y in range(0, floor_h, 80):
        cv2.line(floor, (0, y), (floor_w, y), (220, 220, 220), 1)
    cv2.putText(
        floor, "footfallplan (synthetic)", (40, 50),
        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (80, 80, 80), 2,
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
            [[p.x / 100 * floor_w, p.y / 100 * floor_h] for p in c.map_points],
            dtype=np.int32,
        )
        cv2.polylines(floor, [pts], True, col, 2)
        cx = int(pts[:, 0].mean())
        cy = int(pts[:, 1].mean())
        cv2.putText(floor, c.name[:12], (cx - 40, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    cv2.imwrite(str(target / "footfallplan.png"), floor)

    # ---- per camera synthetic images
    cam_w, cam_h = 2560, 1440
    for i, (serial, c) in enumerate(cameras.items()):
        img = np.full((cam_h, cam_w, 3), 60, dtype=np.uint8)
        # gradient floor
        for y in range(cam_h // 2, cam_h):
            shade = 60 + (y - cam_h // 2) // 8
            img[y, :, :] = (shade, shade + 4, shade + 6)

        # synthetic white "table" rectangle in lower 60%
        tx0, ty0 = int(cam_w * 0.30), int(cam_h * 0.55)
        tx1, ty1 = int(cam_w * 0.70), int(cam_h * 0.85)
        cv2.rectangle(img, (tx0, ty0), (tx1, ty1), (245, 245, 245), -1)
        cv2.rectangle(img, (tx0, ty0), (tx1, ty1), (200, 200, 200), 2)

        # algorithm-overlay virtual line (cyan) — should be filtered
        cv2.line(img, (100, 200), (cam_w - 100, 200), (255, 255, 0), 3)
        cv2.line(img, (200, 100), (200, cam_h - 100), (0, 255, 255), 3)

        # calibration quad (yellow)
        cam_pts = c.camera_points_array().astype(np.int32)
        cv2.polylines(img, [cam_pts], True, (0, 255, 255), 3)
        for p in c.camera_points:
            cv2.circle(img, (int(p.x), int(p.y)), 12, (0, 0, 255), -1)
        cv2.putText(
            img, f"{c.name} / {serial}", (60, 80),
            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2,
        )
        cv2.imwrite(str(target / f"{serial}.jpg"), img)

    print(f"Generated fixtures -> {target}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "tests" / "fixtures" / "sample_store")
    generate(target)
