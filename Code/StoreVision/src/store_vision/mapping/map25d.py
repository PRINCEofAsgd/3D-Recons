"""2.5D map: GeoJSON FeatureCollection with extrusion height."""

from __future__ import annotations

import json
from pathlib import Path

from store_vision.data.models import MapObject25D


def build_map25d(
    objects: list[MapObject25D],
    out_path: Path | None = None,
    *,
    metadata: dict | None = None,
) -> dict:
    """生成桌面轮廓挤出的 2.5D GeoJSON，并保留路线与可信度说明。"""

    features = [obj.to_geojson_feature() for obj in objects if len(obj.polygon_cm) >= 3]
    fc = {
        "type": "FeatureCollection",
        "features": features,
        "crs": {"type": "name", "properties": {"name": "floor_cm"}},
        "metadata": dict(metadata or {}),
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(fc, f, indent=2)
    return fc


def load_map25d(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
