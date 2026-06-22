from pathlib import Path

from bifrost.api.models import PlatformSummary, RomSummary
from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.symlink_manager import (
    SymlinkOperation,
    apply_operation,
    evaluate_operation,
    plan_symlink_operations,
)


class StubClient:
    def list_platforms(self):
        return [
            PlatformSummary(id=1, name="NES", slug="nes", fs_slug="nes", rom_count=2),
        ]

    def list_roms(self):
        return [
            RomSummary(
                id=10,
                name="Super Mario Bros",
                fs_name="Super Mario Bros.nes",
                fs_path="roms/nes",
                full_path="roms/nes/Super Mario Bros.nes",
                platform_id=1,
                platform_slug="nes",
                missing_from_fs=False,
                is_unidentified=False,
            )
        ]

    def list_firmware(self, platform_id=None):
        return [
            {
                "id": 55,
                "file_name": "scph1001.bin",
                "file_path": "bios/psx",
            }
        ]


class StubClientWithPrefixedPaths(StubClient):
    def list_roms(self):
        return [
            RomSummary(
                id=10,
                name="Super Mario Bros",
                fs_name="Super Mario Bros.nes",
                fs_path="roms/nes",
                full_path="roms/nes/Super Mario Bros.nes",
                platform_id=1,
                platform_slug="nes",
                missing_from_fs=False,
                is_unidentified=False,
            )
        ]

    def list_firmware(self, platform_id=None):
        return [
            {
                "id": 55,
                "file_name": "scph1001.bin",
                "file_path": "bios/psx",
            }
        ]


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
        assets=AssetsConfig(folder_map={"box_front": "covers"}),
    )


def test_plan_symlink_operations_builds_rom_bios_and_asset_entries(tmp_path: Path):
    config = make_config(tmp_path)
    ops = plan_symlink_operations(config, StubClient())

    assert any(op.category == "rom" and op.destination.name == "Super Mario Bros.nes" for op in ops)
    assert any(op.category == "bios" and op.destination.name == "scph1001.bin" for op in ops)

    # Asset symlinks are now per-ROM files: <media>/<platform>/<esde_folder>/<rom_stem>.<ext>
    # folder_map={"box_front": "covers"} → preferred file is box_front.png (no special case)
    asset_op = next(
        (op for op in ops if op.category == "asset" and op.destination.parent.name == "covers"),
        None,
    )
    assert asset_op is not None
    assert asset_op.destination.name == "Super Mario Bros.png"
    assert asset_op.target.name == "box_front.png"       # default: <asset_type>.png
    assert asset_op.target.parent.name == "box_front"    # romm asset type subdir
    # full target: resources/roms/<platform_id>/<rom_id>/box_front/box_front.png
    assert asset_op.target.parts[-4:-1] == ("1", "10", "box_front")

    rom_op = next(op for op in ops if op.category == "rom")
    bios_op = next(op for op in ops if op.category == "bios")
    assert rom_op.target.name == "Super Mario Bros.nes"
    assert bios_op.target.name == "scph1001.bin"


def test_evaluate_operation_detects_existing_correct_symlink(tmp_path: Path):
    target = tmp_path / "target.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")

    destination = tmp_path / "dest.bin"
    destination.symlink_to(target)

    result = evaluate_operation(
        SymlinkOperation(
            category="bios",
            destination=destination,
            target=target,
            is_dir=False,
        )
    )

    assert result.action == "ok"


def test_plan_symlink_operations_normalizes_prefixed_roms_and_bios_paths(tmp_path: Path):
    config = make_config(tmp_path)
    ops = plan_symlink_operations(config, StubClientWithPrefixedPaths())

    rom_op = next(op for op in ops if op.category == "rom")
    bios_op = next(op for op in ops if op.category == "bios")

    assert "/roms/roms/" not in str(rom_op.target)
    assert "/bios/bios/" not in str(bios_op.target)


def test_apply_operation_returns_error_instead_of_raising_for_filesystem_issue(tmp_path: Path):
    blocking_parent = tmp_path / "not_a_directory"
    blocking_parent.write_text("block")
    destination = blocking_parent / "file.bin"
    target = tmp_path / "source.bin"
    target.write_text("x")

    result = apply_operation(
        SymlinkOperation(
            category="bios",
            destination=destination,
            target=target,
            is_dir=False,
        )
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
    # Simulate old asset-dir: broken symlink where the real directory should be.
    covers_dir.symlink_to(nas / "old_structure" / "covers")  # broken — target doesn't exist
    assert covers_dir.is_symlink() and not covers_dir.exists()

    op = SymlinkOperation(
        category="asset",
        destination=covers_dir / "Final Fantasy VII.png",
        target=asset_file,
        is_dir=False,
    )
    result = apply_operation(op)

    assert result.action == "create"
    assert (covers_dir / "Final Fantasy VII.png").is_symlink()
    assert covers_dir.is_dir() and not covers_dir.is_symlink()


def test_evaluate_operation_returns_broken_for_symlink_with_missing_target(tmp_path: Path):
    target = tmp_path / "gone.bin"  # never created
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
    assert destination.is_symlink()  # symlink untouched


def test_apply_operation_returns_missing_target_when_nas_file_absent(tmp_path: Path):
    nas_dir = tmp_path / "nas" / "roms" / "psx"
    nas_dir.mkdir(parents=True)
    target = nas_dir / "Xenogears.chd"  # directory exists but file does not

    roms_dir = tmp_path / "roms" / "psx"
    roms_dir.mkdir(parents=True)
    destination = roms_dir / "Xenogears.chd"

    op = SymlinkOperation(category="rom", destination=destination, target=target, is_dir=False)
    result = apply_operation(op)

    assert result.action == "missing-target"
    assert not destination.exists() and not destination.is_symlink()
