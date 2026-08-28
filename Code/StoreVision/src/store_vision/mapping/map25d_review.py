"""2.5D 人工复核：选择对象、生成派生 GeoJSON，并保留审计记录。"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from store_vision.mapping.map25d_preview import render_map25d_preview


def default_included_feature_ids(collection: dict[str, Any]) -> set[str]:
    """返回自动门禁默认纳入的对象 ID；旧 GeoJSON 默认全部纳入。"""

    included: set[str] = set()
    for feature in collection.get("features", []):
        feature_id = str(feature.get("id", ""))
        properties = feature.get("properties", {})
        if feature_id and properties.get("review_default_included", True):
            included.add(feature_id)
    return included


def build_reviewed_collection(
    candidate_collection: dict[str, Any],
    included_ids: Iterable[str],
) -> dict[str, Any]:
    """根据人工选择生成独立结果，不改写自动输出和候选池。"""

    selected = {str(item) for item in included_ids}
    reviewed = copy.deepcopy(candidate_collection)
    reviewed["features"] = [
        feature
        for feature in reviewed.get("features", [])
        if str(feature.get("id", "")) in selected
    ]
    metadata = reviewed.setdefault("metadata", {})
    metadata["review_status"] = "manually_reviewed"
    metadata["reviewed_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    metadata["review_included_ids"] = sorted(selected)
    metadata["review_candidate_count"] = len(
        candidate_collection.get("features", [])
    )
    return reviewed


def write_review_outputs(
    candidate_collection: dict[str, Any],
    included_ids: Iterable[str],
    output_dir: str | Path,
    *,
    floor_plan_path: str | Path | None = None,
    render_preview: bool = False,
) -> tuple[Path, Path | None]:
    """持久化人工选择；静态预览仅在显式保存时重绘以保证勾选流畅。"""

    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = {str(item) for item in included_ids}
    reviewed = build_reviewed_collection(candidate_collection, selected)
    geojson_path = output / "map25d_reviewed.geojson"
    geojson_path.write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    all_ids = {
        str(feature.get("id", ""))
        for feature in candidate_collection.get("features", [])
        if feature.get("id") is not None
    }
    audit_path = output / "review" / "object_selection.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewed_at": reviewed["metadata"]["reviewed_at"],
                "candidate_count": len(all_ids),
                "included_ids": sorted(selected),
                "excluded_ids": sorted(all_ids - selected),
                "source": "map25d_candidates.geojson",
                "derived_output": geojson_path.name,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    preview_path: Path | None = None
    floor = Path(floor_plan_path).expanduser() if floor_plan_path else None
    if render_preview and floor is not None and floor.is_file():
        preview_path = render_map25d_preview(
            reviewed,
            floor,
            output / "map25d_reviewed_preview.png",
        )
    return geojson_path, preview_path


__all__ = [
    "build_reviewed_collection",
    "default_included_feature_ids",
    "write_review_outputs",
]
