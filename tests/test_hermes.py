"""Tests for HermesEmitter."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sandboxpulse.hermes import HermesEmitter
from sandboxpulse.models import AgentState, Signal

_TS = datetime(2026, 6, 10, tzinfo=UTC)


def _signal(
    state: AgentState = AgentState.SUCCESS,
    *,
    agent_id: str = "claude-abc12345",
    seq: int = 0,
    ts: datetime = _TS,
    result: Any | None = None,
    error_detail: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        agent_id=agent_id,
        state=state,
        timestamp=ts,
        seq=seq,
        result=result,
        error_detail=error_detail,
        metadata=metadata or {},
    )


def _drop(directory: Path, sig: Signal) -> Path:
    """Write the signal file the way the hook does."""
    path = directory / f"{sig.agent_id}-{sig.seq}.signal.json"
    path.write_text(sig.model_dump_json())
    return path


async def _emit(em: HermesEmitter, directory: Path, sig: Signal) -> Path:
    path = _drop(directory, sig)
    await em.emit(sig, path)
    return path


class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


async def _until(predicate: Any, wait_s: float = 3.0) -> None:
    """Poll until predicate() is true; the emitter sends from a background task."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while not predicate():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_skips_activity_states_and_consumes_files(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat")
    paths = []
    with patch("sandboxpulse.hermes.asyncio.create_subprocess_exec") as spawn:
        for seq, state in enumerate(
            (
                AgentState.IDLE,
                AgentState.GENERATING,
                AgentState.TOOL_CALLING,
                AgentState.EXECUTING,
            )
        ):
            paths.append(await _emit(em, signal_dir, _signal(state, seq=seq)))
    spawn.assert_not_called()
    assert not any(p.exists() for p in paths)  # activity markers carry no value


@pytest.mark.asyncio
async def test_sends_terminal_signal_and_deletes_file(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat")
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(
            em,
            signal_dir,
            _signal(metadata={"provider": "claude", "model": "claude-opus-4-8"}),
        )
        await em.aclose()

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "hermes"
    assert argv[1] == "send"
    assert "--to" in argv
    assert "weixin:user@im.wechat" in argv
    assert "--subject" not in argv
    message = argv[-1]
    assert message.startswith("claude (claude-opus-4-8)")
    assert "success" in message
    assert not path.exists()  # delivered -> obligation met


@pytest.mark.asyncio
async def test_subject_passed_when_configured(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", subject="[SandboxPulse]")
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal())
        await em.aclose()

    argv = calls[0]
    assert "--subject" in argv
    assert "[SandboxPulse]" in argv


def test_format_message_includes_agent_reply() -> None:
    msg = HermesEmitter.format_message(
        _signal(result="Done — fixed the bug.", metadata={"provider": "codex"})
    )
    assert msg.splitlines()[0] == "codex ✅ success"
    assert "Done — fixed the bug." in msg


def test_format_message_ignores_non_string_result() -> None:
    msg = HermesEmitter.format_message(_signal(result={"exit_code": 0}))
    assert "exit_code" not in msg


def test_format_message_truncates_long_reply() -> None:
    msg = HermesEmitter.format_message(_signal(result="x" * 5000))
    assert len(msg) < 2200
    assert msg.endswith("…")


def test_format_message_falls_back_to_agent_id_prefix() -> None:
    msg = HermesEmitter.format_message(_signal())
    assert msg.startswith("claude ")
    assert "success" in msg


def test_format_message_renders_claude_usage_line() -> None:
    # the signal timestamp is 2026-06-10T00:00:00Z (see _signal)
    msg = HermesEmitter.format_message(
        _signal(
            metadata={
                "provider": "claude",
                "usage": {
                    "context_tokens": 118027,
                    "primary_used_percent": 6.0,
                    "primary_window_minutes": 300,
                    "primary_resets_at": "2026-06-10T04:41:30+00:00",
                    "secondary_used_percent": 16.0,
                    "secondary_window_minutes": 10080,
                    "secondary_resets_at": "2026-06-15T17:30:00+00:00",
                },
            }
        )
    )
    assert msg.splitlines()[-1] == (
        "Usage █░░░░░░░░░ 6% (resets in 4h 41m) | Weekly ██░░░░░░░░ 16% (resets in 5d 17h)"
    )


def test_format_message_drops_usage_without_quota_windows() -> None:
    msg = HermesEmitter.format_message(
        _signal(metadata={"provider": "claude", "usage": {"context_tokens": 118027}})
    )
    assert "Usage" not in msg


def test_format_message_renders_codex_usage_line() -> None:
    # codex reports resets_at as epoch seconds, relative to the signal time
    base = int(_TS.timestamp())
    msg = HermesEmitter.format_message(
        _signal(
            result="ok",
            metadata={
                "provider": "codex",
                "model": "gpt-5.5",
                "usage": {
                    "context_tokens": 13295,
                    "context_window": 258400,
                    "primary_used_percent": 8.0,
                    "primary_window_minutes": 300,
                    "primary_resets_at": base + 281 * 60 + 30,
                    "secondary_used_percent": 4.0,
                    "secondary_window_minutes": 10080,
                    "secondary_resets_at": base + 8250 * 60,
                },
            },
        )
    )
    lines = msg.splitlines()
    assert lines[0] == "codex (gpt-5.5) ✅ success"
    assert lines[-1] == (
        "Usage █░░░░░░░░░ 8% (resets in 4h 41m) | Weekly ░░░░░░░░░░ 4% (resets in 5d 17h)"
    )


def test_format_message_no_usage_line_without_usage() -> None:
    msg = HermesEmitter.format_message(_signal(metadata={"usage": "garbage"}))
    assert "Usage" not in msg


@pytest.mark.asyncio
async def test_error_detail_included_in_message(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat")
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(AgentState.ERROR, error_detail="boom: it broke"))
        await em.aclose()

    message = calls[0][-1]
    assert "error" in message
    assert "boom: it broke" in message


@pytest.mark.asyncio
async def test_missing_binary_retries_until_delivered(signal_dir: Path) -> None:
    """A missing binary is transient (PATH gaps after reboot) — the message
    must survive it, not be dropped."""
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.05,
    )
    attempts: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        attempts.append(args)
        if len(attempts) == 1:
            raise FileNotFoundError
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal(result="survives missing binary"))
        await _until(lambda: len(attempts) == 2)
        await em.aclose()

    assert attempts[0][-1] == attempts[1][-1]  # same message retried
    assert not path.exists()


