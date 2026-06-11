"""Hermes IM-gateway emitter (via the `hermes send` CLI).

The signal file on disk IS the delivery queue: a terminal signal's file is
removed only once its notification has been delivered, superseded by a newer
signal from the same agent, or cancelled by new activity from that agent.
Whatever is still on disk when the process dies is replayed by the next
watcher run — delivery is at-least-once, never silently dropped.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sandboxpulse.models import AgentState, Signal, is_terminal

_log = logging.getLogger(__name__)

_STATE_LABELS: dict[AgentState, str] = {
    AgentState.SUCCESS: "✅ success",
    AgentState.ERROR: "❌ error",
    AgentState.TIMEOUT: "⏰ timeout",
    AgentState.WAITING_APPROVAL: "⏸️ 等待你输入",
}


def _is_pause(state: AgentState) -> bool:
    """Pause notifications: the agent stopped mid-run to ask the user."""
    return state is AgentState.WAITING_APPROVAL


def _supersedes(new: Signal, queued: Signal) -> bool:
    """Whether `new` makes `queued` obsolete. A result (terminal) is never
    obsoleted by a pause notice, and always obsoletes a queued pause notice
    from its agent — the session moved past the pause; between signals of
    the same class the newer timestamp wins."""
    new_terminal, queued_terminal = is_terminal(new.state), is_terminal(queued.state)
    if queued_terminal and not new_terminal:
        return False
    if new_terminal and not queued_terminal:
        return True
    return new.timestamp >= queued.timestamp

# IM messages should stay readable; replies beyond this are cut.
_REPLY_MAX_CHARS = 2000


_BAR_SLOTS = 10


def _bar(used_percent: float) -> str:
    filled = max(0, min(_BAR_SLOTS, round(used_percent / 100 * _BAR_SLOTS)))
    return "█" * filled + "░" * (_BAR_SLOTS - filled)


def _parse_resets_at(raw: object) -> datetime | None:
    """resets_at as a datetime — codex sends epoch seconds, claude ISO 8601."""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _countdown(raw: object, now: datetime) -> str | None:
    resets = _parse_resets_at(raw)
    if resets is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    minutes = int((resets - now).total_seconds()) // 60
    if minutes < 0:
        return None
    if minutes >= 1440:
        return f"{minutes // 1440}d {minutes % 1440 // 60}h"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m"


def _usage_line(metadata: dict, now: datetime) -> str | None:
    """Quota footer with usage bars for the 5h and weekly rate windows, e.g.

    `Usage █░░░░░░░░░ 6% (resets in 4h 41m) | Weekly ██░░░░░░░░ 16% (...)`
    """
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        return None
    segments: list[str] = []
    for name, label in (("primary", "Usage"), ("secondary", "Weekly")):
        percent = usage.get(f"{name}_used_percent")
        if not isinstance(percent, (int, float)):
            continue
        segment = f"{label} {_bar(percent)} {round(percent)}%"
        countdown = _countdown(usage.get(f"{name}_resets_at"), now)
        if countdown:
            segment += f" (resets in {countdown})"
        segments.append(segment)
    return " | ".join(segments) if segments else None


def _reply_text(signal: Signal) -> str | None:
    if not isinstance(signal.result, str):
        return None
    text = signal.result.strip()
    if not text:
        return None
    if len(text) > _REPLY_MAX_CHARS:
        text = text[:_REPLY_MAX_CHARS].rstrip() + " …"
    return text


def _provider_of(signal: Signal) -> str:
    return str(signal.metadata.get("provider") or signal.agent_id.rsplit("-", 1)[0])


def _discard(path: Path) -> None:
    """The file's delivery obligation is met (sent, superseded, or cancelled)."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _wall_of(path: Path) -> float:
    """When the signal landed on disk — basis for the lateness marker, so it
    stays honest across process restarts."""
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


class _Pending(NamedTuple):
    signal: Signal
    path: Path
    attempts: int
    eligible_at: float  # event-loop monotonic time
    enqueued_wall: float  # wall clock the signal file was written


