"""Tests for claude quota-usage enrichment."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandboxpulse import usage
from sandboxpulse.models import AgentState, Signal
from sandboxpulse.usage import claude_limit_percents, enrich_signal_usage

_PAYLOAD = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-06-10T19:00:00+00:00"},
    "seven_day": {"utilization": 16.0, "resets_at": "2026-06-16T08:00:00+00:00"},
    "seven_day_opus": None,
}
_PERCENTS = {
    "primary_used_percent": 4.0,
    "primary_window_minutes": 300,
    "primary_resets_at": "2026-06-10T19:00:00+00:00",
    "secondary_used_percent": 16.0,
    "secondary_window_minutes": 10080,
    "secondary_resets_at": "2026-06-16T08:00:00+00:00",
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    usage._cache.clear()


def _credentials(tmp_path: Path) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}))
    return path


def test_percents_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "_fetch_payload", lambda token: _PAYLOAD)
    assert claude_limit_percents(_credentials(tmp_path)) == _PERCENTS


def test_percents_cached_between_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch(token: str) -> dict:
        calls.append(token)
        return _PAYLOAD

    monkeypatch.setattr(usage, "_fetch_payload", fetch)
    path = _credentials(tmp_path)
    claude_limit_percents(path)
    claude_limit_percents(path)
    assert calls == ["tok-123"]


def test_percents_missing_credentials(tmp_path: Path) -> None:
    assert claude_limit_percents(tmp_path / "nope.json") == {}


def test_percents_endpoint_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(token: str) -> dict:
        raise OSError("offline")

    monkeypatch.setattr(usage, "_fetch_payload", boom)
    assert claude_limit_percents(_credentials(tmp_path)) == {}


def test_percents_partial_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        usage,
        "_fetch_payload",
        lambda token: {"five_hour": {"utilization": 50}, "seven_day": None},
    )
    assert claude_limit_percents(_credentials(tmp_path)) == {
        "primary_used_percent": 50.0,
        "primary_window_minutes": 300,
    }


def _signal(state: AgentState, provider: str, metadata: dict[str, Any] | None = None) -> Signal:
    base: dict[str, Any] = {"provider": provider}
    base.update(metadata or {})
    return Signal(agent_id=f"{provider}-abc12345", state=state, seq=0, metadata=base)


def test_enrich_adds_percents_to_claude_terminal_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "_fetch_payload", lambda token: _PAYLOAD)
    sig = _signal(AgentState.SUCCESS, "claude", {"usage": {"context_tokens": 5}})
    enrich_signal_usage(sig, credentials_path=_credentials(tmp_path))
    assert sig.metadata["usage"] == {"context_tokens": 5, **_PERCENTS}


def test_enrich_skips_codex_and_non_terminal(tmp_path: Path) -> None:
    codex = _signal(AgentState.SUCCESS, "codex")
    enrich_signal_usage(codex, credentials_path=tmp_path / "nope.json")
    assert "usage" not in codex.metadata

    running = _signal(AgentState.GENERATING, "claude")
    enrich_signal_usage(running, credentials_path=tmp_path / "nope.json")
    assert "usage" not in running.metadata


def test_enrich_without_data_leaves_signal_untouched(tmp_path: Path) -> None:
    sig = _signal(AgentState.SUCCESS, "claude")
    enrich_signal_usage(sig, credentials_path=tmp_path / "nope.json")
    assert "usage" not in sig.metadata
