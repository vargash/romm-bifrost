from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bifrost.cli import EXIT_OK, main


def _stub_ctl(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


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
