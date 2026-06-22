from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bifrost.cli import EXIT_CONFIG_ERROR, EXIT_OK, main
from bifrost.config import AppConfig, RommConfig, load_config, save_config


def test_config_show_prints_current_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["config", "show", "--config", str(config_path)])

    assert result.exit_code == EXIT_OK
    assert "romm.url" in result.output
    assert "http://romm.local" in result.output
    assert "romm.client_token" in result.output


def test_config_set_updates_single_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "config",
            "set",
            "romm.url",
            "http://new.local/",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_path)
    assert cfg.romm.url == "http://new.local"


def test_config_set_rejects_unknown_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["config", "set", "romm.unknown", "value", "--config", str(config_path)],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
