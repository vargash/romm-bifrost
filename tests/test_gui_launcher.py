from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from bifrost.cli import main
from bifrost.config import AppConfig, EsdeConfig, RommConfig
from bifrost.gui import launcher


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://x", client_token="rmm_1"),
        esde=EsdeConfig(roms_path=str(tmp_path / "roms")),
    )


def test_default_directory_is_the_emulators_system(tmp_path: Path) -> None:
    assert launcher.default_directory(_config(tmp_path)) == tmp_path / "roms" / "emulators"


def test_install_writes_an_executable_script_that_opens_the_gui_fullscreen(tmp_path: Path) -> None:
    path = launcher.install(tmp_path / "emulators", ["/opt/my tools/bifrost"])

    assert path == tmp_path / "emulators" / "Bifrost.sh"
    assert os.access(path, os.X_OK)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/sh"
    assert shlex.split(lines[-1]) == ["exec", "/opt/my tools/bifrost", "gui", "--fullscreen"]
    assert launcher.is_managed(path)


def test_install_is_repeatable_but_never_overwrites_a_foreign_file(tmp_path: Path) -> None:
    launcher.install(tmp_path, ["bifrost"])
    launcher.install(tmp_path, ["other"])  # our own file: replaced

    foreign = tmp_path / "Bifrost.sh"
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    with pytest.raises(launcher.LauncherError):
        launcher.install(tmp_path, ["bifrost"])
    with pytest.raises(launcher.LauncherError):
        launcher.remove(tmp_path)
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_remove_deletes_only_a_managed_launcher(tmp_path: Path) -> None:
    assert launcher.remove(tmp_path) is None
    launcher.install(tmp_path, ["bifrost"])

    assert launcher.remove(tmp_path) == tmp_path / "Bifrost.sh"
    assert not (tmp_path / "Bifrost.sh").exists()


def test_fallback_command_uses_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

    command = launcher.bifrost_command()

    assert command[1:] == ["-m", "bifrost"]


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[romm]\nurl = "http://x"\nclient_token = "rmm_1"\n'
        f'[esde]\nroms_path = "{tmp_path / "roms"}"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_cli_installs_and_removes_the_launcher(tmp_path: Path) -> None:
    cfg = _config_file(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["gui", "install-launcher", "--config", str(cfg), "--bifrost-bin", "/x/bifrost"])

    assert result.exit_code == 0, result.output
    script = tmp_path / "roms" / "emulators" / "Bifrost.sh"
    assert "/x/bifrost gui --fullscreen" in script.read_text(encoding="utf-8")

    result = runner.invoke(main, ["gui", "uninstall-launcher", "--config", str(cfg)])
    assert result.exit_code == 0 and not script.exists()


def test_cli_refuses_to_overwrite_a_foreign_launcher(tmp_path: Path) -> None:
    cfg = _config_file(tmp_path)
    target = tmp_path / "roms" / "emulators"
    target.mkdir(parents=True)
    (target / "Bifrost.sh").write_text("mine", encoding="utf-8")

    result = CliRunner().invoke(main, ["gui", "install-launcher", "--config", str(cfg)])

    assert result.exit_code != 0
    assert (target / "Bifrost.sh").read_text(encoding="utf-8") == "mine"


def test_gui_without_pygame_explains_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bifrost.gui.app as app

    monkeypatch.setattr(app, "pygame_available", lambda: False)

    result = CliRunner().invoke(main, ["gui", "--config", str(_config_file(tmp_path))])

    assert result.exit_code != 0
    assert "pygame" in result.output
