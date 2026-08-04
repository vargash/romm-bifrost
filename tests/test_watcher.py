from __future__ import annotations

import subprocess
import sys
from typing import Any

import bifrost.watcher as watcher


def test_run_sync_invokes_multi_token_command_as_separate_argv(monkeypatch) -> None:
    """Regression: bifrost_cmd used to be collapsed into a single string (e.g.
    "<python> -m bifrost.cli"), which subprocess tried to exec as one literal
    filename containing spaces and always failed with FileNotFoundError. It
    must be passed as a real argv prefix so each token is a separate arg.
    """
    monkeypatch.setattr(watcher, "_last_sync_time", 0.0)
    captured: dict[str, Any] = {}

    def fake_run(cmd_args, **kwargs):  # noqa: ANN001
        captured["cmd_args"] = cmd_args
        return subprocess.CompletedProcess(cmd_args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    watcher._run_sync([sys.executable, "-m", "bifrost.cli"])

    assert captured["cmd_args"] == [sys.executable, "-m", "bifrost.cli", "save", "sync", "--apply"]


def test_run_sync_single_token_command(monkeypatch) -> None:
    monkeypatch.setattr(watcher, "_last_sync_time", 0.0)
    captured: dict[str, Any] = {}

    def fake_run(cmd_args, **kwargs):  # noqa: ANN001
        captured["cmd_args"] = cmd_args
        return subprocess.CompletedProcess(cmd_args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    watcher._run_sync(["bifrost"])

    assert captured["cmd_args"] == ["bifrost", "save", "sync", "--apply"]


def test_run_sync_respects_cooldown(monkeypatch) -> None:
    monkeypatch.setattr(watcher, "_last_sync_time", watcher.time.monotonic())
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))  # noqa: ARG005

    watcher._run_sync(["bifrost"])

    assert calls == []
