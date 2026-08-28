"""命令行入口冷启动回归测试。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cli_imports_in_fresh_interpreter(tmp_path: Path) -> None:
    """新解释器必须能导入真实入口，防止测试导入顺序掩盖循环依赖。"""

    environment = os.environ.copy()
    environment["MPLCONFIGDIR"] = str(tmp_path / "matplotlib")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from store_vision.cli import main; print(main.__name__)",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "main"
