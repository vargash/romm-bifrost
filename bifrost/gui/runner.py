"""Run a bifrost command in a child process and stream its output line by line."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def cli_argv(args: tuple[str, ...], config_path: Path | None) -> list[str]:
    """Command line running ``bifrost <args>`` with the interpreter that runs the GUI."""
    argv = [sys.executable, "-m", "bifrost.cli", *args]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return argv


class CommandRunner:
    """A running child process whose output is read on a thread and drained with poll().

    Commands run exactly as they do from a shell (same code path, same lock), minus
    colours and prompts: stdin is closed, so a command that would ask a question fails
    instead of waiting forever.
    """

    def __init__(self, argv: list[str]) -> None:
        env = {
            **os.environ,
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "110",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self.returncode: int | None = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            for part in raw.replace("\r", "\n").splitlines():
                self._lines.put(_ANSI.sub("", part).rstrip())
        self._proc.wait()
        self._lines.put(None)

    def poll(self) -> list[str]:
        """Lines produced since the last call; sets returncode once output has ended."""
        lines: list[str] = []
        while True:
            try:
                item = self._lines.get_nowait()
            except queue.Empty:
                return lines
            if item is None:
                self.returncode = self._proc.returncode
                return lines
            lines.append(item)

    def cancel(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
