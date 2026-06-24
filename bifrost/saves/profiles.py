"""Declarative registry of (platform, emulator) save profiles.

Each SaveProfile describes where an emulator stores its save files and how they
should be interpreted. Profiles with supported=False are discovered but not
synced — a warning is logged so users know the profile is recognised but
not yet handled.

Supported mappings:
  per_rom_basename  — one save file per ROM, named after the ROM basename
  per_rom_dir       — one subdirectory per ROM (e.g. PPSSPP)
  shared_memcard    — a single memory card file shared across games (PSX, PS2, GC)
  custom            — emulator-specific logic not covered by the above

EmuDeck layout convention: <saves_root>/<emulator>/saves/<files>.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SaveProfile:
    platform: str
    emulator: str
    save_subpath: str
    include_globs: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    mapping: Literal["per_rom_basename", "per_rom_dir", "shared_memcard", "custom"]
    screenshot_sidecar: bool
    romm_emulator: str | None
    supported: bool = True
    # When True, strips a trailing _N slot suffix from the filename stem before ROM name
    # matching (e.g. "Game_1.mcd" → match against "Game").  The full filename including
    # the suffix is still sent to RomM as-is.
    strip_slot_suffix: bool = False


PROFILES: tuple[SaveProfile, ...] = (
    # RetroArch — "Save folder per core" OFF (EmuDeck default).
    # All cores share retroarch/saves/; files named <rom_basename>.<ext>.
    # Per-core-ON users should add emulator-specific profiles with the
    # core's subdirectory as save_subpath (e.g. "retroarch/saves/SwanStation").
    SaveProfile(
        platform="multi",
        emulator="retroarch",
        save_subpath="retroarch/saves",
        include_globs=("*.srm", "*.sav", "*.sra", "*.eep", "*.fla", "*.nv", "*.hi", "*.mem", "*.mcr", "*.gme"),  # noqa: E501
        exclude_globs=("*.png", "*.state*"),
        mapping="per_rom_basename",
        screenshot_sidecar=False,
        romm_emulator="retroarch",
    ),
    # mGBA standalone (GBA)
    SaveProfile(
        platform="gba",
        emulator="mgba",
        save_subpath="mgba/saves",
        include_globs=("*.sav",),
        exclude_globs=("*.png", "*.ss*"),
        mapping="per_rom_basename",
        screenshot_sidecar=False,
        romm_emulator="mgba",
    ),
    # melonDS (NDS)
    SaveProfile(
        platform="nds",
        emulator="melonds",
        save_subpath="melonds/saves",
        include_globs=("*.sav", "*.dsv"),
        exclude_globs=("*.png",),
        mapping="per_rom_basename",
        screenshot_sidecar=False,
        romm_emulator="melonds",
    ),
    # DuckStation (PSX standalone) — per-game memory cards (EmuDeck default).
    # Filename pattern: <game>_<slot>.mcd (e.g. "Monkey Hero_1.mcd").
    # strip_slot_suffix strips the trailing _N before ROM name matching so
    # "Monkey Hero_1" → looks up "Monkey Hero" in the ROM index.
    SaveProfile(
        platform="psx",
        emulator="duckstation",
        save_subpath="duckstation/saves",
        include_globs=("*.mcd",),
        exclude_globs=(),
        mapping="per_rom_basename",
        screenshot_sidecar=False,
        romm_emulator="duckstation",
        supported=True,
        strip_slot_suffix=True,
    ),
    # PCSX2 (PS2) — shared memory card
    SaveProfile(
        platform="ps2",
        emulator="pcsx2",
        save_subpath="pcsx2/saves",
        include_globs=("*.ps2",),
        exclude_globs=(),
        mapping="shared_memcard",
        screenshot_sidecar=False,
        romm_emulator="pcsx2",
        supported=False,
    ),
    # Dolphin (GC/Wii) — mixed/shared memory card
    SaveProfile(
        platform="gc",
        emulator="dolphin",
        save_subpath="dolphin/saves",
        include_globs=("*.raw", "*.gci"),
        exclude_globs=(),
        mapping="shared_memcard",
        screenshot_sidecar=False,
        romm_emulator="dolphin",
        supported=False,
    ),
)
