"""Tests for Settings."""
from __future__ import annotations

from pathlib import Path

import pytest

from sandboxpulse.config import Settings

_ENV_VARS = [
    "SANDBOXPULSE_HERMES_TARGET",
    "SANDBOXPULSE_HERMES_MIN_INTERVAL_S",
    "SANDBOXPULSE_HERMES_DEBOUNCE_S",
    "SANDBOXPULSE_HERMES_PULL_LOG",
    "SANDBOXPULSE_LOG_LEVEL",
]


def test_defaults_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.hermes_target is None
    assert s.hermes_min_interval_s == 10.0
    assert s.hermes_debounce_s == 0.0
    assert s.hermes_pull_log == Path("~/.hermes/logs/gateway.log")
    assert s.log_level == "INFO"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOXPULSE_HERMES_TARGET", "weixin:user@im.wechat")
    monkeypatch.setenv("SANDBOXPULSE_HERMES_DEBOUNCE_S", "12.5")
    s = Settings(_env_file=None)
    assert s.hermes_target == "weixin:user@im.wechat"
    assert s.hermes_debounce_s == 12.5