@pytest.mark.asyncio
async def test_timeout_kills_process_and_retries(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        timeout_s=0.05,
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.05,
    )
    hung = _FakeProc()

    async def hang(self: Any = None) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    hung.communicate = hang  # type: ignore[method-assign]
    procs = iter([hung, _FakeProc(returncode=0)])
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return next(procs)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal())
        await _until(lambda: len(calls) == 2)
        await em.aclose()

    assert hung.killed
    assert not path.exists()


@pytest.mark.asyncio
async def test_burst_collapses_to_latest_per_agent(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=60.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        p1 = await _emit(em, signal_dir, _signal(result="first", seq=1, ts=_TS))
        await _until(lambda: len(calls) == 1)  # leading edge: sent immediately
        p2 = await _emit(
            em, signal_dir, _signal(result="second", seq=2, ts=_TS + timedelta(seconds=1))
        )
        p3 = await _emit(
            em, signal_dir, _signal(result="third", seq=3, ts=_TS + timedelta(seconds=2))
        )
        assert not p2.exists()  # superseded while queued
        await em.aclose()

    assert len(calls) == 2
    assert "first" in calls[0][-1]
    assert "third" in calls[1][-1]
    assert all("second" not in c[-1] for c in calls)
    assert not p1.exists() and not p3.exists()


@pytest.mark.asyncio
async def test_stale_terminal_is_dropped(signal_dir: Path) -> None:
    """Backlog replay can deliver files out of order; an older terminal must
    never replace a newer queued one."""
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        newer = await _emit(em, signal_dir, _signal(result="newer", seq=2, ts=_TS))
        stale = await _emit(
            em, signal_dir, _signal(result="stale", seq=1, ts=_TS - timedelta(minutes=5))
        )
        assert not stale.exists()
        assert newer.exists()
        await em.aclose()

    assert len(calls) == 1
    assert "newer" in calls[0][-1]


@pytest.mark.asyncio
async def test_stale_activity_does_not_cancel(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="keep me", seq=2, ts=_TS))
        await _emit(
            em,
            signal_dir,
            _signal(AgentState.TOOL_CALLING, seq=1, ts=_TS - timedelta(minutes=5)),
        )
        await em.aclose()

    assert len(calls) == 1
    assert "keep me" in calls[0][-1]


