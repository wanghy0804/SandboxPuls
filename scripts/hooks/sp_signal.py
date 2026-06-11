#!/usr/bin/env python3
"""Bridge Claude Code / Codex hook events into SandboxPulse Signal files.

Reads a hook payload as JSON on stdin, writes a Signal-shaped JSON file
into $SP_SIGNAL_DIR (default ./signals). On terminal events (Stop,
SessionEnd, SubagentStop) the agent's last reply is extracted — from the
payload itself when present, else from the session transcript — into
Signal.result so downstream emitters can show what the agent said.
Pause events — PermissionRequest, pause-type Notifications, and question
tools (AskUserQuestion / ExitPlanMode / request_user_input) — become
waiting_approval signals carrying what the agent is waiting on.

Set $SP_DEBUG_DIR to also dump each raw hook payload there for inspection.
Never blocks the agent: any failure exits 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

_STATE_MAP = {
    "SessionStart": "idle",
    "UserPromptSubmit": "generating",
    "PreToolUse": "tool_calling",
    "PermissionRequest": "waiting_approval",
    "PostToolUse": "executing",
    "Stop": "success",
    "SessionEnd": "success",
    "SubagentStop": "success",
    "Notification": "waiting_approval",
    "PreCompact": "generating",
}

# Tool calls whose whole purpose is to stop and ask the user something —
# the session is paused on a human from the moment they start.
# AskUserQuestion/ExitPlanMode are Claude Code, request_user_input is Codex.
_QUESTION_TOOLS = {"AskUserQuestion", "ExitPlanMode", "request_user_input"}

# Claude Code notification types that mean "paused on the user"; the rest
# (auth_success, elicitation_complete, ...) are plain progress.
_PAUSE_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog"}

_TERMINAL_EVENTS = {"Stop", "SessionEnd", "SubagentStop"}
_REPLY_MAX_CHARS = 1500
# Hooks run under tight timeouts; only the transcript tail can hold the
# last reply, so never read more than this many bytes.
_TAIL_BYTES = 2_000_000


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _REPLY_MAX_CHARS:
        return text
    return text[:_REPLY_MAX_CHARS].rstrip() + " …"


def _resolve_state(event: dict, event_name: str) -> str:
    """Event -> state, recognising "paused on the user" beyond the plain map."""
    if event_name == "PreToolUse" and event.get("tool_name") in _QUESTION_TOOLS:
        return "waiting_approval"
    if event_name == "Notification":
        ntype = event.get("notification_type") or event.get("type")
        if isinstance(ntype, str) and ntype:
            return "waiting_approval" if ntype in _PAUSE_NOTIFICATIONS else "generating"
        # payload shape varies across versions: with no type field, treat
        # everything except auth chatter as a pause — a spurious pause ping
        # beats a silent wait
        if "auth" in str(event.get("message") or "").lower():
            return "generating"
        return "waiting_approval"
    return _STATE_MAP.get(event_name, "generating")


def _ask_user_question_text(tool_input: dict) -> str | None:
    """Render AskUserQuestion's questions + options like the terminal UI."""
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return None
    parts: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        text = str(question.get("question") or "").strip()
        if not text:
            continue
        lines = [text]
        options = question.get("options")
        if isinstance(options, list):
            for index, option in enumerate(options, 1):
                label = option.get("label") if isinstance(option, dict) else option
                if isinstance(label, str) and label.strip():
                    lines.append(f"{index}. {label.strip()}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) or None


def _waiting_detail(event: dict, event_name: str) -> str | None:
    """What the agent is paused on, for the notification body."""
    if event_name == "Notification":
        for key in ("message", "title"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None
    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return tool_name or None
    if tool_name == "AskUserQuestion":
        text = _ask_user_question_text(tool_input)
        if text:
            return text
    if tool_name == "ExitPlanMode":
        plan = tool_input.get("plan")
        if isinstance(plan, str) and plan.strip():
            return f"计划待确认:\n{plan}"
    for key in ("question", "prompt", "message"):  # request_user_input et al.
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return f"{tool_name}: {command}" if tool_name else command
    compact = json.dumps(tool_input, ensure_ascii=False)
    if len(compact) > 300:
        compact = compact[:300] + "…"
    return f"{tool_name} {compact}".strip() or None


def _tail_lines(path: Path) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > _TAIL_BYTES:
            fh.seek(size - _TAIL_BYTES)
            fh.readline()  # drop the partial first line
        data = fh.read()
    return data.decode("utf-8", errors="replace").splitlines()


def _claude_entry_text(entry: dict) -> str | None:
    """Text of a Claude Code transcript entry, if it is an assistant message."""
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t).strip()
        return joined or None
    return None


def _codex_entry_text(entry: dict) -> str | None:
    """Text of a Codex rollout entry, if it is an assistant message."""
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    if entry.get("type") == "event_msg" and payload.get("type") == "agent_message":
        message = payload.get("message")
        if isinstance(message, str):
            return message.strip() or None
        return None
    if (
        entry.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    ):
        content = payload.get("content")
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "output_text"
            ]
            joined = "\n".join(t for t in texts if t).strip()
            return joined or None
    return None


_CLAUDE_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _claude_entry_usage(entry: dict) -> tuple[int, str | None] | None:
    """(context_tokens, model) from a Claude assistant entry, if it has usage."""
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens = sum(
        value for key in _CLAUDE_USAGE_KEYS if isinstance(value := usage.get(key), int)
    )
    if tokens <= 0:
        return None
    model = message.get("model")
    return tokens, model if isinstance(model, str) else None


