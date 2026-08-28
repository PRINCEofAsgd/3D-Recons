"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.generate_fixtures import generate

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sample_store"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    if not (FIXTURE_DIR / "footfallplan.png").exists():
        generate(FIXTURE_DIR)
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app