@pytest.mark.asyncio
async def test_sends_paced_by_min_interval(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.2, debounce_s=0.0)
    times: list[float] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        times.append(asyncio.get_running_loop().time())
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="agent one"))
        await _emit(
            em, signal_dir, _signal(result="agent two", agent_id="codex-zzz99999", seq=1)
        )
        await _until(lambda: len(times) == 2)
        await em.aclose()

    assert len(times) == 2
    assert times[1] - times[0] >= 0.18


@pytest.mark.asyncio
async def test_failed_send_keeps_file_and_retries_after_backoff(signal_dir: Path) -> None:
    # `hermes send --quiet` exits non-zero with EMPTY stderr on rate limit;
    # any non-zero exit must back off and retry.
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.2,
    )
    procs = iter([_FakeProc(returncode=1, stderr=b""), _FakeProc(returncode=0)])
    times: list[float] = []
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        times.append(asyncio.get_running_loop().time())
        calls.append(args)
        return next(procs)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal(result="hello"))
        await _until(lambda: len(times) == 1)
        await asyncio.sleep(0.05)  # still inside the backoff window
        assert path.exists()  # undelivered -> the file is the persistence
        await _until(lambda: len(times) == 2)
        await em.aclose()

    assert times[1] - times[0] >= 0.18  # waited out the backoff
    assert calls[0][-1] == calls[1][-1]  # same message retried
    assert not path.exists()


@pytest.mark.asyncio
async def test_retry_backoff_grows_exponentially(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.1,
    )
    procs = iter(
        [
            _FakeProc(returncode=1, stderr=b""),
            _FakeProc(returncode=1, stderr=b""),
            _FakeProc(returncode=0),
        ]
    )
    times: list[float] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        times.append(asyncio.get_running_loop().time())
        return next(procs)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="hello"))
        await _until(lambda: len(times) == 3)
        await em.aclose()

    assert times[1] - times[0] >= 0.08  # first backoff ~0.1s
    assert times[2] - times[1] >= 0.28  # second backoff ~0.3s (3x)


@pytest.mark.asyncio
async def test_retries_forever(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.01,
        retry_backoff_cap_s=0.02,
    )
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=1, stderr=b"")

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal())
        await _until(lambda: len(calls) >= 6)  # no give-up threshold exists
        await em.aclose()

    assert path.exists()  # still undelivered -> still on disk for the next run


@pytest.mark.asyncio
async def test_aclose_flushes_pending_without_pacing(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=60.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="agent one"))
        await _until(lambda: len(calls) == 1)
        path = await _emit(
            em, signal_dir, _signal(result="agent two", agent_id="codex-zzz99999", seq=1)
        )  # queued 60s out
        start = asyncio.get_running_loop().time()
        await em.aclose()  # must flush immediately, not wait out the interval
        elapsed = asyncio.get_running_loop().time() - start

    assert len(calls) == 2
    assert "agent two" in calls[1][-1]
    assert elapsed < 5.0
    assert not path.exists()


@pytest.mark.asyncio
async def test_aclose_leaves_undeliverable_file_on_disk(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=60.0,
    )
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=1, stderr=b"")

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal(result="must survive restart"))
        await _until(lambda: len(calls) == 1)  # first try fails, requeued
        await em.aclose()  # final try also fails -> file stays put

    assert len(calls) == 2
    assert path.exists()
    restored = Signal.model_validate_json(path.read_text())
    assert restored.result == "must survive restart"


