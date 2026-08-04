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
class RemoteCoreAlias:
    """A manually-verified foreign core/emulator tag this profile's save format
    can substitute for.

    core_slug is a foreign "emulator" tag — as reported by RomM clients other
    than this Bifrost install, e.g. a mobile RetroArch app sending its libretro
    core id directly. Save-file format compatibility across cores must be
    verified by hand per alias (extension/framing can differ even when the
    underlying save bytes are the same standard). Never applied automatically:
    only takes effect when the user opts in via a matching sync.core_mappings
    entry in config, since a wrong assumption here can corrupt a save file.
    """

    core_slug: str
    note: str
    # When set, this remote core's saves are expected to be exactly this many
    # raw bytes (e.g. the PS1 standard memory card is 131072 bytes / 128KiB, a
    # fixed hardware spec). Any bytes beyond this size are a foreign
    # wrapper/trailer appended by the uploading client, not part of the save
    # image itself, and are truncated on download so the target emulator
    # doesn't reject the file for having the wrong size.
    expected_size_bytes: int | None = None


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
    # Foreign cores/emulators Bifrost has verified produce save files byte-compatible
    # with this profile's format — see RemoteCoreAlias. Still requires an explicit
    # sync.core_mappings entry in config to take effect on a given device.
    compatible_remote_cores: tuple[RemoteCoreAlias, ...] = ()


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
        compatible_remote_cores=(
            RemoteCoreAlias(
                core_slug="mednafen_psx_hw",
                note="both use the standard 128KB PS1 per-game memory card image",
                expected_size_bytes=131072,
            ),
        ),
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


def find_compatible_profile(core_slug: str | None) -> tuple[SaveProfile, RemoteCoreAlias] | None:
    """Return the (profile, alias) pair whose compatible_remote_cores lists core_slug.

    Curated-only lookup — ignores config entirely. Useful to tell "no
    Bifrost-verified pairing exists for this core" apart from "a pairing
    exists but the user hasn't configured a sync.core_mappings entry for it
    yet" when building advisory messages, and to resolve legacy
    sync.cross_core_compat tags during config migration. See
    resolve_core_mapping for the opt-in, config-aware lookup.
    """
    if not core_slug:
        return None
    for profile in PROFILES:
        for alias in profile.compatible_remote_cores:
            if alias.core_slug == core_slug:
                return profile, alias
    return None


@dataclass(frozen=True)
class CoreMappingResolution:
    """Result of validating a (platform, remote_core, local_emulator) mapping."""

    ok: bool
    target_profile: SaveProfile | None
    note: str
    expected_size_bytes: int | None
    verified: bool
    rejected_reason: str | None = None
    # Set only when verified is False and remote_core is curated (compatible_remote_cores)
    # on a DIFFERENT profile than the requested target — i.e. Bifrost has data on this
    # core, just not for the target the caller asked for. None means remote_core is
    # entirely unknown to Bifrost (no profile lists it at all). Lets callers word the
    # unverified warning differently: a likely target mismatch vs a wholly unverified core.
    known_compatible_with: str | None = None


def resolve_core_mapping(
    *,
    platform: str,
    remote_core: str,
    local_emulator: str,
    expected_size_bytes: int | None = None,
) -> CoreMappingResolution:
    """Validate a user-declared or curated core mapping against PROFILES.

    Checks that local_emulator names an existing, supported local profile and
    that platform is consistent with it (a target profile with
    platform="multi", e.g. RetroArch, accepts a mapping declared for any
    specific platform). If remote_core also appears in the target profile's
    curated compatible_remote_cores, the mapping is "verified" and its note /
    expected_size_bytes default to the curated alias's; otherwise it's applied
    as an unverified custom mapping (matching size/extension alone doesn't
    guarantee true byte-compatibility, so this is surfaced distinctly by
    callers). An unverified result further distinguishes, via
    known_compatible_with, whether remote_core is curated for some OTHER
    profile (likely a mismatched target) or entirely unknown to Bifrost.
    """
    target_profile = next(
        (p for p in PROFILES if p.romm_emulator == local_emulator), None
    )
    if target_profile is None:
        return CoreMappingResolution(
            ok=False,
            target_profile=None,
            note="",
            expected_size_bytes=None,
            verified=False,
            rejected_reason=f"no local profile named {local_emulator!r}",
        )
    if not target_profile.supported:
        return CoreMappingResolution(
            ok=False,
            target_profile=None,
            note="",
            expected_size_bytes=None,
            verified=False,
            rejected_reason=f"{local_emulator!r} is a recognised but unsupported profile",
        )
    if target_profile.platform != "multi" and target_profile.platform != platform:
        return CoreMappingResolution(
            ok=False,
            target_profile=None,
            note="",
            expected_size_bytes=None,
            verified=False,
            rejected_reason=(
                f"platform mismatch: {local_emulator!r} is a "
                f"{target_profile.platform!r} profile, not {platform!r}"
            ),
        )
    alias = next(
        (a for a in target_profile.compatible_remote_cores if a.core_slug == remote_core),
        None,
    )
    if alias is not None:
        resolved_size = (
            expected_size_bytes if expected_size_bytes is not None else alias.expected_size_bytes
        )
        return CoreMappingResolution(
            ok=True,
            target_profile=target_profile,
            note=alias.note,
            expected_size_bytes=resolved_size,
            verified=True,
        )
    elsewhere = find_compatible_profile(remote_core)
    if elsewhere is not None:
        other_profile, other_alias = elsewhere
        return CoreMappingResolution(
            ok=True,
            target_profile=target_profile,
            note=(
                f"{remote_core!r} is Bifrost-verified compatible with "
                f"{other_profile.romm_emulator!r} ({other_alias.note}), not "
                f"{target_profile.romm_emulator!r} — you are linking it to a different "
                "local emulator than the one Bifrost has verified for this core."
            ),
            expected_size_bytes=expected_size_bytes,
            verified=False,
            known_compatible_with=other_profile.romm_emulator,
        )
    return CoreMappingResolution(
        ok=True,
        target_profile=target_profile,
        note=(
            f"Bifrost has no data on {remote_core!r} at all — proceeding purely on "
            "the core slug reported by RomM's API; confirm save formats truly match."
        ),
        expected_size_bytes=expected_size_bytes,
        verified=False,
    )
