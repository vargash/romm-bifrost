"""Decide which entries ES-DE sees for a ROM: its file, its folder or the game files inside.

RomM promotes a ROM to a folder ("multi-file game") when extras (mods, DLC,
manuals...) are added next to it, or when it is uploaded as one. ES-DE only
launches files on most systems, so for platforms in ``inner_file_platforms``
bifrost links the game file(s) at the top level of the folder and leaves the
extras subfolders alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bifrost.api.models import RomSummary
from bifrost.config import AppConfig
from bifrost.multidisc import extract_disc_number

# Single-image systems whose folder ROMs are launched through the image inside.
DEFAULT_INNER_FILE_PLATFORMS: frozenset[str] = frozenset({"ps2", "gc", "wii", "psp", "dreamcast"})

# ES-DE systems that only list folder ROMs carrying a specific extension
# (e.g. ps3 accepts "Game.ps3" but ignores "Game").
_FOLDER_ROM_EXTENSION: dict[str, str] = {"ps3": ".ps3"}


def esde_rom_name(slug: str | None, fs_name: str, is_folder: bool) -> str:
    """Name under which ES-DE must see a ROM: folder ROMs get the system's folder extension."""
    ext = _FOLDER_ROM_EXTENSION.get(slug or "")
    if ext and is_folder and not fs_name.lower().endswith((ext, ext + "dir")):
        return fs_name + ext
    return fs_name


def rom_target_path(roms_root: Path, path_value: str, fs_name: str | None) -> Path:
    """Absolute NAS path of a ROM (file or folder) from the path RomM reports for it."""
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0] == "roms":
        candidate = Path(*candidate.parts[1:])
    if fs_name and candidate.name != fs_name:
        candidate = candidate / fs_name
    return roms_root / candidate


@dataclass(frozen=True)
class LaunchEntry:
    """One symlink in the ES-DE platform directory."""

    name: str
    target: Path


def inner_file_platforms(config: AppConfig) -> frozenset[str]:
    return DEFAULT_INNER_FILE_PLATFORMS | frozenset(config.esde.inner_file_platforms)


def _top_level_files(folder: Path) -> list[str] | None:
    """Names of the regular, non-hidden files directly inside folder; None if unreadable."""
    try:
        with os.scandir(folder) as entries:
            return sorted(
                e.name for e in entries if not e.name.startswith(".") and e.is_file()
            )
    except OSError:
        return None


def launch_entries(
    slug: str,
    fs_name: str,
    fs_extension: str | None,
    target: Path,
    inner_platforms: frozenset[str],
) -> list[LaunchEntry] | None:
    """Entries to expose for one ROM; None when the folder can't be read (skip this round).

    A folder ROM on an inner-file platform exposes its top-level game file, or one
    entry per disc when every file carries a disc marker. Anything else (no files,
    cue/bin sets, unlisted platforms) keeps the folder link.
    """
    is_folder = not fs_extension
    as_is = [LaunchEntry(esde_rom_name(slug, fs_name, is_folder), target)]
    if not is_folder or slug not in inner_platforms:
        return as_is

    files = _top_level_files(target)
    if files is None:
        return None
    if len(files) == 1:
        return [LaunchEntry(files[0], target / files[0])]
    if len(files) > 1 and all(extract_disc_number(f) is not None for f in files):
        discs = sorted(files, key=lambda f: (extract_disc_number(f) or 0, f))
        return [LaunchEntry(f, target / f) for f in discs]
    return as_is


@dataclass(frozen=True)
class InnerFolderScan:
    """Folder ROMs on inner-file platforms, as seen on the NAS (raw RomM ROM dicts)."""

    launch_names: dict[int, str] = field(default_factory=dict)  # ROM id → its single link name
    disc_links: dict[int, list[str]] = field(default_factory=dict)  # ROM id → disc link names
    handled_ids: frozenset[int] = frozenset()  # every folder ROM resolved by file scan


def scan_inner_folders(
    config: AppConfig,
    roms_raw: list[dict[str, Any]],
    platform_slug_by_id: dict[int, str],
) -> InnerFolderScan:
    """Resolve the links of every folder ROM on an inner-file platform without API calls."""
    roms_root = Path(config.nas.library_path).expanduser() / config.nas.roms_subpath
    inner_platforms = inner_file_platforms(config)
    launch_names: dict[int, str] = {}
    disc_links: dict[int, list[str]] = {}
    handled: set[int] = set()
    for rom in roms_raw:
        rom_id = rom.get("id")
        platform_id = rom.get("platform_id")
        fs_name = rom.get("fs_name")
        path_hint = rom.get("full_path") or rom.get("fs_path")
        if (
            not isinstance(rom_id, int)
            or not isinstance(platform_id, int)
            or not fs_name
            or not path_hint
            or rom.get("fs_extension") != ""
        ):
            continue
        slug = platform_slug_by_id.get(platform_id)
        if slug not in inner_platforms:
            continue
        folder = rom_target_path(roms_root, path_hint, fs_name)
        entries = launch_entries(slug, fs_name, "", folder, inner_platforms)
        if entries is None:
            continue
        handled.add(rom_id)
        if len(entries) > 1:
            disc_links[rom_id] = [e.name for e in entries]
        elif entries[0].target != folder:
            launch_names[rom_id] = entries[0].name
    return InnerFolderScan(launch_names, disc_links, frozenset(handled))


_SCAN_FIELDS = {"id", "platform_id", "fs_name", "fs_extension", "full_path", "fs_path"}


def with_game_files(
    config: AppConfig,
    client: Any,
    roms: list[RomSummary],
) -> list[RomSummary]:
    """Copies of roms where folder ROMs carry the names of their game files.

    Saves and states are named after the file the emulator launched, which for a
    folder ROM is not the folder name. Platforms are only fetched when the library
    has folder ROMs at all.
    """
    if not any(rom.fs_name and rom.fs_extension == "" for rom in roms):
        return roms
    platform_slug_by_id = {p.id: p.fs_slug for p in client.list_platforms() if p.fs_slug}
    scan = scan_inner_folders(
        config, [rom.model_dump(include=_SCAN_FIELDS) for rom in roms], platform_slug_by_id
    )
    names = {**{rom_id: [name] for rom_id, name in scan.launch_names.items()}, **scan.disc_links}
    return [
        rom.model_copy(update={"game_files": names[rom.id]}) if rom.id in names else rom
        for rom in roms
    ]