class HermesEmitter:
    """Push terminal and pause signals to an IM platform through `hermes send`.

    Plain activity signals (tool calls, generation progress) never reach
    chat. Terminal signals (the result) and waiting_approval signals (the
    agent paused mid-run on a question / permission prompt / idle wait) do.
    Notifying must never break or block a run.

    Delivery contract: terminal notifications MUST eventually arrive (the
    whole point of the system). Every send failure — non-zero exit, timeout,
    even a missing hermes binary — is retried forever with exponential
    backoff (capped). The signal file is the persistence: it is deleted only
    on delivery, supersede, or cancel, so anything undelivered at shutdown
    is simply picked up by the next run's backlog scan. Timeliness is
    protected by reducing volume instead of dropping:

    - `emit` returns immediately and the send fires at once (results must
      arrive immediately); `min_interval_s` only spaces multiple queued
      sends.
    - `debounce_s` (default 0 = off) optionally delays eligibility; any
      new activity from the same agent within that window cancels it (the
      session didn't really end — its later terminal signal supersedes).
      Cancel-on-activity applies to still-queued signals either way.
    - While queued, newer terminal signals replace older ones per agent;
      stale signals arriving out of order (backlog replay) are dropped.
    - Claude signals are sent before other providers when both are queued.
    - A message delivered more than `late_marker_after_s` after its signal
      was written is prefixed with a lateness marker instead of dropped.
    - `flush_now()` (wired to the user messaging the bot) empties the queue
      immediately with short pacing — a fresh inbound mints a new iLink
      context token, which is the one moment sends reliably succeed.
    - Pause notices (waiting_approval) send immediately but stay
      subordinate to results: they never displace or outlive a queued
      result, a result always replaces a queued pause, and once any
      notification is delivered for an agent further pauses are suppressed
      until that agent shows new activity (one ping per stop, not one per
      idle reminder).
    """

    def __init__(
        self,
        target: str,
        *,
        hermes_bin: str = "hermes",
        subject: str | None = None,
        timeout_s: float = 60.0,
        min_interval_s: float = 10.0,
        debounce_s: float = 0.0,
        retry_backoff_s: float = 300.0,
        retry_backoff_cap_s: float = 900.0,
        late_marker_after_s: float = 600.0,
        flush_interval_s: float = 10.0,
    ) -> None:
        self._target = target
        self._hermes_bin = hermes_bin
        self._subject = subject
        self._timeout_s = timeout_s
        self._min_interval_s = min_interval_s
        self._debounce_s = debounce_s
        self._retry_backoff_s = retry_backoff_s
        self._retry_backoff_cap_s = retry_backoff_cap_s
        self._late_marker_after_s = late_marker_after_s
        self._flush_interval_s = flush_interval_s
        self._flush_until = 0.0
        # agent_id -> latest pending notification; insertion order = FIFO
        self._pending: dict[str, _Pending] = {}
        # agents whose stop/pause the user has already been pinged about and
        # that have shown no activity since — repeat pauses are suppressed
        self._quiesced: set[str] = set()
        self._kick = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._next_send_at = 0.0
        self._closing = False

    @staticmethod
    def format_message(signal: Signal) -> str:
        provider = _provider_of(signal)
        model = signal.metadata.get("model")
        who = f"{provider} ({model})" if model else provider
        label = _STATE_LABELS.get(signal.state, signal.state.value)
        lines = [f"{who} {label}"]
        if signal.error_detail:
            lines.append(signal.error_detail)
        reply = _reply_text(signal)
        if reply:
            lines.append("")
            lines.append(reply)
        stats = _usage_line(signal.metadata, signal.timestamp)
        if stats:
            lines.append("")
            lines.append(stats)
        return "\n".join(lines)

    async def emit(self, signal: Signal, path: Path) -> None:
        """Take over `path`: it is deleted once its obligation is met."""
        if not is_terminal(signal.state) and not _is_pause(signal.state):
            # activity from this agent means the session didn't really end
            # (or the pause was answered at the terminal): a queued
            # notification is obsolete, and the next pause is news again
            self._quiesced.discard(signal.agent_id)
            queued = self._pending.get(signal.agent_id)
            if queued is not None and queued.signal.timestamp <= signal.timestamp:
                del self._pending[signal.agent_id]
                _discard(queued.path)
                _log.info(
                    "cancelled queued hermes notification for %s (agent active again)",
                    signal.agent_id,
                )
            _discard(path)
            return
        if self._closing:
            _log.warning(
                "hermes emitter closing; %s stays on disk for the next run", path.name
            )
            return
        if _is_pause(signal.state) and signal.agent_id in self._quiesced:
            # the user already knows this agent stopped or is waiting, and
            # nothing has happened since — a repeat ping adds nothing
            _log.info("suppressing repeat pause notification for %s", signal.agent_id)
            _discard(path)
            return
        prev = self._pending.get(signal.agent_id)
        if prev is not None:
            if not _supersedes(signal, prev.signal):
                _log.info(
                    "dropping signal for %s (seq %s does not supersede queued seq %s)",
                    signal.agent_id,
                    signal.seq,
                    prev.signal.seq,
                )
                _discard(path)
                return
            _log.info(
                "collapsing queued hermes notification for %s (seq %s superseded by seq %s)",
                signal.agent_id,
                prev.signal.seq,
                signal.seq,
            )
            _discard(prev.path)
        self._pending[signal.agent_id] = _Pending(
            signal=signal,
            path=path,
            attempts=0,
            eligible_at=asyncio.get_running_loop().time() + self._debounce_s,
            enqueued_wall=_wall_of(path),
        )
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(
                self._drain(), name="hermes-emitter-drain"
            )
        self._kick.set()

    def flush_now(self) -> None:
        """The user just messaged the bot — the iLink context token is
        freshly minted, which is the one moment sends reliably succeed.
        Make everything queued eligible immediately (skipping debounce and
        retry backoff) and use short pacing for the next minute.
        """
        loop = asyncio.get_running_loop()
        self._flush_until = loop.time() + 60.0
        self._next_send_at = 0.0
        if self._pending:
            self._pending = {
                agent_id: entry._replace(eligible_at=0.0)
                for agent_id, entry in self._pending.items()
            }
            _log.info("inbound from user — flushing %d queued notification(s)", len(self._pending))
            if self._worker is None or self._worker.done():
                self._worker = loop.create_task(self._drain(), name="hermes-emitter-drain")
        self._kick.set()

    async def aclose(self) -> None:
        """Try each queued notification once more (without pacing/debounce)
        and stop. Whatever still fails keeps its signal file on disk and is
        replayed by the next watcher run."""
        self._closing = True
        self._kick.set()
        if self._worker is not None:
            await self._worker
            self._worker = None

    def _pick(self, candidates: list[str]) -> str:
        """Claude notifications jump the queue; otherwise FIFO."""
        for agent_id in candidates:
            if _provider_of(self._pending[agent_id].signal) == "claude":
                return agent_id
        return candidates[0]

    async def _drain(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                self._kick.clear()
                if not self._pending:
                    if self._closing:
                        return
                    await self._kick.wait()
                    continue
                if self._closing:
                    entry = self._pending.pop(self._pick(list(self._pending)))
                    if await self._send(entry):
                        _discard(entry.path)
                    else:
                        _log.warning(
                            "undelivered notification for %s stays on disk for the next run",
                            entry.signal.agent_id,
                        )
                    continue
                now = loop.time()
                eligible = [a for a, e in self._pending.items() if e.eligible_at <= now]
                if not eligible:
                    next_at = min(e.eligible_at for e in self._pending.values())
                    # interruptible: aclose() or new signals re-evaluate early
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._kick.wait(), timeout=next_at - now)
                    continue
                if self._next_send_at > now:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._kick.wait(), timeout=self._next_send_at - now
                        )
                    continue
                agent_id = self._pick(eligible)
                entry = self._pending.pop(agent_id)
                if await self._send(entry):
                    _discard(entry.path)
                    self._quiesced.add(agent_id)
                    leftover = self._pending.get(agent_id)
                    if leftover is not None and (
                        _is_pause(leftover.signal.state)
                        or (
                            is_terminal(entry.signal.state)
                            and leftover.signal.timestamp <= entry.signal.timestamp
                        )
                    ):
                        # slipped in while this one was in flight: any pause
                        # notice is redundant now (the user was just pinged),
                        # and an out-of-order older result must not go out
                        # after a newer one — but a queued result never dies
                        # to a delivered pause
                        del self._pending[agent_id]
                        _discard(leftover.path)
                    interval = (
                        self._flush_interval_s
                        if loop.time() < self._flush_until
                        else self._min_interval_s
                    )
                    self._next_send_at = loop.time() + interval
                else:
                    attempts = entry.attempts + 1
                    backoff = min(
                        self._retry_backoff_s * (3**entry.attempts),
                        self._retry_backoff_cap_s,
                    )
                    newer = self._pending.get(agent_id)
                    if newer is None or not _supersedes(newer.signal, entry.signal):
                        # nothing meanwhile, or only something that cannot
                        # replace this one (e.g. a pause notice vs a result)
                        if newer is not None:
                            _discard(newer.path)
                        self._pending[agent_id] = entry._replace(
                            attempts=attempts, eligible_at=loop.time()
                        )
                    else:
                        # a superseding signal replaced this one while it was
                        # in flight
                        _discard(entry.path)
                    _log.warning(
                        "hermes send failed; retrying %s in %.0fs (try %d)",
                        agent_id,
                        backoff,
                        attempts,
                    )
                    self._next_send_at = loop.time() + backoff
        except Exception:
            _log.exception("hermes emitter worker crashed")

    async def _send(self, entry: _Pending) -> bool:
        """Run one `hermes send`. True means delivered; anything else —
        non-zero exit, timeout, missing binary — is retried by the caller."""
        message = self.format_message(entry.signal)
        delay_s = time.time() - entry.enqueued_wall
        if delay_s > self._late_marker_after_s:
            message = f"⏰ 迟到 {max(1, int(delay_s // 60))}m\n{message}"
        argv = [self._hermes_bin, "send", "--to", self._target]
        if self._subject:
            argv += ["--subject", self._subject]
        argv += ["--quiet", message]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _log.error("hermes binary not found: %s", self._hermes_bin)
            return False
        except Exception:
            _log.exception("failed to spawn hermes send")
            return False
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            _log.error("hermes send timed out after %.0fs", self._timeout_s)
            return False
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            _log.error(
                "hermes send failed (exit %s): %s",
                proc.returncode,
                detail or "(no stderr — see ~/.hermes/logs/errors.log)",
            )
            return False
        return True
