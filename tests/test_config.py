from __future__ import annotations

from pathlib import Path

import pytest

from bifrost.config import AppConfig, RommConfig, load_config, save_config
from bifrost.errors import ConfigError, ConfigPermissionError


def test_load_config_success(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local:8080/"
client_token = "rmm_123456"
device_id = ""
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    cfg = load_config(config_file)
    assert cfg.romm.url == "http://romm.local:8080"
    assert cfg.romm.client_token.startswith("rmm_")


def test_load_config_invalid_token(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local:8080"
client_token = "invalid"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    with pytest.raises(ConfigError):
        load_config(config_file)


def test_load_config_rejects_unsafe_permissions(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local:8080"
client_token = "rmm_123456"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o644)

    with pytest.raises(ConfigPermissionError):
        load_config(config_file)


def test_load_config_migrates_legacy_folder_map_keys(tmp_path: Path) -> None:
    """Old flat-structure folder_map keys are silently upgraded to per-game names."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"

[assets.folder_map]
boxes = "box3dfront"
covers = "covers"
bezels = "bezels"
fanart = "fanart"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    config = load_config(config_file)
    fm = config.assets.folder_map

    # Legacy keys must be renamed; their values preserved.
    assert "boxes" not in fm
    assert "covers" not in fm
    assert "bezels" not in fm
    assert fm.get("box3d") == "box3dfront"
    assert fm.get("cover") == "covers"
    assert fm.get("bezel") == "bezels"
    # Non-legacy keys survive unchanged.
    assert fm.get("fanart") == "fanart"


def test_load_config_migrates_legacy_sync_mode_to_direction(tmp_path: Path) -> None:
    """Old sync.sync_mode is remapped to sync.direction; sync_mode field removed."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"

[sync]
sync_mode = "push_pull"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    config = load_config(config_file)
    assert config.sync.direction == "push_pull"


def test_load_config_migrates_sync_mode_unknown_value_to_default(tmp_path: Path) -> None:
    """sync.sync_mode with a value not valid for direction defaults to push_pull."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"

[sync]
sync_mode = "api"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    config = load_config(config_file)
    assert config.sync.direction == "push_pull"


def test_save_config_sets_secure_mode(tmp_path: Path) -> None:
    app_config = AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token"))
    config_file = tmp_path / "config.toml"

    save_config(app_config, config_file)

    mode = config_file.stat().st_mode & 0o777
    assert mode == 0o600
