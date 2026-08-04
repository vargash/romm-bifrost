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
from bifrost.saves.profiles import find_compatible_profile

CONFIG_DIR_NAME = "bifrost"
CONFIG_FILE_NAME = "config.toml"


class RommConfig(BaseModel):
    """RomM API connectivity settings."""

    url: str = Field(description="Base URL of the RomM instance, e.g. http://192.168.1.x:8080.")
    client_token: str = Field(
        min_length=5, description="RomM API client token (starts with 'rmm_')."
    )
    device_id: str = Field(
        default="", description="This device's RomM device id, set by 'bifrost setup'."
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, description="HTTP request timeout in seconds for RomM API calls."
    )
    legacy_upload_fallback: bool = Field(
        default=False,
        description="Use the legacy single-shot upload endpoint instead of chunked uploads.",
    )


class NasConfig(BaseModel):
    """NAS path configuration."""

    library_path: str = Field(
        default="/path/to/romm/library", description="Path to RomM's library root on the NAS."
    )
    resources_path: str = Field(
        default="/path/to/romm/resources", description="Path to RomM's resources root on the NAS."
    )
    roms_subpath: str = Field(
        default="roms", description="Subdirectory under library_path holding ROMs."
    )
    bios_subpath: str = Field(
        default="bios", description="Subdirectory under library_path holding BIOS files."
    )


class EsdeConfig(BaseModel):
    """ES-DE directories."""

    roms_path: str = Field(default="~/Emulation/roms", description="Local ES-DE ROMs directory.")
    gamelists_path: str = Field(
        default="~/ES-DE/gamelists", description="Local ES-DE gamelists directory."
    )
    custom_systems_path: str = Field(
        default="~/ES-DE/custom_systems", description="Local ES-DE custom_systems directory."
    )


class EmudeckConfig(BaseModel):
    """EmuDeck directories."""

    bios_path: str = Field(default="~/Emulation/bios", description="Local EmuDeck BIOS directory.")
    media_path: str = Field(
        default="~/Emulation/tools/downloaded_media",
        description="Local EmuDeck downloaded-media directory.",
    )
    saves_path: str = Field(
        default="~/Emulation/saves", description="Local EmuDeck saves directory Bifrost scans."
    )


