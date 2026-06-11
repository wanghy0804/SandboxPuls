"""Typer CLI for SandboxPulse."""
from __future__ import annotations

import asyncio
import contextlib
import signal as _signal
import time
from pathlib import Path

import typer
from rich.console import Console

from sandboxpulse import __version__
from sandboxpulse.config import Settings
from sandboxpulse.hermes import HermesEmitter
from sandboxpulse.logging import configure_logging
from sandboxpulse.models import Signal
from sandboxpulse.usage import enrich_signal_usage
from sandboxpulse.watcher import SignalFileWatcher

app = typer.Typer(help="SandboxPulse — forwards AI coding agent session signals to IM")
console = Console()

# a *.tmp this old can only be the crumb of a hook that died mid-write
_TMP_MAX_AGE_S = 3600.0


def _sweep_stale_tmp(signal_dir: Path) -> int:
    if not signal_dir.exists():
        return 0
    cutoff = time.time() - _TMP_MAX_AGE_S
    count = 0
    for p in signal_dir.glob("*.signal.json.tmp"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                count += 1
        except OSError:
            pass
    return count


def _install_stop_handler(stop_event: asyncio.Event) -> None:
    """Make systemd's SIGTERM a graceful stop so queued notifications get a
    final send attempt before the process exits."""
    with contextlib.suppress(NotImplementedError, RuntimeError):
        asyncio.get_running_loop().add_signal_handler(_signal.SIGTERM, stop_event.set)


_INBOUND_MARKER = "inbound message: platform=weixin"


async def _pull_trigger_loop(
    log_path: Path, hermes: HermesEmitter, *, poll_s: float = 2.0
) -> None:
    """Tail the hermes gateway log; a new weixin inbound means the user just
    messaged the bot — the context token is freshly minted, so flush the
    notification queue while sends can actually succeed."""
    pos = log_path.stat().st_size if log_path.exists() else 0
    while True:
        await asyncio.sleep(poll_s)
        try:
            size = log_path.stat().st_size
        except OSError:
            continue
        if size < pos:  # log rotated/truncated
            pos = 0
        if size == pos:
            continue
        try:
            with log_path.open("r", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            continue
        if _INBOUND_MARKER in chunk:
            hermes.flush_now()


@app.command()
def version() -> None:
    """Print the SandboxPulse version."""
    console.print(f"sandboxpulse {__version__}")


@app.command()
def watch(
    signal_dir: Path = typer.Option(Path("./signals"), "--signal-dir"),
    hermes_target: str | None = typer.Option(
        None,
        "--hermes-target",
        help="Forward terminal/waiting signals via hermes send (default: $SANDBOXPULSE_HERMES_TARGET)",
    ),
) -> None:
    """Consume inbound .signal.json files and forward terminal/waiting ones to IM."""
    settings = Settings()
    configure_logging(settings.log_level)
    target = hermes_target or settings.hermes_target
    swept = _sweep_stale_tmp(signal_dir)
    if swept:
        console.print(f"[dim]swept {swept} stale tmp file(s)[/dim]")
    console.print(f"[bold]Watching[/bold] {signal_dir} (Ctrl+C to stop)")
    if target:
        console.print(
            f"[dim]forwarding terminal/waiting signals -> hermes send --to {target}[/dim]"
        )

    async def go() -> None:
        stop_event = asyncio.Event()
        _install_stop_handler(stop_event)
        hermes = (
            HermesEmitter(
                target,
                min_interval_s=settings.hermes_min_interval_s,
                debounce_s=settings.hermes_debounce_s,
            )
            if target
            else None
        )

        async def on_signal(s: Signal, path: Path) -> None:
            console.print(s.model_dump_json())
            if hermes is None:
                # journal-only mode still consumes what it has seen
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return
            await asyncio.to_thread(enrich_signal_usage, s)
            await hermes.emit(s, path)

        watcher = SignalFileWatcher(directory=signal_dir, on_signal=on_signal)
        backlog = await watcher.start()
        if backlog:
            console.print(f"[dim]replaying {backlog} backlog signal(s)[/dim]")
        pull_task: asyncio.Task[None] | None = None
        if hermes is not None and settings.hermes_pull_log:
            pull_task = asyncio.create_task(
                _pull_trigger_loop(settings.hermes_pull_log.expanduser(), hermes),
                name="hermes-pull-trigger",
            )
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            if pull_task is not None:
                pull_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pull_task
            await watcher.stop()
            if hermes is not None:
                await hermes.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        console.print("[dim]stopped[/dim]")


if __name__ == "__main__":
    app()
