"""生成一次性合成 Phase 2 输入与显式测试报告，不读取真实 data/。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.generate_fixtures import generate
from tests.unit.test_cloud_phase1 import _gui_synthetic
from tests.unit.test_workspace import _shared_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "sample_store"
    if not (fixture / "footfallplan.png").exists():
        generate(fixture)
    source = _gui_synthetic(fixture, root / "source")
    (root / "synthetic-report.json").write_text(
        json.dumps(_shared_report(source), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"source": str(source), "synthetic_report": str(root / "synthetic-report.json")}))


if __name__ == "__main__":
    main()
