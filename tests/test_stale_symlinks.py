"""Tests for stale symlink detection and removal (bifrost sync cleanup)."""

from __future__ import annotations

from pathlib import Path

from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.symlink_manager import (
    RemoveSymlinkOperation,
    SymlinkOperation,
    apply_remove_operation,
    evaluate_remove_operation,
    plan_stale_removals,
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


def _rom_op(roms_root: Path, slug: str, filename: str, nas_root: Path) -> SymlinkOperation:
    return SymlinkOperation(
        category="rom",
        destination=roms_root / slug / filename,
        target=nas_root / "roms" / slug / filename,
        is_dir=False,
    )


# ---------------------------------------------------------------------------
# evaluate_remove_operation / apply_remove_operation
# ---------------------------------------------------------------------------


def test_evaluate_remove_returns_remove_for_symlink(tmp_path: Path):
    link = tmp_path / "game.nes"
    target = tmp_path / "target.nes"
    target.write_text("rom")
    link.symlink_to(target)

    op = RemoveSymlinkOperation(category="rom", destination=link)
    result = evaluate_remove_operation(op)
    assert result.action == "remove"


def test_evaluate_remove_returns_skip_for_regular_file(tmp_path: Path):
    f = tmp_path / "notasymlink.nes"
    f.write_text("real file")

    op = RemoveSymlinkOperation(category="rom", destination=f)
    result = evaluate_remove_operation(op)
    assert result.action == "skip"


def test_evaluate_remove_returns_skip_for_missing_path(tmp_path: Path):
    op = RemoveSymlinkOperation(category="rom", destination=tmp_path / "ghost.nes")
    result = evaluate_remove_operation(op)
    assert result.action == "skip"


def test_apply_remove_deletes_symlink(tmp_path: Path):
    link = tmp_path / "game.nes"
    target = tmp_path / "target.nes"
    target.write_text("rom")
    link.symlink_to(target)

    op = RemoveSymlinkOperation(category="rom", destination=link)
    result = apply_remove_operation(op)
    assert result.action == "remove"
    assert not link.exists()
    assert not link.is_symlink()


def test_apply_remove_deletes_broken_symlink(tmp_path: Path):
    link = tmp_path / "gone.nes"
    link.symlink_to(tmp_path / "nonexistent.nes")
    assert link.is_symlink()

    op = RemoveSymlinkOperation(category="rom", destination=link)
    result = apply_remove_operation(op)
    assert result.action == "remove"
    assert not link.is_symlink()


def test_apply_remove_is_idempotent_when_already_gone(tmp_path: Path):
    op = RemoveSymlinkOperation(category="rom", destination=tmp_path / "gone.nes")
    result = apply_remove_operation(op)
    assert result.action == "skip"


# ---------------------------------------------------------------------------
# plan_stale_removals
# ---------------------------------------------------------------------------


def test_plan_stale_removals_finds_broken_symlink(tmp_path: Path):
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)

    # Active symlink in the plan
    active = psx_dir / "Crash.chd"
    active.symlink_to(nas_root / "roms" / "psx" / "Crash.chd")

    # Broken symlink NOT in the plan (target deleted)
    stale = psx_dir / "OldGame.chd"
    stale.symlink_to(nas_root / "roms" / "psx" / "OldGame.chd")
    assert stale.is_symlink() and not stale.exists()

    ops = [_rom_op(roms_root, "psx", "Crash.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert len(remove_ops) == 1
    assert remove_ops[0].destination == stale
    assert remove_ops[0].category == "rom"


def test_plan_stale_removals_finds_nas_symlink_not_in_plan(tmp_path: Path):
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)
    nas_file = nas_root / "roms" / "psx" / "RemovedFromRomm.chd"
    nas_file.parent.mkdir(parents=True)
    nas_file.write_text("data")

    # Symlink to a NAS file that still exists on disk but is no longer in the plan
    stale = psx_dir / "RemovedFromRomm.chd"
    stale.symlink_to(nas_file)

    ops = [_rom_op(roms_root, "psx", "Crash.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert any(r.destination == stale for r in remove_ops)


def test_plan_stale_removals_ignores_non_nas_symlinks(tmp_path: Path):
    """A symlink pointing outside the NAS root is not bifrost-managed and must not be touched."""
    config = _make_config(tmp_path)
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)

    external_target = tmp_path / "external" / "game.chd"
    external_target.parent.mkdir(parents=True)
    external_target.write_text("data")

    user_link = psx_dir / "UserGame.chd"
    user_link.symlink_to(external_target)

    ops: list[SymlinkOperation] = []
    remove_ops = plan_stale_removals(config, ops)

    # psx has no planned ops → slug not in managed_slugs → dir not scanned
    assert len(remove_ops) == 0


def test_plan_stale_removals_does_not_remove_planned_symlinks(tmp_path: Path):
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)

    for name in ("Crash.chd", "FF7.chd"):
        link = psx_dir / name
        link.symlink_to(nas_root / "roms" / "psx" / name)

    ops = [
        _rom_op(roms_root, "psx", "Crash.chd", nas_root),
        _rom_op(roms_root, "psx", "FF7.chd", nas_root),
    ]
    remove_ops = plan_stale_removals(config, ops)
    assert remove_ops == []


def test_plan_stale_removals_ignores_regular_files(tmp_path: Path):
    """Regular files (like .m3u) in the ROM dir must not be removed."""
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)

    # A regular .m3u file (not a symlink)
    m3u = psx_dir / "FF7.m3u"
    m3u.write_text("FF7 (Disc 1).bin\nFF7 (Disc 2).bin\n")

    ops = [_rom_op(roms_root, "psx", "Crash.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert all(r.destination != m3u for r in remove_ops)


def test_plan_stale_removals_scans_bios_dir(tmp_path: Path):
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    bios_root = tmp_path / "bios"
    bios_root.mkdir(parents=True)
    (roms_root / "psx").mkdir(parents=True)

    # Stale BIOS symlink (broken)
    stale_bios = bios_root / "old_bios.bin"
    stale_bios.symlink_to(nas_root / "bios" / "old_bios.bin")

    ops = [_rom_op(roms_root, "psx", "Crash.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert any(r.destination == stale_bios and r.category == "bios" for r in remove_ops)


def test_plan_stale_removals_detects_broken_planned_symlink_when_nas_accessible(tmp_path: Path):
    """A broken symlink that IS in the plan is removed when the NAS root is reachable."""
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)
    # Make the NAS root accessible (but not the specific ROM file).
    nas_root.mkdir(parents=True)

    # Planned symlink whose NAS target was deleted.
    broken = psx_dir / "Xenogears.chd"
    broken.symlink_to(nas_root / "roms" / "psx" / "Xenogears.chd")
    assert broken.is_symlink() and not broken.exists()

    ops = [_rom_op(roms_root, "psx", "Xenogears.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert any(r.destination == broken for r in remove_ops)


def test_plan_stale_removals_ignores_broken_planned_symlink_when_nas_down(tmp_path: Path):
    """When the NAS root itself is unreachable, in-plan broken symlinks are NOT flagged."""
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"  # intentionally not created → NAS "down"
    roms_root = tmp_path / "roms"
    psx_dir = roms_root / "psx"
    psx_dir.mkdir(parents=True)

    broken = psx_dir / "Xenogears.chd"
    broken.symlink_to(nas_root / "roms" / "psx" / "Xenogears.chd")
    assert broken.is_symlink() and not broken.exists()

    ops = [_rom_op(roms_root, "psx", "Xenogears.chd", nas_root)]
    remove_ops = plan_stale_removals(config, ops)

    assert not any(r.destination == broken for r in remove_ops)


def test_remove_operation_target_property_returns_symlink_target(tmp_path: Path):
    target = tmp_path / "actual_target.chd"
    target.write_text("data")
    link = tmp_path / "game.chd"
    link.symlink_to(target)

    op = RemoveSymlinkOperation(category="rom", destination=link)
    assert op.target == target


def test_remove_operation_target_property_on_broken_symlink(tmp_path: Path):
    link = tmp_path / "ghost.chd"
    link.symlink_to(tmp_path / "nonexistent.chd")

    op = RemoveSymlinkOperation(category="rom", destination=link)
    # Should not raise; returns a resolved path
    _ = op.target


def test_plan_stale_removals_removes_legacy_asset_dir_symlinks(tmp_path: Path):
    """Old asset-dir directory symlinks under downloaded_media are flagged for removal."""
    config = _make_config(tmp_path)
    nas_root = tmp_path / "nas"
    nas_root.mkdir(parents=True)

    # Simulate old asset-dir broken symlink: media/psx/covers → NAS (doesn't exist)
    media_psx = tmp_path / "media" / "psx"
    media_psx.mkdir(parents=True)
    old_covers = media_psx / "covers"
    old_covers.symlink_to(nas_root / "resources" / "roms" / "11" / "covers")
    assert old_covers.is_symlink() and not old_covers.exists()

    # No ops — simulates empty sync plan (no asset ops)
    remove_ops = plan_stale_removals(config, ops=[])

    assert any(r.destination == old_covers for r in remove_ops)
