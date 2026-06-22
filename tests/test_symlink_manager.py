from pathlib import Path

from bifrost.api.models import PlatformSummary, RomSummary, SsMetadata
from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.symlink_manager import (
    SymlinkOperation,
    _asset_relative_path,
    _resource_relative_path,
    apply_operation,
    evaluate_operation,
    plan_symlink_operations,
)


def _make_rom(**kwargs) -> RomSummary:
    defaults = dict(
        id=10,
        name="Super Mario Bros",
        fs_name="Super Mario Bros.nes",
        fs_path="roms/nes",
        full_path="roms/nes/Super Mario Bros.nes",
        platform_id=1,
        path_cover_large="/assets/romm/resources/roms/1/10/cover/big.png",
        merged_screenshots=["/assets/romm/resources/roms/1/10/screenshots/0.jpg"],
        has_manual=True,
        path_manual="roms/1/10/manual/10.pdf",
        ss_metadata=SsMetadata(
            box3d_path="roms/1/10/box3d/box3d.png",
            fanart_path="roms/1/10/fanart/fanart.png",
            video_normalized_path="roms/1/10/video_normalized/video-normalized.mp4",
        ),
    )
    defaults.update(kwargs)
    return RomSummary(**defaults)


class StubClient:
    def list_platforms(self):
        return [PlatformSummary(id=1, name="NES", fs_slug="nes")]

    def list_roms(self):
        return [_make_rom()]

    def list_firmware(self, platform_id=None):
        return [{"id": 55, "file_name": "scph1001.bin", "file_path": "bios/psx"}]


class StubClientWithPrefixedPaths(StubClient):
    pass


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://localhost", client_token="rmm_token"),
        nas=NasConfig(
            library_path=str(tmp_path / "nas" / "romm" / "library"),
            resources_path=str(tmp_path / "nas" / "romm" / "resources"),
            roms_subpath="roms",
            bios_subpath="bios",
        ),
        esde=EsdeConfig(roms_path=str(tmp_path / "esde" / "roms")),
        emudeck=EmudeckConfig(
            bios_path=str(tmp_path / "emudeck" / "Emulation" / "bios"),
            media_path=str(tmp_path / "emudeck" / "Emulation" / "tools" / "downloaded_media"),
        ),
        assets=AssetsConfig(folder_map={"cover": "covers", "box3d": "3dboxes", "screenshots": "screenshots"}),
    )


# ---------------------------------------------------------------------------
# _resource_relative_path
# ---------------------------------------------------------------------------


def test_resource_relative_path_bare_relative():
    assert _resource_relative_path("roms/1/10/box3d/box3d.png") == "roms/1/10/box3d/box3d.png"


def test_resource_relative_path_url_style():
    raw = "/assets/romm/resources/roms/11/1047/cover/big.png?ts=2026-06-22 12:50:00"
    assert _resource_relative_path(raw) == "roms/11/1047/cover/big.png"


def test_resource_relative_path_none():
    assert _resource_relative_path(None) is None


def test_resource_relative_path_screenshot_jpg():
    raw = "/assets/romm/resources/roms/11/1047/screenshots/0.jpg"
    assert _resource_relative_path(raw) == "roms/11/1047/screenshots/0.jpg"


# ---------------------------------------------------------------------------
# _asset_relative_path
# ---------------------------------------------------------------------------


def test_asset_relative_path_cover():
    rom = _make_rom(path_cover_large="/assets/romm/resources/roms/1/10/cover/big.png")
    assert _asset_relative_path(rom, "cover") == "roms/1/10/cover/big.png"


def test_asset_relative_path_cover_absent():
    rom = _make_rom(path_cover_large=None)
    assert _asset_relative_path(rom, "cover") is None


def test_asset_relative_path_screenshot_jpg():
    rom = _make_rom(merged_screenshots=["/assets/romm/resources/roms/1/10/screenshots/0.jpg"])
    assert _asset_relative_path(rom, "screenshots") == "roms/1/10/screenshots/0.jpg"


def test_asset_relative_path_screenshot_absent():
    rom = _make_rom(merged_screenshots=[])
    assert _asset_relative_path(rom, "screenshots") is None


def test_asset_relative_path_manual_present():
    rom = _make_rom(has_manual=True, path_manual="roms/1/10/manual/10.pdf")
    assert _asset_relative_path(rom, "manual") == "roms/1/10/manual/10.pdf"


def test_asset_relative_path_manual_absent():
    rom = _make_rom(has_manual=False, path_manual=None)
    assert _asset_relative_path(rom, "manual") is None


