from __future__ import annotations

import subprocess
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from bifrost.cli import EXIT_OK, main
from bifrost.locking import SaveSyncLockError


def _stub_ctl(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


class _CtlRecorder:
    """Stub for subprocess.run that dispatches systemctl calls per-unit.

    `active_units` marks which units `is-active` reports as "active"; all
    others report "inactive". Every call is recorded verbatim (argv list)
    for assertions.
    """

    def __init__(self, active_units: Iterable[str] = ()) -> None:
        self.active_units = set(active_units)
        self.calls: list[list[str]] = []

    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = list(args[0]) if args and isinstance(args[0], list) else list(args)
        self.calls.append(argv)
        if len(argv) >= 3 and argv[0] == "systemctl" and argv[2] == "is-active":
            unit = argv[3]
            stdout = "active" if unit in self.active_units else "inactive"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def calls_matching(self, *tokens: str) -> list[list[str]]:
        return [c for c in self.calls if all(t in c for t in tokens)]


@contextmanager
def _fake_lock_free(*_args: object, **_kwargs: object):
    yield


@contextmanager
def _fake_lock_held(*_args: object, **_kwargs: object):
    raise SaveSyncLockError("locked")
    yield  # pragma: no cover - unreachable, keeps this a generator


def test_systemd_install_resolves_missing_bifrost_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bifrost not on PATH: units must not keep the hardcoded %h/.local/bin/bifrost."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("subprocess.run", _stub_ctl)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output

    unit_file = tmp_path / "systemd" / "user" / "bifrost-save-watch.service"
    content = unit_file.read_text()
    assert "%h/.local/bin/bifrost" not in content
    assert "bifrost.cli save watch" in content


def test_systemd_install_bifrost_bin_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("subprocess.run", _stub_ctl)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "systemd",
            "install",
            "--config",
            str(tmp_path / "missing-config.toml"),
            "--bifrost-bin",
            "/opt/bifrost/bin/bifrost",
        ],
    )

    assert result.exit_code == EXIT_OK, result.output

    unit_file = tmp_path / "systemd" / "user" / "bifrost-sync.service"
    content = unit_file.read_text()
    assert "ExecStart=/opt/bifrost/bin/bifrost sync --apply" in content
    assert "ExecStart=/opt/bifrost/bin/bifrost gamelist --apply" in content


def test_systemd_install_restarts_active_service_when_lock_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units={"bifrost-save-watch.service"})
    monkeypatch.setattr("subprocess.run", recorder)
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_free)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "restarted" in result.output
    assert recorder.calls_matching("restart", "bifrost-save-watch.service")


def test_systemd_install_skips_restart_when_lock_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units={"bifrost-save-watch.service"})
    monkeypatch.setattr("subprocess.run", recorder)
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_held)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "skipped restart" in result.output
    assert "restart it manually" in result.output
    assert "bifrost-save-watch.service" in result.output
    assert not recorder.calls_matching("restart", "bifrost-save-watch.service")
    assert not recorder.calls_matching("enable", "bifrost-save-watch.service")


def test_systemd_install_starts_inactive_service_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units=set())
    monkeypatch.setattr("subprocess.run", recorder)
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_free)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "enabled + started" in result.output
    assert recorder.calls_matching("enable", "--now", "bifrost-save-watch.service")
    assert not recorder.calls_matching("restart", "bifrost-save-watch.service")


def test_systemd_install_timers_always_enable_now_regardless_of_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units={"bifrost-save-watch.service"})
    monkeypatch.setattr("subprocess.run", recorder)
    # Lock held is only meaningful for the persistent service — timers must
    # be unaffected either way.
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_held)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert recorder.calls_matching("enable", "--now", "bifrost-sync.timer")
    assert recorder.calls_matching("enable", "--now", "bifrost-save-sync.timer")
    assert not recorder.calls_matching("restart", "bifrost-sync.timer")
    assert not recorder.calls_matching("restart", "bifrost-save-sync.timer")


def test_systemd_install_dry_run_reports_restart_vs_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units={"bifrost-save-watch.service"})
    monkeypatch.setattr("subprocess.run", recorder)
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_free)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--dry-run", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "would restart" in result.output
    assert "picking up updated code" in result.output
    # Dry-run must never mutate: only read-only is-active probes allowed.
    assert not recorder.calls_matching("enable")
    assert not recorder.calls_matching("restart")
    assert not recorder.calls_matching("daemon-reload")


def test_systemd_install_dry_run_reports_skip_when_lock_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    recorder = _CtlRecorder(active_units={"bifrost-save-watch.service"})
    monkeypatch.setattr("subprocess.run", recorder)
    monkeypatch.setattr("bifrost.cli.save_sync_lock", _fake_lock_held)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["systemd", "install", "--dry-run", "--config", str(tmp_path / "missing-config.toml")],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "would skip restart" in result.output
    assert "save sync in progress" in result.output
