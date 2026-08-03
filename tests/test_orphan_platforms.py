"""Tests for orphan platform folder detection and removal (bifrost sync --prune-orphans)."""

from __future__ import annotations

from pathlib import Path

from bifrost.api.models import PlatformSummary
from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.symlink_manager import (
    OrphanRemovalOperation,
    apply_orphan_removal,
    evaluate_orphan_removal,
    find_orphan_platform_folders,
    list_orphan_folder_contents,
)


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://localhost", client_token="rmm_token"),
        nas=NasConfig(
            library_path=str(tmp_path / "nas"),
            resources_path=str(tmp_path / "res"),
        ),
        esde=EsdeConfig(roms_path=str(tmp_path / "roms")),
        emudeck=EmudeckConfig(
            bios_path=str(tmp_path / "bios"),
            media_path=str(tmp_path / "media"),
        ),
        assets=AssetsConfig(folder_map={}),
    )


def _platform(fs_slug: str) -> PlatformSummary:
    return PlatformSummary(id=1, fs_slug=fs_slug, name=fs_slug)


# ---------------------------------------------------------------------------
# find_orphan_platform_folders
# ---------------------------------------------------------------------------


def test_finds_unmatched_empty_folder(tmp_path: Path):
    config = _make_config(tmp_path)
    (tmp_path / "roms" / "n64").mkdir(parents=True)

    orphans = find_orphan_platform_folders(config, platforms=[_platform("psx")])

    assert len(orphans) == 1
    assert orphans[0].path == tmp_path / "roms" / "n64"
    assert orphans[0].safe is True
    assert orphans[0].reason == ""


def test_skips_folder_matching_known_platform(tmp_path: Path):
    config = _make_config(tmp_path)
    (tmp_path / "roms" / "psx").mkdir(parents=True)

    orphans = find_orphan_platform_folders(config, platforms=[_platform("psx")])

    assert orphans == []


def test_ignores_top_level_regular_files(tmp_path: Path):
    config = _make_config(tmp_path)
    roms_root = tmp_path / "roms"
    roms_root.mkdir(parents=True)
    (roms_root / "readme.txt").write_text("hello")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert orphans == []


def test_unsafe_when_folder_contains_real_file(tmp_path: Path):
    config = _make_config(tmp_path)
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)
    (n64_dir / "Mario64.z64").write_text("rom data")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is False
    assert "Mario64.z64" in orphans[0].reason


def test_safe_when_folder_contains_only_bifrost_symlinks(tmp_path: Path):
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)
    nas_target = nas_root / "roms" / "n64" / "Mario64.z64"
    nas_target.parent.mkdir(parents=True)
    nas_target.write_text("data")
    (n64_dir / "Mario64.z64").symlink_to(nas_target)

    # Platform "n64" no longer exists in RomM -> still orphan-eligible even though non-empty.
    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is True


def test_unsafe_when_folder_contains_foreign_symlink(tmp_path: Path):
    config = _make_config(tmp_path)
    external_target = tmp_path / "external" / "game.z64"
    external_target.parent.mkdir(parents=True)
    external_target.write_text("data")

    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)
    (n64_dir / "game.z64").symlink_to(external_target)

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is False
    assert "foreign symlink" in orphans[0].reason


def test_top_level_symlink_is_not_a_candidate(tmp_path: Path):
    config = _make_config(tmp_path)
    roms_root = tmp_path / "roms"
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir(parents=True)
    roms_root.mkdir(parents=True)
    (roms_root / "n64").symlink_to(real_dir)

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert orphans == []


def test_no_roms_root_returns_empty(tmp_path: Path):
    config = _make_config(tmp_path)
    orphans = find_orphan_platform_folders(config, platforms=[])
    assert orphans == []


# ---------------------------------------------------------------------------
# evaluate_orphan_removal / apply_orphan_removal
# ---------------------------------------------------------------------------


def test_evaluate_orphan_removal_reports_would_remove(tmp_path: Path):
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)

    op = OrphanRemovalOperation(destination=n64_dir)
    result = evaluate_orphan_removal(op)

    assert result.action == "would-remove"
    assert n64_dir.exists()  # dry-run: nothing removed


def test_apply_orphan_removal_removes_safe_folder(tmp_path: Path):
    nas_root = tmp_path / "nas"
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)
    nas_target = nas_root / "roms" / "n64" / "Mario64.z64"
    nas_target.parent.mkdir(parents=True)
    nas_target.write_text("data")
    (n64_dir / "Mario64.z64").symlink_to(nas_target)

    op = OrphanRemovalOperation(destination=n64_dir)
    result = apply_orphan_removal(op, nas_root)

    assert result.action == "remove"
    assert not n64_dir.exists()
    assert nas_target.exists()  # NAS file itself is never touched


def test_apply_orphan_removal_refuses_folder_with_real_file(tmp_path: Path):
    nas_root = tmp_path / "nas"
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)
    (n64_dir / "Mario64.z64").write_text("rom data")

    op = OrphanRemovalOperation(destination=n64_dir)
    result = apply_orphan_removal(op, nas_root)

    assert result.action == "skip"
    assert n64_dir.exists()


