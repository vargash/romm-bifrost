"""Tests for bifrost.saves.profiles and bifrost.saves.layout."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bifrost.saves.layout import EmudeckEsdeLayout
from bifrost.saves.profiles import PROFILES, SaveProfile

# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------


def test_profiles_registry_has_supported_profiles() -> None:
    supported = [p for p in PROFILES if p.supported]
    assert len(supported) >= 1, "at least one supported profile required"


def test_profiles_registry_has_unsupported_shared_memcard() -> None:
    unsupported = [p for p in PROFILES if not p.supported]
    assert len(unsupported) >= 1
    mappings = {p.mapping for p in unsupported}
    assert "shared_memcard" in mappings


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_has_non_empty_include_globs(profile: SaveProfile) -> None:
    assert len(profile.include_globs) > 0, f"{profile.emulator}: include_globs is empty"


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_has_unique_save_subpath(profile: SaveProfile) -> None:
    subpaths = [p.save_subpath for p in PROFILES]
    assert subpaths.count(profile.save_subpath) == 1, (
        f"duplicate save_subpath {profile.save_subpath!r}"
    )


def test_adding_a_profile_requires_single_registry_entry() -> None:
    """Adding a profile = one entry in PROFILES."""
    emulators = [p.emulator for p in PROFILES]
    # Each emulator id appears at most once in the flat registry
    assert len(emulators) == len(set(emulators)), "duplicate emulator ids in PROFILES"


# ---------------------------------------------------------------------------
# EmudeckEsdeLayout — scan_saves
# ---------------------------------------------------------------------------


def _make_saves(saves_root: Path, subpath: str, filenames: list[str]) -> Path:
    profile_dir = saves_root / subpath
    profile_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (profile_dir / name).write_bytes(b"save-data")
    return profile_dir


def test_scan_finds_retroarch_srm_files(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm", "Zelda.srm"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert names == {"Mario.srm", "Zelda.srm"}
    assert all(sf.profile.emulator == "retroarch" for sf in results)


def test_scan_finds_mgba_sav_files(tmp_path: Path) -> None:
    _make_saves(tmp_path, "mgba/saves", ["Metroid.sav"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert "Metroid.sav" in names
    metroid = next(sf for sf in results if sf.path.name == "Metroid.sav")
    assert metroid.profile.emulator == "mgba"


def test_scan_excludes_state_files_from_retroarch(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm", "Mario.state", "Mario.state1"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert "Mario.srm" in names
    assert "Mario.state" not in names
    assert "Mario.state1" not in names


def test_scan_excludes_png_sidecar_files(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm", "Mario.png"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert "Mario.png" not in names


def test_scan_does_not_recurse_into_subdirectories(tmp_path: Path) -> None:
    """per-core subdirs under retroarch/saves are not scanned by the flat profile."""
    flat_dir = tmp_path / "retroarch/saves"
    flat_dir.mkdir(parents=True)
    (flat_dir / "Mario.srm").write_bytes(b"data")
    per_core_dir = flat_dir / "SwanStation"
    per_core_dir.mkdir()
    (per_core_dir / "MonkeyHero.srm").write_bytes(b"data")

    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert "Mario.srm" in names
    assert "MonkeyHero.srm" not in names


def test_unsupported_profile_dir_logged_as_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _make_saves(tmp_path, "pcsx2/saves", ["memcard.ps2"])
    layout = EmudeckEsdeLayout()
    with caplog.at_level(logging.WARNING, logger="bifrost.saves.layout"):
        results = layout.scan_saves(tmp_path)
    # Unsupported profile produces no ScannedFile entries
    assert not any(sf.profile.emulator == "pcsx2" for sf in results)
    assert any("pcsx2" in record.message for record in caplog.records)


def test_scan_empty_saves_root_returns_no_results(tmp_path: Path) -> None:
    layout = EmudeckEsdeLayout()
    assert layout.scan_saves(tmp_path) == []


def test_scan_missing_profile_dir_skipped_silently(tmp_path: Path) -> None:
    # mgba dir does not exist — no error, no results for mgba
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    assert not any(sf.profile.emulator == "mgba" for sf in results)


def test_scan_enabled_emulators_filters_profiles(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm"])
    _make_saves(tmp_path, "mgba/saves", ["Metroid.sav"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path, enabled_emulators=["mgba"])
    emulators = {sf.profile.emulator for sf in results}
    assert emulators == {"mgba"}


def test_scanned_file_profile_romm_emulator_set(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", ["Mario.srm"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    assert results[0].profile.romm_emulator == "retroarch"


def test_duckstation_profile_strip_slot_suffix_flag(tmp_path: Path) -> None:
    """DuckStation profile has strip_slot_suffix=True and scans .mcd files."""
    _make_saves(tmp_path, "duckstation/saves", ["Monkey Hero_1.mcd", "Monkey Hero_2.mcd"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert names == {"Monkey Hero_1.mcd", "Monkey Hero_2.mcd"}
    duck = next(sf for sf in results if sf.profile.emulator == "duckstation")
    assert duck.profile.strip_slot_suffix is True
    assert duck.profile.mapping == "per_rom_basename"


def test_scan_hidden_files_excluded(tmp_path: Path) -> None:
    _make_saves(tmp_path, "retroarch/saves", [".hidden.srm", "Mario.srm"])
    layout = EmudeckEsdeLayout()
    results = layout.scan_saves(tmp_path)
    names = {sf.path.name for sf in results}
    assert ".hidden.srm" not in names
    assert "Mario.srm" in names
