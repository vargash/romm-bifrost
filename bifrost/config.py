"""Configuration management for Bifrost."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field, ValidationError

from bifrost.errors import ConfigError, ConfigPermissionError

CONFIG_DIR_NAME = "bifrost"
CONFIG_FILE_NAME = "config.toml"


class RommConfig(BaseModel):
    """RomM API connectivity settings."""

    url: str
    client_token: str = Field(min_length=5)
    device_id: str = ""
    timeout_seconds: float = Field(default=10.0, ge=1.0)
    legacy_upload_fallback: bool = False


class NasConfig(BaseModel):
    """NAS path configuration."""

    library_path: str = "/path/to/romm/library"
    resources_path: str = "/path/to/romm/resources"
    roms_subpath: str = "roms"
    bios_subpath: str = "bios"


class EsdeConfig(BaseModel):
    """ES-DE directories."""

    roms_path: str = "~/ROMs"
    gamelists_path: str = "~/.emulationstation/gamelists"
    custom_systems_path: str = "~/.emulationstation/custom_systems"


class EmudeckConfig(BaseModel):
    """EmuDeck directories."""

    bios_path: str = "~/BIOS"
    media_path: str = "/Emulation/tools/downloaded_media"
    saves_path: str = "~/saves"


class AssetsConfig(BaseModel):
    """Asset folder mapping RomM per-game asset type -> ES-DE media folder.

    Keys are the subdirectory names under resources/roms/<platform_id>/<rom_id>/.
    Values are the corresponding subdirectory names under downloaded_media/<platform>/.
    """

    folder_map: dict[str, str] = Field(
        default_factory=lambda: {
            # RomM per-game asset type → ES-DE media subfolder
            "cover": "covers",
            "fanart": "fanart",
            "box3d": "3dboxes",
            "box2d_back": "backcovers",
            "logo": "marquees",        # ES-DE marquees = game logos, not arcade marquees
            "miximage": "miximages",
            "title_screen": "titlescreens",
            "video_normalized": "videos",
            "manual": "manuals",
            "physical": "physicalmedia",
            "screenshots": "screenshots",  # links to first screenshot (0.png)
            "bezel": "bezels",         # non-standard ES-DE path, used by some themes
        }
    )


class SyncProfilesConfig(BaseModel):
    """Per-emulator profile gating for save sync.

    enabled: list of emulator ids to scan (e.g. ["retroarch", "mgba"]).
    Empty list (default) means all supported profiles are active.
    """

    enabled: list[str] = Field(default_factory=list)


class SyncConfig(BaseModel):
    """Save-sync defaults."""

    conflict_strategy: str = "ask"
    direction: Literal["push_pull", "push_only", "pull_only"] = "push_pull"
    parallel_workers: int = Field(default=16, ge=1)
    profiles: SyncProfilesConfig = Field(default_factory=SyncProfilesConfig)


class OutputConfig(BaseModel):
    """CLI output settings."""

    format: str = "table"
    verbose: bool = False
    log_file: str = ""


class CacheConfig(BaseModel):
    """Disk cache settings for RomM API responses."""

    enabled: bool = True
    ttl_roms_hours: int = 6
    ttl_platforms_hours: int = 24
    ttl_firmware_hours: int = 24
    cache_dir: str = ""


class AppConfig(BaseModel):
    """Full Bifrost configuration."""

    romm: RommConfig
    nas: NasConfig = Field(default_factory=NasConfig)
    esde: EsdeConfig = Field(default_factory=EsdeConfig)
    emudeck: EmudeckConfig = Field(default_factory=EmudeckConfig)
    assets: AssetsConfig = Field(default_factory=AssetsConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)


def default_config_path() -> Path:
    """Return default config path following XDG base directory conventions."""

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return base / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _ensure_secure_permissions(path: Path) -> None:
    """Require config file permissions to be user-only on POSIX systems."""

    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigPermissionError(
            f"Unsafe permissions on {path}: {oct(mode)}. Expected 0o600 or stricter."
        )


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


# Keys used in the old flat RomM asset structure → new per-game asset type names.
_FOLDER_MAP_LEGACY: dict[str, str] = {
    "backcovers": "box2d_back",
    "bezels": "bezel",
    "boxes": "box3d",
    "covers": "cover",
    "manuals": "manual",
    "marquees": "logo",      # ES-DE marquees = RomM logo, not arcade marquee
    "miximages": "miximage",
    "titlescreens": "title_screen",
    "videos": "video_normalized",
}


def _migrate_folder_map(data: dict[str, Any]) -> None:
    """Rename legacy folder_map keys to their per-game equivalents, in-place."""
    fm = data.get("assets", {}).get("folder_map")
    if not isinstance(fm, dict):
        return
    for old_key, new_key in _FOLDER_MAP_LEGACY.items():
        if old_key in fm:
            fm.setdefault(new_key, fm.pop(old_key))


def _migrate_sync_mode(data: dict[str, Any]) -> None:
    """Migrate legacy sync.sync_mode → sync.direction, in-place.

    sync_mode was previously used both as the RomM device registration field and
    as the internal sync direction ("push_pull" / "push_only" / "pull_only").
    The registration field is now always "api"; sync.direction carries the internal meaning.
    """
    sync = data.get("sync")
    if not isinstance(sync, dict):
        return
    old_value = sync.pop("sync_mode", None)
    if old_value and "direction" not in sync:
        valid = {"push_pull", "push_only", "pull_only"}
        sync["direction"] = old_value if old_value in valid else "push_pull"


def _parse_config(data: dict[str, Any]) -> AppConfig:
    if "romm" in data and "url" in data["romm"]:
        data["romm"]["url"] = _normalize_url(str(data["romm"]["url"]))
    _migrate_folder_map(data)
    _migrate_sync_mode(data)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate configuration from TOML file."""

    config_path = path or default_config_path()
    if not config_path.exists():
        raise ConfigError(
            f"Configuration file not found: {config_path}. Run 'bifrost setup' first."
        )

    _ensure_secure_permissions(config_path)

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    config = _parse_config(data)
    if not config.romm.client_token.startswith("rmm_"):
        raise ConfigError("romm.client_token must start with 'rmm_'.")
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Persist configuration to disk with safe file permissions."""

    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    serialized = tomli_w.dumps(config.model_dump(mode="python"))
    config_path.write_text(serialized, encoding="utf-8")

    if os.name == "posix":
        config_path.chmod(0o600)

    return config_path