def test_apply_orphan_removal_toctou_reguard(tmp_path: Path):
    """A folder that looked safe at plan time but gained a real file before apply is refused."""
    nas_root = tmp_path / "nas"
    n64_dir = tmp_path / "roms" / "n64"
    n64_dir.mkdir(parents=True)

    op = OrphanRemovalOperation(destination=n64_dir)
    # Simulate a file appearing between planning and apply.
    (n64_dir / "SneakyFile.z64").write_text("data")

    result = apply_orphan_removal(op, nas_root)

    assert result.action == "skip"
    assert n64_dir.exists()


def test_apply_orphan_removal_is_idempotent_when_already_gone(tmp_path: Path):
    nas_root = tmp_path / "nas"
    op = OrphanRemovalOperation(destination=tmp_path / "roms" / "ghost")

    result = apply_orphan_removal(op, nas_root)

    assert result.action == "skip"


# ---------------------------------------------------------------------------
# EmuDeck scaffolding whitelist (systeminfo.txt, metadata.txt)
# ---------------------------------------------------------------------------


def test_folder_with_only_systeminfo_txt_is_safe(tmp_path: Path):
    """EmuDeck stamps every platform folder with systeminfo.txt; alone it must not block pruning."""
    config = _make_config(tmp_path)
    kodi_dir = tmp_path / "roms" / "kodi"
    kodi_dir.mkdir(parents=True)
    (kodi_dir / "systeminfo.txt").write_text("EmuDeck scaffolding")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is True


def test_folder_with_only_metadata_txt_is_safe(tmp_path: Path):
    config = _make_config(tmp_path)
    kodi_dir = tmp_path / "roms" / "kodi"
    kodi_dir.mkdir(parents=True)
    (kodi_dir / "metadata.txt").write_text("EmuDeck scaffolding")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is True


def test_folder_with_systeminfo_and_metadata_txt_is_safe(tmp_path: Path):
    config = _make_config(tmp_path)
    kodi_dir = tmp_path / "roms" / "kodi"
    kodi_dir.mkdir(parents=True)
    (kodi_dir / "systeminfo.txt").write_text("EmuDeck scaffolding")
    (kodi_dir / "metadata.txt").write_text("EmuDeck scaffolding")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is True


def test_folder_with_systeminfo_txt_and_other_file_is_unsafe(tmp_path: Path):
    config = _make_config(tmp_path)
    emulators_dir = tmp_path / "roms" / "emulators"
    emulators_dir.mkdir(parents=True)
    (emulators_dir / "systeminfo.txt").write_text("EmuDeck scaffolding")
    (emulators_dir / "ryujinx.sh").write_text("#!/bin/sh\n")

    orphans = find_orphan_platform_folders(config, platforms=[])

    assert len(orphans) == 1
    assert orphans[0].safe is False
    assert "ryujinx.sh" in orphans[0].reason


def test_apply_orphan_removal_removes_folder_with_only_systeminfo_txt(tmp_path: Path):
    nas_root = tmp_path / "nas"
    kodi_dir = tmp_path / "roms" / "kodi"
    kodi_dir.mkdir(parents=True)
    (kodi_dir / "systeminfo.txt").write_text("EmuDeck scaffolding")

    op = OrphanRemovalOperation(destination=kodi_dir)
    result = apply_orphan_removal(op, nas_root)

    assert result.action == "remove"
    assert not kodi_dir.exists()


# ---------------------------------------------------------------------------
# list_orphan_folder_contents / force override
# ---------------------------------------------------------------------------


def test_list_orphan_folder_contents_includes_systeminfo_txt(tmp_path: Path):
    """Unlike the safety scan, the content listing shown for manual review ignores no files."""
    nas_root = tmp_path / "nas"
    emulators_dir = tmp_path / "roms" / "emulators"
    emulators_dir.mkdir(parents=True)
    (emulators_dir / "systeminfo.txt").write_text("data")
    (emulators_dir / "ryujinx.sh").write_text("#!/bin/sh\n")

    contents = list_orphan_folder_contents(emulators_dir, nas_root)

    assert set(contents) == {"systeminfo.txt", "ryujinx.sh"}


def test_apply_orphan_removal_refuses_unsafe_folder_without_force(tmp_path: Path):
    nas_root = tmp_path / "nas"
    emulators_dir = tmp_path / "roms" / "emulators"
    emulators_dir.mkdir(parents=True)
    (emulators_dir / "ryujinx.sh").write_text("#!/bin/sh\n")

    op = OrphanRemovalOperation(destination=emulators_dir)
    result = apply_orphan_removal(op, nas_root)

    assert result.action == "skip"
    assert emulators_dir.exists()


def test_apply_orphan_removal_with_force_removes_unsafe_folder(tmp_path: Path):
    nas_root = tmp_path / "nas"
    emulators_dir = tmp_path / "roms" / "emulators"
    emulators_dir.mkdir(parents=True)
    (emulators_dir / "ryujinx.sh").write_text("#!/bin/sh\n")

    op = OrphanRemovalOperation(destination=emulators_dir, force=True)
    result = apply_orphan_removal(op, nas_root)

    assert result.action == "remove"
    assert not emulators_dir.exists()


def test_apply_orphan_removal_with_force_still_requires_real_directory(tmp_path: Path):
    nas_root = tmp_path / "nas"
    op = OrphanRemovalOperation(destination=tmp_path / "roms" / "ghost", force=True)

    result = apply_orphan_removal(op, nas_root)

    assert result.action == "skip"
