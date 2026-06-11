"""Watchdog-based inbound signal watcher.

Signal files are the delivery queue: each file's parsed Signal is handed
downstream together with its path, and the consumer deletes the file once
its obligation is met. On start the backlog already on disk is replayed in
mtime order, so signals written while no watcher was running still get
delivered; a file queued twice (backlog scan and inotify can overlap) is
simply skipped the second time because it is already gone.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sandboxpulse.models import Signal

_log = logging.getLogger(__name__)

OnSignal = Callable[[Signal, Path], Awaitable[None]]


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class _Handler(FileSystemEventHandler):
    def __init__(self, queue: asyncio.Queue[Path], loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if not path.name.endswith(".signal.json"):
            return
        asyncio.run_coroutine_threadsafe(self._queue.put(path), self._loop)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "") or ""
        if dest:
            self._enqueue(str(dest))


class SignalFileWatcher:
    def __init__(self, *, directory: Path, on_signal: OnSignal) -> None:
        self._dir = Path(directory)
        self._on_signal = on_signal
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._observer: Any | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> int:
        """Start watching. Returns the number of backlog files queued."""
        if self._running:
            return 0
        self._dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        self._observer = Observer()
        self._observer.schedule(_Handler(self._queue, loop), str(self._dir), recursive=False)
        self._observer.start()
        # scan after the observer is live so no file falls between the two;
        # mtime order lets per-agent collapse downstream keep the newest
        backlog = sorted(self._dir.glob("*.signal.json"), key=_mtime_or_zero)
        for path in backlog:
            self._queue.put_nowait(path)
        self._running = True
        self._consumer = asyncio.create_task(self._consume())
        return len(backlog)

    async def _consume(self) -> None:
        while self._running:
            try:
                path = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                raw = path.read_text()
            except OSError:
                continue  # already consumed via a duplicate queue entry
            try:
                signal = Signal.model_validate_json(raw)
            except (ValidationError, ValueError) as exc:
                _log.warning("dropping malformed signal file %s: %s", path.name, exc)
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            try:
                await self._on_signal(signal, path)
            except Exception:
                _log.exception("on_signal callback failed")

    async def stop(self) -> None:
        self._running = False
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=1.0)
            self._observer = None
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._consumer
            self._consumer = None
