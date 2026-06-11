"""Tests for SignalFileWatcher."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandboxpulse.models import AgentState, Signal
from sandboxpulse.watcher import SignalFileWatcher


def _payload(seq: int) -> str:
    return Signal(
        agent_id="a1",
        state=AgentState.SUCCESS,
        timestamp=datetime(2026, 6, 7, tzinfo=UTC),
        seq=seq,
    ).model_dump_json()


async def _until(predicate: object, wait_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while not predicate():  # type: ignore[operator]
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_watcher_emits_on_new_file(tmp_path: Path) -> None:
    received: list[tuple[Signal, Path]] = []

    async def consume(signal: Signal, path: Path) -> None:
        received.append((signal, path))

    watcher = SignalFileWatcher(directory=tmp_path, on_signal=consume)
    await watcher.start()
    try:
        target = tmp_path / "a1-000001.signal.json"
        target.write_text(_payload(1))
        await _until(lambda: received)
    finally:
        await watcher.stop()
    assert received[0][0].seq == 1
    assert received[0][1] == target


@pytest.mark.asyncio
async def test_watcher_emits_on_atomic_rename(tmp_path: Path) -> None:
    """Hooks write via tmp + rename (atomic). Inotify reports this as IN_MOVED_TO."""
    received: list[tuple[Signal, Path]] = []

    async def consume(signal: Signal, path: Path) -> None:
        received.append((signal, path))

    watcher = SignalFileWatcher(directory=tmp_path, on_signal=consume)
    await watcher.start()
    try:
        final = tmp_path / "a1-000003.signal.json"
        tmp = final.with_name(final.name + ".tmp")
        tmp.write_text(_payload(3))
        tmp.rename(final)
        await _until(lambda: received)
    finally:
        await watcher.stop()
    assert received[0][0].seq == 3


@pytest.mark.asyncio
async def test_malformed_file_is_deleted(tmp_path: Path) -> None:
    received: list[Signal] = []

    async def consume(signal: Signal, path: Path) -> None:
        received.append(signal)

    bad = tmp_path / "bad.signal.json"
    watcher = SignalFileWatcher(directory=tmp_path, on_signal=consume)
    await watcher.start()
    try:
        bad.write_text("{ not valid")
        (tmp_path / "good.signal.json").write_text(_payload(2))
        await _until(lambda: received)
        await _until(lambda: not bad.exists())
    finally:
        await watcher.stop()
    assert any(s.seq == 2 for s in received)


@pytest.mark.asyncio
async def test_backlog_replayed_in_mtime_order(tmp_path: Path) -> None:
    """Signals written while no watcher was running are delivered on start,
    oldest first (names deliberately disagree with mtime order)."""
    received: list[int] = []

    async def consume(signal: Signal, path: Path) -> None:
        received.append(signal.seq)
        path.unlink(missing_ok=True)

    now = time.time()
    for name, seq, age_s in (
        ("z-oldest.signal.json", 1, 300),
        ("a-newest.signal.json", 3, 100),
        ("m-middle.signal.json", 2, 200),
    ):
        p = tmp_path / name
        p.write_text(_payload(seq))
        os.utime(p, (now - age_s, now - age_s))

    watcher = SignalFileWatcher(directory=tmp_path, on_signal=consume)
    backlog = await watcher.start()
    try:
        await _until(lambda: len(received) == 3)
    finally:
        await watcher.stop()
    assert backlog == 3
    assert received == [1, 2, 3]


@pytest.mark.asyncio
async def test_duplicate_queue_entry_is_skipped(tmp_path: Path) -> None:
    """Backlog scan and inotify can both queue the same file; once consumed
    (deleted by the callback) the second sighting is a no-op."""
    received: list[Signal] = []

    async def consume(signal: Signal, path: Path) -> None:
        received.append(signal)
        path.unlink(missing_ok=True)

    watcher = SignalFileWatcher(directory=tmp_path, on_signal=consume)
    await watcher.start()
    try:
        target = tmp_path / "a1-000001.signal.json"
        target.write_text(_payload(1))
        await _until(lambda: received)
        watcher._queue.put_nowait(target)  # simulate the overlapping sighting
        await asyncio.sleep(0.2)
    finally:
        await watcher.stop()
    assert len(received) == 1