def test_asset_relative_path_ss_metadata_field():
    rom = _make_rom(ss_metadata=SsMetadata(box3d_path="roms/1/10/box3d/box3d.png"))
    assert _asset_relative_path(rom, "box3d") == "roms/1/10/box3d/box3d.png"


def test_asset_relative_path_ss_metadata_field_absent():
    rom = _make_rom(ss_metadata=SsMetadata(box3d_path=None))
    assert _asset_relative_path(rom, "box3d") is None


def test_asset_relative_path_no_ss_metadata():
    rom = _make_rom(ss_metadata=None)
    assert _asset_relative_path(rom, "box3d") is None


def test_asset_relative_path_unknown_type():
    rom = _make_rom()
    assert _asset_relative_path(rom, "nonexistent_type") is None


# ---------------------------------------------------------------------------
# plan_symlink_operations
# ---------------------------------------------------------------------------


def test_plan_symlink_operations_builds_rom_bios_and_asset_entries(tmp_path: Path):
    config = make_config(tmp_path)
    ops = plan_symlink_operations(config, StubClient())

    assert any(op.category == "rom" and op.destination.name == "Super Mario Bros.nes" for op in ops)
    assert any(op.category == "bios" and op.destination.name == "scph1001.bin" for op in ops)

    # cover asset: path from path_cover_large → big.png → .png extension
    cover_op = next(
        (op for op in ops if op.category == "asset" and op.destination.parent.name == "covers"),
        None,
    )
    assert cover_op is not None
    assert cover_op.destination.name == "Super Mario Bros.png"
    assert cover_op.target.name == "big.png"
    assert cover_op.target.parts[-2] == "cover"

    # box3d asset: path from ss_metadata.box3d_path
    box3d_op = next(
        (op for op in ops if op.category == "asset" and op.destination.parent.name == "3dboxes"),
        None,
    )
    assert box3d_op is not None
    assert box3d_op.destination.name == "Super Mario Bros.png"
    assert box3d_op.target.name == "box3d.png"

    # screenshots asset: .jpg extension picked up from merged_screenshots
    ss_op = next(
        (op for op in ops if op.category == "asset" and op.destination.parent.name == "screenshots"),
        None,
    )
    assert ss_op is not None
    assert ss_op.destination.suffix == ".jpg"
    assert ss_op.target.name == "0.jpg"


def test_plan_symlink_operations_skips_absent_assets(tmp_path: Path):
    """ROMs with no cover path produce no cover symlink op."""
    config = make_config(tmp_path)
    rom_no_cover = _make_rom(path_cover_large=None, merged_screenshots=[], ss_metadata=None)

    class StubNoAssets(StubClient):
        def list_roms(self):
            return [rom_no_cover]

    ops = plan_symlink_operations(config, StubNoAssets())
    assert not any(op.category == "asset" for op in ops)


def test_plan_symlink_operations_normalizes_prefixed_roms_and_bios_paths(tmp_path: Path):
    config = make_config(tmp_path)
    ops = plan_symlink_operations(config, StubClientWithPrefixedPaths())

    rom_op = next(op for op in ops if op.category == "rom")
    bios_op = next(op for op in ops if op.category == "bios")

    assert "/roms/roms/" not in str(rom_op.target)
    assert "/bios/bios/" not in str(bios_op.target)


# ---------------------------------------------------------------------------
# evaluate_operation / apply_operation
# ---------------------------------------------------------------------------


def test_evaluate_operation_detects_existing_correct_symlink(tmp_path: Path):
    target = tmp_path / "target.bin"
    target.write_text("x")
    destination = tmp_path / "dest.bin"
    destination.symlink_to(target)

    result = evaluate_operation(
        SymlinkOperation(category="bios", destination=destination, target=target, is_dir=False)
    )
    assert result.action == "ok"


def test_apply_operation_returns_error_instead_of_raising_for_filesystem_issue(tmp_path: Path):
    blocking_parent = tmp_path / "not_a_directory"
    blocking_parent.write_text("block")
    destination = blocking_parent / "file.bin"
    target = tmp_path / "source.bin"
    target.write_text("x")

    result = apply_operation(
        SymlinkOperation(category="bios", destination=destination, target=target, is_dir=False)
    )
    assert result.action == "error"