@pytest.mark.asyncio
async def test_emit_while_closing_leaves_file(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat")
    await em.aclose()
    with patch("sandboxpulse.hermes.asyncio.create_subprocess_exec") as spawn:
        path = await _emit(em, signal_dir, _signal())
    spawn.assert_not_called()
    assert path.exists()  # the next watcher run delivers it


@pytest.mark.asyncio
async def test_debounce_delays_send(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=0.2)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal())
        await asyncio.sleep(0.05)
        assert calls == []  # still inside the quiet window
        await _until(lambda: len(calls) == 1)
        await em.aclose()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_agent_activity_cancels_pending_notification(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        queued = await _emit(em, signal_dir, _signal(result="will be cancelled", seq=1))
        # the same agent starts a new turn before the quiet window elapses
        activity = await _emit(
            em, signal_dir, _signal(AgentState.TOOL_CALLING, seq=2, ts=_TS + timedelta(seconds=1))
        )
        await em.aclose()

    assert calls == []
    assert not queued.exists() and not activity.exists()


@pytest.mark.asyncio
async def test_late_delivery_gets_marker(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        sig = _signal(result="slow one")
        path = _drop(signal_dir, sig)
        hour_ago = time.time() - 3600  # backlog file written an hour ago
        os.utime(path, (hour_ago, hour_ago))
        await em.emit(sig, path)
        await _until(lambda: len(calls) == 1)
        await em.aclose()

    assert calls[0][-1].startswith("⏰ 迟到 ")


@pytest.mark.asyncio
async def test_claude_jumps_queue_ahead_of_codex(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.15, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="warmup"))
        await _until(lambda: len(calls) == 1)  # consume the leading-edge slot
        await _emit(
            em,
            signal_dir,
            _signal(
                result="codex msg",
                agent_id="codex-zzz99999",
                seq=1,
                metadata={"provider": "codex"},
            ),
        )  # queued first…
        await _emit(
            em, signal_dir, _signal(result="claude msg", agent_id="claude-yyy88888", seq=2)
        )
        await _until(lambda: len(calls) == 3)
        await em.aclose()

    assert "claude msg" in calls[1][-1]  # …but claude goes out first
    assert "codex msg" in calls[2][-1]


@pytest.mark.asyncio
async def test_flush_now_sends_queued_immediately_claude_first(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=60.0,
        debounce_s=60.0,
        flush_interval_s=0.05,
    )
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(
            em,
            signal_dir,
            _signal(
                result="codex msg",
                agent_id="codex-zzz99999",
                seq=1,
                metadata={"provider": "codex"},
            ),
        )
        await _emit(em, signal_dir, _signal(result="claude msg", seq=2))
        await asyncio.sleep(0.05)
        assert calls == []  # both held by debounce
        em.flush_now()
        await _until(lambda: len(calls) == 2)
        await em.aclose()

    assert "claude msg" in calls[0][-1]  # claude jumps the flush queue
    assert "codex msg" in calls[1][-1]


@pytest.mark.asyncio
async def test_waiting_sends_immediately_with_question(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(
            em,
            signal_dir,
            _signal(AgentState.WAITING_APPROVAL, result="数据库怎么分?\n1. 同实例新建库"),
        )
        await _until(lambda: len(calls) == 1)
        await em.aclose()

    message = calls[0][-1]
    assert "等待你输入" in message
    assert "数据库怎么分?" in message
    assert not path.exists()


@pytest.mark.asyncio
async def test_repeat_pause_suppressed_until_new_activity(signal_dir: Path) -> None:
    """One ping per stop: after a delivered pause, further pauses (e.g. the
    60s idle reminder) stay silent until the agent does something again."""
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(AgentState.WAITING_APPROVAL, result="q1", seq=1))
        await _until(lambda: len(calls) == 1)
        repeat = await _emit(
            em,
            signal_dir,
            _signal(AgentState.WAITING_APPROVAL, result="q1", seq=2, ts=_TS + timedelta(seconds=60)),
        )
        await asyncio.sleep(0.05)
        assert len(calls) == 1  # suppressed
        assert not repeat.exists()
        # the user answers at the terminal -> activity -> next pause is news
        await _emit(
            em, signal_dir, _signal(AgentState.TOOL_CALLING, seq=3, ts=_TS + timedelta(seconds=61))
        )
        await _emit(
            em,
            signal_dir,
            _signal(AgentState.WAITING_APPROVAL, result="q2", seq=4, ts=_TS + timedelta(seconds=62)),
        )
        await _until(lambda: len(calls) == 2)
        await em.aclose()

    assert "q2" in calls[1][-1]


@pytest.mark.asyncio
async def test_idle_pause_after_delivered_result_is_suppressed(signal_dir: Path) -> None:
    """Stop already pinged the user; the idle reminder a minute later must
    not ping again."""
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=0.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        await _emit(em, signal_dir, _signal(result="done", seq=1))
        await _until(lambda: len(calls) == 1)
        idle = await _emit(
            em,
            signal_dir,
            _signal(
                AgentState.WAITING_APPROVAL,
                result="Claude is waiting for your input",
                seq=2,
                ts=_TS + timedelta(seconds=60),
            ),
        )
        await asyncio.sleep(0.05)
        await em.aclose()

    assert len(calls) == 1
    assert not idle.exists()


@pytest.mark.asyncio
async def test_pause_never_displaces_queued_result(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        result = await _emit(em, signal_dir, _signal(result="the result", seq=1))
        pause = await _emit(
            em,
            signal_dir,
            _signal(AgentState.WAITING_APPROVAL, result="a pause", seq=2, ts=_TS + timedelta(seconds=1)),
        )
        assert not pause.exists()  # dropped, result still queued
        assert result.exists()
        await em.aclose()

    assert len(calls) == 1
    assert "the result" in calls[0][-1]


@pytest.mark.asyncio
async def test_result_replaces_queued_pause(signal_dir: Path) -> None:
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        pause = await _emit(
            em, signal_dir, _signal(AgentState.WAITING_APPROVAL, result="a pause", seq=1)
        )
        await _emit(
            em,
            signal_dir,
            _signal(result="the result", seq=2, ts=_TS + timedelta(seconds=1)),
        )
        assert not pause.exists()
        await em.aclose()

    assert len(calls) == 1
    assert "the result" in calls[0][-1]


@pytest.mark.asyncio
async def test_activity_cancels_queued_pause(signal_dir: Path) -> None:
    """The user answered at the terminal before the ping went out — there is
    nothing to notify anymore."""
    em = HermesEmitter(target="weixin:user@im.wechat", min_interval_s=0.0, debounce_s=5.0)
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return _FakeProc(returncode=0)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        pause = await _emit(
            em, signal_dir, _signal(AgentState.WAITING_APPROVAL, result="a pause", seq=1)
        )
        await _emit(
            em,
            signal_dir,
            _signal(AgentState.EXECUTING, seq=2, ts=_TS + timedelta(seconds=1)),
        )
        await em.aclose()

    assert calls == []
    assert not pause.exists()


@pytest.mark.asyncio
async def test_failed_result_survives_pause_queued_during_flight(signal_dir: Path) -> None:
    """A result whose send failed must be retried even if a pause notice got
    queued while it was in flight — pause never outranks result."""
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=0.0,
        debounce_s=0.0,
        retry_backoff_s=0.1,
    )
    procs = iter([_FakeProc(returncode=1, stderr=b""), _FakeProc(returncode=0)])
    calls: list[tuple[Any, ...]] = []
    em_holder: dict[str, Any] = {}

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        if len(calls) == 1:
            # while the result send is in flight, a pause notice arrives
            sig = _signal(
                AgentState.WAITING_APPROVAL,
                result="late pause",
                seq=2,
                ts=_TS + timedelta(seconds=1),
            )
            await em_holder["em"].emit(sig, _drop(signal_dir, sig))
        return next(procs)

    em_holder["em"] = em
    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal(result="the result", seq=1))
        await _until(lambda: len(calls) == 2)
        await em.aclose()

    assert "the result" in calls[1][-1]  # the retry is the result, not the pause
    assert not path.exists()


@pytest.mark.asyncio
async def test_flush_window_overrides_retry_backoff(signal_dir: Path) -> None:
    em = HermesEmitter(
        target="weixin:user@im.wechat",
        min_interval_s=60.0,
        debounce_s=0.0,
        retry_backoff_s=60.0,
        flush_interval_s=0.05,
    )
    procs = iter([_FakeProc(returncode=1, stderr=b""), _FakeProc(returncode=0)])
    calls: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        return next(procs)

    with patch(
        "sandboxpulse.hermes.asyncio.create_subprocess_exec", side_effect=fake_spawn
    ):
        path = await _emit(em, signal_dir, _signal(result="stuck behind backoff"))
        await _until(lambda: len(calls) == 1)  # first try fails -> 60s backoff
        em.flush_now()  # user messaged the bot: retry NOW
        await _until(lambda: len(calls) == 2)
        await em.aclose()

    assert "stuck behind backoff" in calls[1][-1]
    assert not path.exists()
