"""Tests for the Typer CLI."""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from sandboxpulse.cli import _pull_trigger_loop, _sweep_stale_tmp, app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "sandboxpulse" in result.stdout.lower()


def test_watch_is_the_only_consumer_command() -> None:
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--signal-dir" in result.stdout
    assert "--hermes-target" in result.stdout


def test_sweep_stale_tmp_only_removes_old_crumbs(tmp_path: Path) -> None:
    stale = tmp_path / "a.signal.json.tmp"
    fresh = tmp_path / "b.signal.json.tmp"
    signal = tmp_path / "c.signal.json"
    for p in (stale, fresh, signal):
        p.write_text("{}")
    hours_ago = time.time() - 7200
    os.utime(stale, (hours_ago, hours_ago))

    assert _sweep_stale_tmp(tmp_path) == 1
    assert not stale.exists()
    assert fresh.exists()  # a hook may still be mid-write
    assert signal.exists()  # never touch real signal files


@pytest.mark.asyncio
async def test_pull_trigger_loop_flushes_on_inbound(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    log.write_text("old line\n")
    hermes = Mock()
    task = asyncio.get_running_loop().create_task(
        _pull_trigger_loop(log, hermes, poll_s=0.05)
    )
    try:
        await asyncio.sleep(0.12)  # tailer records baseline position
        hermes.flush_now.assert_not_called()
        with log.open("a") as f:
            f.write("2026-06-11 INFO gateway.run: inbound message: platform=weixin user=x\n")
        deadline = asyncio.get_running_loop().time() + 3.0
        while not hermes.flush_now.called:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
