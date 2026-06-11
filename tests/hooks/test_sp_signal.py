"""Tests for the sp_signal.py hook bridge script."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "sp_signal.py"

_spec = importlib.util.spec_from_file_location("sp_signal", _SCRIPT)
assert _spec is not None and _spec.loader is not None
sp_signal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp_signal)


def _claude_transcript(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def _assistant_entry(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _run_hook(payload: dict[str, Any], signal_dir: Path, provider: str = "claude") -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"SP_PROVIDER": provider, "SP_SIGNAL_DIR": str(signal_dir), "PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert proc.returncode == 0
    files = list(signal_dir.glob("*.signal.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_stop_extracts_last_claude_reply(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            _assistant_entry({"type": "text", "text": "All tests pass now."}),
            _assistant_entry({"type": "tool_use", "name": "Bash", "input": {}}),
        ],
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
    )
    assert signal["state"] == "success"
    assert signal["result"] == "All tests pass now."


def test_stop_joins_multiple_text_blocks(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path,
        [_assistant_entry({"type": "text", "text": "part one"}, {"type": "text", "text": "part two"})],
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
    )
    assert signal["result"] == "part one\npart two"


def test_stop_extracts_codex_agent_message(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Refactor complete."}],
                },
            },
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Refactor complete."}},
            {"type": "event_msg", "payload": {"type": "token_count"}},
        ],
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
        provider="codex",
    )
    assert signal["result"] == "Refactor complete."


def test_stop_prefers_payload_last_assistant_message(signal_dir: Path) -> None:
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "quick answer"},
        signal_dir,
        provider="codex",
    )
    assert signal["result"] == "quick answer"


def test_non_terminal_event_has_no_result(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path, [_assistant_entry({"type": "text", "text": "should not appear"})]
    )
    signal = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "transcript_path": str(transcript),
        },
        signal_dir,
    )
    assert signal["state"] == "tool_calling"
    assert signal["result"] is None


def test_notification_permission_prompt_is_waiting(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Bash",
        },
        signal_dir,
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "Claude needs your permission to use Bash"


def test_notification_idle_without_type_is_waiting(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "message": "Claude is waiting for your input",
        },
        signal_dir,
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "Claude is waiting for your input"


def test_notification_auth_is_not_waiting(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "notification_type": "auth_success",
            "message": "Authentication successful",
        },
        signal_dir,
    )
    assert signal["state"] == "generating"
    assert signal["result"] is None


def test_permission_request_carries_command(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf build"},
        },
        signal_dir,
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "Bash: rm -rf build"


def test_ask_user_question_is_waiting_with_options(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "数据库怎么分?",
                        "header": "DB",
                        "options": [
                            {"label": "同实例新建 trading 库"},
                            {"label": "独立 PG 实例"},
                        ],
                    }
                ]
            },
        },
        signal_dir,
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "数据库怎么分?\n1. 同实例新建 trading 库\n2. 独立 PG 实例"


def test_exit_plan_mode_is_waiting_with_plan(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "1. refactor\n2. test"},
        },
        signal_dir,
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "计划待确认:\n1. refactor\n2. test"


def test_codex_request_user_input_is_waiting(signal_dir: Path) -> None:
    signal = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "request_user_input",
            "tool_input": {"prompt": "数据库怎么分?"},
        },
        signal_dir,
        provider="codex",
    )
    assert signal["state"] == "waiting_approval"
    assert signal["result"] == "数据库怎么分?"


def test_missing_transcript_is_harmless(signal_dir: Path) -> None:
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/nope/missing.jsonl"},
        signal_dir,
    )
    assert signal["state"] == "success"
    assert signal["result"] is None


def test_reply_is_truncated(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path, [_assistant_entry({"type": "text", "text": "y" * 9000})]
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
    )
    assert signal["result"] is not None
    assert len(signal["result"]) <= 1502
    assert signal["result"].endswith("…")


def test_codex_rollout_found_by_session_id(tmp_path: Path, signal_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = "019eb193-1d78-73b3-a327-590a0247b503"
    rollout_dir = tmp_path / "home" / ".codex" / "sessions" / "2026" / "06" / "10"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / f"rollout-2026-06-10T20-47-49-{sid}.jsonl"
    rollout.write_text(
        json.dumps(
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "from rollout"}}
        )
    )
    monkeypatch.setattr(sp_signal.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    reply, _model, _usage = sp_signal._extract({"session_id": sid}, "codex")
    assert reply == "from rollout"


def test_stop_extracts_claude_usage_and_model(tmp_path: Path, signal_dir: Path) -> None:
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 959,
        "cache_read_input_tokens": 112657,
        "output_tokens": 4409,
    }
    transcript = _claude_transcript(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "model": "claude-fable-5",
                    "usage": usage,
                    "content": [{"type": "text", "text": "done"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "model": "claude-fable-5",
                    "usage": usage,
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                },
            },
        ],
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
    )
    assert signal["result"] == "done"
    assert signal["metadata"]["model"] == "claude-fable-5"
    assert signal["metadata"]["usage"] == {"context_tokens": 2 + 959 + 112657 + 4409}


def test_stop_extracts_codex_usage_and_model(tmp_path: Path, signal_dir: Path) -> None:
    transcript = _claude_transcript(
        tmp_path,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "xhigh"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 26572},
                        "last_token_usage": {"total_tokens": 13295},
                        "model_context_window": 258400,
                    },
                    "rate_limits": {
                        "primary": {
                            "used_percent": 8.0,
                            "window_minutes": 300,
                            "resets_at": 1781113676,
                        },
                        "secondary": {
                            "used_percent": 4.0,
                            "window_minutes": 10080,
                            "resets_at": 1781611469,
                        },
                    },
                },
            },
        ],
    )
    signal = _run_hook(
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
        signal_dir,
        provider="codex",
    )
    assert signal["result"] == "ok"
    assert signal["metadata"]["model"] == "gpt-5.5"
    assert signal["metadata"]["usage"] == {
        "context_tokens": 13295,
        "context_window": 258400,
        "primary_used_percent": 8.0,
        "primary_window_minutes": 300,
        "primary_resets_at": 1781113676,
        "secondary_used_percent": 4.0,
        "secondary_window_minutes": 10080,
        "secondary_resets_at": 1781611469,
    }


def test_malformed_stdin_exits_zero(signal_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input="not json{{",
        capture_output=True,
        text=True,
        env={"SP_PROVIDER": "claude", "SP_SIGNAL_DIR": str(signal_dir), "PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert proc.returncode == 0