def _codex_entry_usage(entry: dict) -> dict | None:
    """Context + rate-limit numbers from a Codex token_count event."""
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    usage: dict = {}
    info = payload.get("info")
    if isinstance(info, dict):
        last = info.get("last_token_usage") or info.get("total_token_usage")
        if isinstance(last, dict) and isinstance(last.get("total_tokens"), int):
            usage["context_tokens"] = last["total_tokens"]
        if isinstance(info.get("model_context_window"), int):
            usage["context_window"] = info["model_context_window"]
    limits = payload.get("rate_limits")
    if isinstance(limits, dict):
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not isinstance(window, dict):
                continue
            if isinstance(window.get("used_percent"), (int, float)):
                usage[f"{name}_used_percent"] = window["used_percent"]
            if isinstance(window.get("window_minutes"), int):
                usage[f"{name}_window_minutes"] = window["window_minutes"]
            if isinstance(window.get("resets_at"), (int, float)):
                usage[f"{name}_resets_at"] = window["resets_at"]
    return usage or None


def _codex_entry_model(entry: dict) -> str | None:
    if entry.get("type") != "turn_context":
        return None
    payload = entry.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) else None


def _scan_transcript(path: Path) -> tuple[str | None, str | None, dict]:
    """One reversed pass over the transcript tail.

    Collects the newest assistant reply with text, the model name, and
    usage numbers (Claude entries carry usage on every assistant message —
    including tool_use-only ones; Codex carries a token_count event).
    """
    try:
        lines = _tail_lines(path)
    except OSError:
        return None, None, {}
    reply: str | None = None
    model: str | None = None
    usage: dict = {}
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if reply is None:
            reply = _claude_entry_text(entry) or _codex_entry_text(entry)
        if not usage:
            claude_usage = _claude_entry_usage(entry)
            if claude_usage is not None:
                usage["context_tokens"] = claude_usage[0]
                model = model or claude_usage[1]
            else:
                codex_usage = _codex_entry_usage(entry)
                if codex_usage is not None:
                    usage.update(codex_usage)
        if model is None:
            model = _codex_entry_model(entry)
        if reply is not None and model is not None and usage:
            break
    return reply, model, usage


def _find_codex_rollout(session_id: str) -> Path | None:
    """Locate a Codex rollout file by session id (filename embeds the uuid)."""
    root = Path.home() / ".codex" / "sessions"
    if not session_id or not root.is_dir():
        return None
    matches = sorted(root.glob(f"*/*/*/rollout-*{session_id}*.jsonl"))
    return matches[-1] if matches else None


def _extract(event: dict, provider: str) -> tuple[str | None, str | None, dict]:
    """(reply, model, usage) for a terminal hook event."""
    reply: str | None = None
    for key in ("last_assistant_message", "last-assistant-message"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            reply = value
            break
    raw_path = event.get("transcript_path") or event.get("rollout_path")
    path = Path(str(raw_path)).expanduser() if raw_path else None
    if (path is None or not path.is_file()) and provider == "codex":
        path = _find_codex_rollout(str(event.get("session_id") or ""))
    model: str | None = None
    usage: dict = {}
    if path is not None and path.is_file():
        scanned_reply, model, usage = _scan_transcript(path)
        reply = reply or scanned_reply
    return (_truncate(reply) if reply else None), model, usage


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(event, dict):
        event = {}

    provider = os.environ.get("SP_PROVIDER", "claude")
    signal_dir = Path(os.environ.get("SP_SIGNAL_DIR", "./signals")).expanduser()
    try:
        signal_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return 0

    event_name = event.get("hook_event_name") or event.get("event") or "unknown"
    raw_sid = str(event.get("session_id") or "unknown")
    session_id = hashlib.sha1(raw_sid.encode()).hexdigest()[:8]
    agent_id = f"{provider}-{session_id}"
    state = _resolve_state(event, event_name)

    now = datetime.now(UTC)
    seq = int(now.timestamp() * 1_000_000) % (2**31)

    debug_dir = os.environ.get("SP_DEBUG_DIR")
    if debug_dir:
        try:
            dump_dir = Path(debug_dir).expanduser()
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"{provider}-{event_name}-{seq}.json").write_text(raw)
        except Exception:
            pass

    result = None
    model: str | None = None
    usage: dict = {}
    if event_name in _TERMINAL_EVENTS:
        try:
            result, model, usage = _extract(event, provider)
        except Exception:
            result, model, usage = None, None, {}
    elif state == "waiting_approval":
        try:
            detail = _waiting_detail(event, event_name)
            result = _truncate(detail) if detail else None
        except Exception:
            result = None

    metadata: dict = {
        "event": event_name,
        "tool": event.get("tool_name", ""),
        "provider": provider,
    }
    if model:
        metadata["model"] = model
    if usage:
        metadata["usage"] = usage

    signal = {
        "agent_id": agent_id,
        "state": state,
        "timestamp": now.isoformat(),
        "seq": seq,
        "result": result,
        "error_detail": None,
        "metadata": metadata,
    }

    filename = f"{agent_id}-{seq}-{uuid.uuid4().hex[:6]}.signal.json"
    out_path = signal_dir / filename
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(signal))
        tmp_path.rename(out_path)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