class AssetsConfig(BaseModel):
    """Asset folder mapping RomM per-game asset type -> ES-DE media folder.

    Keys are the subdirectory names under resources/roms/<platform_id>/<rom_id>/.
    Values are the corresponding subdirectory names under downloaded_media/<platform>/.
    """

    folder_map: dict[str, str] = Field(
        description="RomM per-game asset type -> ES-DE media subfolder name.",
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

    enabled: list[str] = Field(
        default_factory=list,
        description="Emulator ids to scan for saves. Empty = all supported profiles.",
    )


class CoreMapping(BaseModel):
    """A user-declared save-format compatibility mapping between a foreign
    "emulator"/core tag and one of this device's local save profiles.

    Curated Bifrost-vetted pairs live in bifrost.saves.profiles
    (SaveProfile.compatible_remote_cores) but still require an entry here to
    be applied — Bifrost never routes a save across cores without explicit
    per-device opt-in. A mapping whose (remote_core, local_emulator) pair is
    NOT also present in the target profile's curated compatible_remote_cores
    is still applied, but flagged unverified in warnings/logs: matching
    size/extension is necessary but not sufficient for true byte-compatibility.
    """

    platform: str = Field(description="Platform this mapping applies to, e.g. 'psx'.")
    remote_core: str = Field(
        description="Foreign emulator/core tag as reported by RomM clients, e.g. 'mednafen_psx_hw'."
    )
    local_emulator: str = Field(
        description="Local profile (SaveProfile.romm_emulator) to route matching saves to."
    )
    expected_size_bytes: int | None = Field(
        default=None,
        description=(
            "If set, downloaded saves are truncated to this many bytes "
            "(strips a foreign trailer)."
        ),
    )


class SyncConfig(BaseModel):
    """Save-sync defaults."""

    save_sync_enabled: bool = Field(default=True, description="Whether save sync runs at all.")
    conflict_strategy: str = Field(
        default="ask",
        description="How to resolve conflicting saves: ask | local_wins | server_wins.",
    )
    direction: Literal["push_pull", "push_only", "pull_only"] = Field(
        default="push_pull", description="Sync direction: push_pull | push_only | pull_only."
    )
    # Stable slot name for saves with no explicit numbered slot. Must match the
    # slot naming used by other RomM clients so saves stay paired on
    # (rom_id, slot) across devices.
    slot: str = Field(
        default="autosave",
        description="Stable slot name for saves with no explicit numbered slot.",
    )
    parallel_workers: int = Field(
        default=16, ge=1, description="Worker threads for parallel save sync operations."
    )
    profiles: SyncProfilesConfig = Field(default_factory=SyncProfilesConfig)
    optimistic_downloads: bool = Field(
        default=True,
        description="Skip re-downloading a save already matching the server's checksum.",
    )
    autocleanup: bool = Field(
        default=False, description="Opt in to automatically pruning old .bak backup files."
    )
    autocleanup_limit: int = Field(
        default=3,
        ge=1,
        description="Number of .bak backups to keep per save when autocleanup is on.",
    )
    prune_orphan_platforms: bool = Field(
        default=False,
        description=(
            "Opt in to reviewing/removing orphan platform folders with no matching RomM platform."
        ),
    )
    orphan_platform_strategy: str = Field(
        default="ask", description="Orphan platform folder removal strategy: ask | remove | skip."
    )
    # User-declared and/or migrated-from-curated foreign-core -> local-profile
    # mappings this device applies during save sync. See
    # bifrost.saves.profiles.SaveProfile.compatible_remote_cores for the
    # Bifrost-curated, vetted pairs a mapping here can (but need not) match.
    # Manage via 'bifrost config add-core-mapping' / 'remove-core-mapping'
    # rather than editing this list directly.
    core_mappings: list[CoreMapping] = Field(
        default_factory=list,
        description=(
            "User-declared cross-core save mappings — "
            "see 'bifrost config list-core-mappings'."
        ),
    )


class OutputConfig(BaseModel):
    """CLI output settings."""

    format: str = Field(default="table", description="CLI output format.")
    verbose: bool = Field(default=False, description="Enable verbose logging output.")
    log_file: str = Field(
        default="", description="Override the default log file path. Empty = default location."
    )


class CacheConfig(BaseModel):
    """Disk cache settings for RomM API responses."""

    enabled: bool = Field(default=True, description="Enable the on-disk RomM API response cache.")
    ttl_roms_hours: int = Field(default=6, description="Cache TTL in hours for ROM listings.")
    ttl_platforms_hours: int = Field(
        default=24, description="Cache TTL in hours for platform listings."
    )
    ttl_firmware_hours: int = Field(
        default=24, description="Cache TTL in hours for firmware listings."
    )
    cache_dir: str = Field(
        default="", description="Override the cache directory. Empty = ~/.cache/bifrost."
    )


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


def _migrate_cross_core_compat(data: dict[str, Any]) -> None:
    """Migrate legacy sync.cross_core_compat (bare opted-in foreign-core tags)
    to sync.core_mappings (explicit platform/remote_core/local_emulator rows),
    in-place.

    Each tag is resolved against the curated compatible_remote_cores table
    (via find_compatible_profile) to recover the platform/local_emulator/
    expected_size_bytes it implied. A tag with no curated match (e.g. stale
    or mistyped) is dropped — there is nothing to migrate it to.
    """
    sync = data.get("sync")
    if not isinstance(sync, dict):
        return
    old_tags = sync.pop("cross_core_compat", None)
    if not old_tags or "core_mappings" in sync:
        return
    migrated: list[dict[str, Any]] = []
    for tag in old_tags:
        found = find_compatible_profile(tag)
        if found is None:
            continue
        profile, alias = found
        migrated.append(
            {
                "platform": profile.platform,
                "remote_core": alias.core_slug,
                "local_emulator": profile.romm_emulator,
                "expected_size_bytes": alias.expected_size_bytes,
            }
        )
    if migrated:
        sync["core_mappings"] = migrated


def _parse_config(data: dict[str, Any]) -> AppConfig:
    if "romm" in data and "url" in data["romm"]:
        data["romm"]["url"] = _normalize_url(str(data["romm"]["url"]))
    _migrate_folder_map(data)
    _migrate_sync_mode(data)
    _migrate_cross_core_compat(data)
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

    # exclude_none: TOML has no null literal. CoreMapping.expected_size_bytes is the
    # only nullable field in AppConfig; omitting it when unset round-trips fine since
    # pydantic already defaults a missing field to None on load.
    serialized = tomli_w.dumps(config.model_dump(mode="python", exclude_none=True))
    config_path.write_text(serialized, encoding="utf-8")

    if os.name == "posix":
        config_path.chmod(0o600)

    return config_path