def test_apply_operation_replaces_broken_parent_symlink_with_real_dir(tmp_path: Path):
    """If the parent directory is a broken symlink (old asset-dir legacy), replace it with a dir."""
    nas = tmp_path / "nas"
    asset_file = nas / "cover" / "big.png"
    asset_file.parent.mkdir(parents=True)
    asset_file.write_text("img")

    media_root = tmp_path / "media" / "psx"
    media_root.mkdir(parents=True)
    covers_dir = media_root / "covers"
    covers_dir.symlink_to(nas / "old_structure" / "covers")  # broken
    assert covers_dir.is_symlink() and not covers_dir.exists()

    op = SymlinkOperation(
        category="asset", destination=covers_dir / "Final Fantasy VII.png", target=asset_file, is_dir=False
    )
    result = apply_operation(op)

    assert result.action == "create"
    assert (covers_dir / "Final Fantasy VII.png").is_symlink()
    assert covers_dir.is_dir() and not covers_dir.is_symlink()


def test_apply_operation_replaces_valid_parent_symlink_with_real_dir(tmp_path: Path):
    """A VALID parent directory symlink (pointing to existing NAS dir) is also replaced."""
    nas = tmp_path / "nas"
    old_flat = nas / "resources" / "roms" / "257" / "videos"
    old_flat.mkdir(parents=True)
    asset_file = nas / "resources" / "roms" / "11" / "1046" / "video_normalized" / "video-normalized.mp4"
    asset_file.parent.mkdir(parents=True)
    asset_file.write_text("vid")

    media_psx = tmp_path / "media" / "psx"
    media_psx.mkdir(parents=True)
    videos_dir = media_psx / "videos"
    videos_dir.symlink_to(old_flat)
    assert videos_dir.is_symlink() and videos_dir.exists()

    op = SymlinkOperation(
        category="asset", destination=videos_dir / "Final Fantasy VII.mp4", target=asset_file, is_dir=False
    )
    result = apply_operation(op)

    assert result.action == "create"
    assert (videos_dir / "Final Fantasy VII.mp4").is_symlink()
    assert videos_dir.is_dir() and not videos_dir.is_symlink()


def test_evaluate_operation_returns_broken_for_symlink_with_missing_target(tmp_path: Path):
    target = tmp_path / "gone.bin"
    destination = tmp_path / "dest.bin"
    destination.symlink_to(target)

    result = evaluate_operation(
        SymlinkOperation(category="rom", destination=destination, target=target, is_dir=False)
    )
    assert result.action == "broken"
    assert "missing" in result.detail.lower()


def test_apply_operation_returns_broken_without_modifying_symlink(tmp_path: Path):
    target = tmp_path / "gone.bin"
    destination = tmp_path / "dest.bin"
    destination.symlink_to(target)

    op = SymlinkOperation(category="rom", destination=destination, target=target, is_dir=False)
    result = apply_operation(op)

    assert result.action == "broken"
    assert destination.is_symlink()


def test_apply_operation_returns_missing_target_when_nas_file_absent(tmp_path: Path):
    nas_dir = tmp_path / "nas" / "roms" / "psx"
    nas_dir.mkdir(parents=True)
    target = nas_dir / "Xenogears.chd"

    roms_dir = tmp_path / "roms" / "psx"
    roms_dir.mkdir(parents=True)
    destination = roms_dir / "Xenogears.chd"

    op = SymlinkOperation(category="rom", destination=destination, target=target, is_dir=False)
    result = apply_operation(op)

    assert result.action == "missing-target"
    assert not destination.exists() and not destination.is_symlink()


def test_apply_operation_returns_missing_target_when_asset_type_dir_absent(tmp_path: Path):
    """Safety net: if an asset op is somehow created with a missing asset type dir, return missing-target."""
    rom_asset_root = tmp_path / "res" / "roms" / "293" / "3736"
    rom_asset_root.mkdir(parents=True)
    target = rom_asset_root / "manual" / "3736.pdf"

    media_dir = tmp_path / "media" / "cps1" / "manuals"
    media_dir.mkdir(parents=True)
    destination = media_dir / "mbombrd.pdf"

    op = SymlinkOperation(category="asset", destination=destination, target=target, is_dir=False)
    result = apply_operation(op)

    assert result.action == "missing-target"
    assert not destination.is_symlink()


def test_apply_operation_creates_symlink_when_nas_asset_root_absent(tmp_path: Path):
    """If even the ROM asset root doesn't exist (NAS down), create symlink optimistically."""
    target = tmp_path / "res" / "roms" / "293" / "9999" / "cover" / "big.png"

    media_dir = tmp_path / "media" / "cps1" / "covers"
    media_dir.mkdir(parents=True)
    destination = media_dir / "UnknownGame.png"

    op = SymlinkOperation(category="asset", destination=destination, target=target, is_dir=False)
    result = apply_operation(op)

    assert result.action == "create"
    assert destination.is_symlink()
