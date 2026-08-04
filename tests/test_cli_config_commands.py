from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bifrost.cli import EXIT_CONFIG_ERROR, EXIT_OK, main
from bifrost.config import AppConfig, RommConfig, load_config, save_config


def test_config_migrate_drops_obsolete_key_and_fills_new_default(tmp_path: Path) -> None:
    """A config predating sync.core_mappings (still on legacy
    sync.cross_core_compat) round-trips through 'config migrate' into the
    current schema on disk: the legacy key is gone, the migrated
    core_mappings entry and any new field defaults are written out.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"

[sync]
cross_core_compat = ["mednafen_psx_hw"]
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    runner = CliRunner()
    result = runner.invoke(main, ["config", "migrate", "--config", str(config_path)])

    assert result.exit_code == EXIT_OK, result.output
    raw = config_path.read_text(encoding="utf-8")
    assert "cross_core_compat" not in raw
    assert "core_mappings" in raw
    assert "save_sync_enabled" in raw  # a field this legacy file never had

    cfg = load_config(config_path)
    assert cfg.sync.core_mappings[0].remote_core == "mednafen_psx_hw"


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


def test_config_add_core_mapping_interactive_curated(tmp_path: Path) -> None:
    """Running with no flags at all walks the user through platform -> source
    core -> target, suggesting the curated target as the default answer."""
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    # platform, remote core, accept suggested target (blank -> curated default)
    result = runner.invoke(
        main,
        ["config", "add-core-mapping", "--config", str(config_path)],
        input="psx\nmednafen_psx_hw\n\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Bifrost knows this core" in result.output
    assert "Verified mapping" in result.output
    cfg = load_config(config_path)
    assert len(cfg.sync.core_mappings) == 1
    mapping = cfg.sync.core_mappings[0]
    assert mapping.local_emulator == "duckstation"
    assert mapping.expected_size_bytes == 131072


def test_config_add_core_mapping_interactive_custom(tmp_path: Path) -> None:
    """No curated match: prompts for target explicitly, then asks for the
    optional expected-size-bytes, then confirms before saving the unverified
    mapping."""
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    # platform, remote core, explicit target, blank size, confirm yes
    result = runner.invoke(
        main,
        ["config", "add-core-mapping", "--config", str(config_path)],
        input="psx\npcsxrearmed\nduckstation\n\ny\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Unknown core" in result.output
    cfg = load_config(config_path)
    assert len(cfg.sync.core_mappings) == 1
    mapping = cfg.sync.core_mappings[0]
    assert mapping.remote_core == "pcsxrearmed"
    assert mapping.local_emulator == "duckstation"
    assert mapping.expected_size_bytes is None


def test_config_add_core_mapping_interactive_requires_target(tmp_path: Path) -> None:
    """Blank target with no curated default is a hard error, not a silent no-op."""
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token")),
        config_path,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["config", "add-core-mapping", "--config", str(config_path)],
        input="psx\npcsxrearmed\n\n",
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "local emulator is required" in result.output
    cfg = load_config(config_path)
    assert cfg.sync.core_mappings == []


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
    assert "Unknown core" in result.output
    assert "no data on" in result.output
    cfg = load_config(config_path)
    assert len(cfg.sync.core_mappings) == 1
    assert cfg.sync.core_mappings[0].remote_core == "some_other_psx_core"


def test_config_add_core_mapping_warns_mismatch_when_core_curated_elsewhere(
    tmp_path: Path,
) -> None:
    """mednafen_psx_hw is curated for duckstation. Linking it to retroarch instead
    (a different, if platform-wildcard-compatible, local profile) should warn about
    the specific known target rather than a generic "unknown core" message.
    """
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
            "--local-emulator",
            "retroarch",
            "--platform",
            "psx",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Likely mismatch" in result.output
    assert "duckstation" in result.output
    cfg = load_config(config_path)
    assert cfg.sync.core_mappings[0].local_emulator == "retroarch"


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


def test_config_list_core_mappings_flags_mismatched_target(tmp_path: Path) -> None:
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
            "--local-emulator",
            "retroarch",
            "--platform",
            "psx",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    result = runner.invoke(
        main, ["config", "list-core-mappings", "--config", str(config_path)]
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "mismatch" in result.output
    assert "duckstation" in result.output


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
