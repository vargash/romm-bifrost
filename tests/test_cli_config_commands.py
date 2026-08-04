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


def test_config_set_parses_comma_separated_list_value(tmp_path: Path) -> None:
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
            "sync.profiles.enabled",
            "retroarch, mgba",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    cfg = load_config(config_path)
    assert cfg.sync.profiles.enabled == ["retroarch", "mgba"]


def test_config_set_empty_string_clears_list_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    base = AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token"))
    base.sync.profiles.enabled = ["retroarch"]
    save_config(base, config_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["config", "set", "sync.profiles.enabled", "", "--config", str(config_path)],
    )

    assert result.exit_code == EXIT_OK, result.output
    cfg = load_config(config_path)
    assert cfg.sync.profiles.enabled == []


def test_config_set_core_mappings_rejects_with_helpful_message(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["config", "set", "sync.core_mappings", "foo", "--config", str(config_path)],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "add-core-mapping" in result.output


def test_config_add_core_mapping_curated_autofill(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "mednafen_psx_hw",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Verified mapping" in result.output
    cfg = load_config(config_path)
    assert len(cfg.sync.core_mappings) == 1
    mapping = cfg.sync.core_mappings[0]
    assert mapping.platform == "psx"
    assert mapping.remote_core == "mednafen_psx_hw"
    assert mapping.local_emulator == "duckstation"
    assert mapping.expected_size_bytes == 131072


def test_config_add_core_mapping_custom_requires_explicit_fields(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "totally_unknown_core",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "--local-emulator" in result.output


def test_config_add_core_mapping_rejects_unsupported_local_emulator(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "some_ps2_core",
            "--local-emulator",
            "pcsx2",
            "--platform",
            "ps2",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "unsupported" in result.output
    cfg = load_config(config_path)
    assert cfg.sync.core_mappings == []


def test_config_add_core_mapping_rejects_platform_mismatch(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "some_core",
            "--local-emulator",
            "duckstation",
            "--platform",
            "ps2",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "platform mismatch" in result.output


def test_config_add_core_mapping_unverified_requires_yes(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "some_other_psx_core",
            "--local-emulator",
            "duckstation",
            "--platform",
            "psx",
            "--config",
            str(config_path),
        ],
        input="n\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Aborted" in result.output
    cfg = load_config(config_path)
    assert cfg.sync.core_mappings == []


def test_config_add_core_mapping_unverified_with_yes_saves(tmp_path: Path) -> None:
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
            "add-core-mapping",
            "--remote-core",
            "some_other_psx_core",
            "--local-emulator",
            "duckstation",
            "--platform",
            "psx",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "not verified" in result.output
    cfg = load_config(config_path)
    assert len(cfg.sync.core_mappings) == 1
    assert cfg.sync.core_mappings[0].remote_core == "some_other_psx_core"


def test_config_remove_core_mapping_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )
    runner = CliRunner()
    runner.invoke(
        main,
        [
            "config",
            "add-core-mapping",
            "--remote-core",
            "mednafen_psx_hw",
            "--config",
            str(config_path),
        ],
    )

    result = runner.invoke(
        main,
        [
            "config",
            "remove-core-mapping",
            "--remote-core",
            "mednafen_psx_hw",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    cfg = load_config(config_path)
    assert cfg.sync.core_mappings == []


def test_config_list_core_mappings_shows_curated_and_configured(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )
    runner = CliRunner()
    runner.invoke(
        main,
        [
            "config",
            "add-core-mapping",
            "--remote-core",
            "mednafen_psx_hw",
            "--config",
            str(config_path),
        ],
    )

    result = runner.invoke(
        main, ["config", "list-core-mappings", "--config", str(config_path)]
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "built-in" in result.output
    assert "configured" in result.output
    assert "mednafen_ps" in result.output


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
