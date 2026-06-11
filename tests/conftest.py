"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def signal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "signals"
    d.mkdir()
    return d
