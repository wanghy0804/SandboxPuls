"""Tests for the Signal model and agent states."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sandboxpulse.models import AgentState, Signal, is_terminal


def test_signal_round_trip() -> None:
    sig = Signal(
        agent_id="claude-abc12345",
        state=AgentState.SUCCESS,
        timestamp=datetime.now(UTC),
        seq=7,
        result="done",
        metadata={"provider": "claude"},
    )
    parsed = Signal.model_validate_json(sig.model_dump_json())
    assert parsed == sig


def test_signal_seq_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        Signal(
            agent_id="a1",
            state=AgentState.SUCCESS,
            timestamp=datetime.now(UTC),
            seq=-1,
        )


def test_only_success_error_timeout_are_terminal() -> None:
    terminal = {s for s in AgentState if is_terminal(s)}
    assert terminal == {AgentState.SUCCESS, AgentState.ERROR, AgentState.TIMEOUT}
