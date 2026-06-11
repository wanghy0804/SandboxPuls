"""Agent states and the Signal data model."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentState(StrEnum):
    IDLE = "idle"
    GENERATING = "generating"
    TOOL_CALLING = "tool_calling"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


_TERMINAL: frozenset[AgentState] = frozenset(
    {AgentState.SUCCESS, AgentState.ERROR, AgentState.TIMEOUT}
)


def is_terminal(state: AgentState) -> bool:
    return state in _TERMINAL


def _now() -> datetime:
    return datetime.now(UTC)


class Signal(BaseModel):
    """One hook event from an agent session, as written to a signal file."""

    agent_id: str
    state: AgentState
    timestamp: datetime = Field(default_factory=_now)
    seq: int = Field(ge=0)
    result: Any | None = None
    error_detail: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
