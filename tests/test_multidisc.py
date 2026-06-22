"""Tests for bifrost.multidisc: disc detection, M3U planning and applying."""

from __future__ import annotations

from pathlib import Path

from bifrost.api.models import PlatformSummary
from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.multidisc import (
    M3uOperation,
    apply_m3u_operation,
    base_name_for_m3u,
    evaluate_m3u_operation,
    extract_disc_number,
    group_multidisc_roms_raw,
    plan_m3u_operations,
    strip_disc_marker,
)

# ---------------------------------------------------------------------------
# extract_disc_number
# ---------------------------------------------------------------------------


def test_extract_disc_number_parentheses_disc():
    assert extract_disc_number("Final Fantasy VII (Disc 1).bin") == 1
    assert extract_disc_number("Final Fantasy VII (Disc 2).bin") == 2
    assert extract_disc_number("Final Fantasy VII (Disc 3).bin") == 3


def test_extract_disc_number_disk_spelling():
    assert extract_disc_number("Game (Disk 1).iso") == 1


def test_extract_disc_number_cd_variant():
    assert extract_disc_number("Game (CD1).bin") == 1
    assert extract_disc_number("Game (CD 2).bin") == 2


def test_extract_disc_number_square_brackets():
    assert extract_disc_number("[Disc 1].bin") == 1


def test_extract_disc_number_no_space_before_number():
    assert extract_disc_number("Game (Disc1).bin") == 1
    assert extract_disc_number("Game (disc2).bin") == 2


def test_extract_disc_number_case_insensitive():
    assert extract_disc_number("game (disc 1).bin") == 1
    assert extract_disc_number("game (DISC 1).bin") == 1


def test_extract_disc_number_returns_none_for_regular_rom():
    assert extract_disc_number("Crash Bandicoot.bin") is None
    assert extract_disc_number("Mario.nes") is None
    assert extract_disc_number("(Track 1).bin") is None


# ---------------------------------------------------------------------------
# strip_disc_marker
# ---------------------------------------------------------------------------


def test_strip_disc_marker_removes_simple_marker():
    assert strip_disc_marker("Final Fantasy VII (Disc 1)") == "Final Fantasy VII"


def test_strip_disc_marker_preserves_region_tags():
    assert strip_disc_marker("Final Fantasy VII (Disc 1) (NTSC)") == "Final Fantasy VII (NTSC)"


def test_strip_disc_marker_collapses_double_spaces():
    # marker in the middle leaves two spaces
    result = strip_disc_marker("A (Disc 1) B")
    assert "  " not in result
    assert result == "A B"


def test_strip_disc_marker_cd_variant():
    assert strip_disc_marker("Game (CD1)") == "Game"


# ---------------------------------------------------------------------------
# base_name_for_m3u
# ---------------------------------------------------------------------------


def test_base_name_for_m3u_returns_none_for_regular_file():
    assert base_name_for_m3u("Crash Bandicoot.bin") is None
    assert base_name_for_m3u("Mario.nes") is None


def test_base_name_for_m3u_strips_extension_and_marker():
    assert base_name_for_m3u("Final Fantasy VII (Disc 1).bin") == "Final Fantasy VII"


def test_base_name_for_m3u_preserves_other_tags():
    assert base_name_for_m3u("Game (Disc 2) (NTSC).cue") == "Game (NTSC)"


# ---------------------------------------------------------------------------
# group_multidisc_roms_raw
# ---------------------------------------------------------------------------

FF7_ROMS = [
    {"id": 1, "fs_name": "Final Fantasy VII (Disc 1).bin"},
    {"id": 2, "fs_name": "Final Fantasy VII (Disc 2).bin"},
    {"id": 3, "fs_name": "Final Fantasy VII (Disc 3).bin"},
    {"id": 4, "fs_name": "Crash Bandicoot.bin"},
]


def test_group_multidisc_roms_raw_groups_disc_files():
    groups = group_multidisc_roms_raw(FF7_ROMS)
    assert "Final Fantasy VII" in groups
    assert len(groups["Final Fantasy VII"]) == 3


def test_group_multidisc_roms_raw_excludes_non_disc_roms():
    groups = group_multidisc_roms_raw(FF7_ROMS)
    assert "Crash Bandicoot" not in groups


def test_group_multidisc_roms_raw_sorts_by_disc_number():
    shuffled = [
        {"id": 3, "fs_name": "Game (Disc 3).bin"},
        {"id": 1, "fs_name": "Game (Disc 1).bin"},
        {"id": 2, "fs_name": "Game (Disc 2).bin"},
    ]
    groups = group_multidisc_roms_raw(shuffled)
    ordered_ids = [r["id"] for r in groups["Game"]]
    assert ordered_ids == [1, 2, 3]


def test_group_multidisc_roms_raw_excludes_single_disc():
    roms = [{"id": 1, "fs_name": "Solo (Disc 1).bin"}]
    assert group_multidisc_roms_raw(roms) == {}


def test_group_multidisc_roms_raw_skips_missing_fs_name():
    roms = [
        {"id": 1, "fs_name": "Game (Disc 1).bin"},
        {"id": 2},  # no fs_name
        {"id": 3, "fs_name": "Game (Disc 2).bin"},
    ]
    groups = group_multidisc_roms_raw(roms)
    assert len(groups["Game"]) == 2


# ---------------------------------------------------------------------------
# evaluate_m3u_operation
# ---------------------------------------------------------------------------


def _make_op(tmp_path: Path, name: str = "FF7") -> M3uOperation:
    return M3uOperation(
        category="m3u",
        destination=tmp_path / "psx" / f"{name}.m3u",
        disc_filenames=(f"{name} (Disc 1).bin", f"{name} (Disc 2).bin"),
    )


def test_evaluate_m3u_operation_create_when_missing(tmp_path: Path):
    op = _make_op(tmp_path)
    result = evaluate_m3u_operation(op)
    assert result.action == "create"


def test_evaluate_m3u_operation_ok_when_content_matches(tmp_path: Path):
    (tmp_path / "psx").mkdir()
    op = _make_op(tmp_path)
    op.destination.write_text("FF7 (Disc 1).bin\nFF7 (Disc 2).bin\n", encoding="utf-8")
    result = evaluate_m3u_operation(op)
    assert result.action == "ok"


def test_evaluate_m3u_operation_replace_when_content_differs(tmp_path: Path):
    (tmp_path / "psx").mkdir()
    op = _make_op(tmp_path)
    op.destination.write_text("old content\n", encoding="utf-8")
    result = evaluate_m3u_operation(op)
    assert result.action == "replace"


# ---------------------------------------------------------------------------
# apply_m3u_operation
# ---------------------------------------------------------------------------


def test_apply_m3u_operation_creates_file_and_directories(tmp_path: Path):
    op = _make_op(tmp_path)
    result = apply_m3u_operation(op)
    assert result.action == "create"
    assert op.destination.exists()
    content = op.destination.read_text(encoding="utf-8")
    assert content == "FF7 (Disc 1).bin\nFF7 (Disc 2).bin\n"


def test_apply_m3u_operation_returns_ok_when_already_up_to_date(tmp_path: Path):
    op = _make_op(tmp_path)
    apply_m3u_operation(op)  # first write
    result = apply_m3u_operation(op)  # idempotent
    assert result.action == "ok"


def test_apply_m3u_operation_replaces_stale_file(tmp_path: Path):
    op = _make_op(tmp_path)
    apply_m3u_operation(op)

    updated = M3uOperation(
        category="m3u",
        destination=op.destination,
        disc_filenames=("FF7 (Disc 1).bin", "FF7 (Disc 2).bin", "FF7 (Disc 3).bin"),
    )
    result = apply_m3u_operation(updated)
    assert result.action == "replace"
    content = updated.destination.read_text(encoding="utf-8")
    assert "FF7 (Disc 3).bin" in content


def test_apply_m3u_operation_returns_error_on_bad_parent(tmp_path: Path):
    blocking = tmp_path / "block"
    blocking.write_text("not a directory")
    op = M3uOperation(
        category="m3u",
        destination=blocking / "sub" / "Game.m3u",
        disc_filenames=("a.bin", "b.bin"),
    )
    result = apply_m3u_operation(op)
    assert result.action == "error"


# ---------------------------------------------------------------------------
# plan_m3u_operations
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://localhost", client_token="rmm_token"),
        nas=NasConfig(library_path=str(tmp_path / "nas"), resources_path=str(tmp_path / "res")),
        esde=EsdeConfig(roms_path=str(tmp_path / "roms")),
        emudeck=EmudeckConfig(
            bios_path=str(tmp_path / "bios"),
            media_path=str(tmp_path / "media"),
        ),
        assets=AssetsConfig(folder_map={}),
    )


class _StubClient:
    """Flat-file multi-disc: multiple ROM entries with disc markers in fs_name."""

    def list_platforms(self) -> list[PlatformSummary]:
        return [PlatformSummary(id=10, fs_slug="psx", name="PlayStation")]

    def list_roms_raw(self) -> list[dict]:
        return [
            {"id": 1, "platform_id": 10, "fs_name": "FF7 (Disc 1).bin",
             "has_multiple_files": False},
            {"id": 2, "platform_id": 10, "fs_name": "FF7 (Disc 2).bin",
             "has_multiple_files": False},
            {"id": 3, "platform_id": 10, "fs_name": "FF7 (Disc 3).bin",
             "has_multiple_files": False},
            {"id": 4, "platform_id": 10, "fs_name": "Crash.bin", "has_multiple_files": False},
        ]

    def get_rom(self, rom_id: int) -> dict:
        raise AssertionError(f"get_rom should not be called for flat-file ROMs (id={rom_id})")


class _StubClientFolderBased:
    """Folder-based multi-disc: one ROM entry per game, files inside a NAS folder."""

    def list_platforms(self) -> list[PlatformSummary]:
        return [PlatformSummary(id=10, fs_slug="psx", name="PlayStation")]

    def list_roms_raw(self) -> list[dict]:
        return [
            {"id": 99, "platform_id": 10, "fs_name": "FF7 (E)", "has_multiple_files": True},
            {"id": 4, "platform_id": 10, "fs_name": "Crash.chd", "has_multiple_files": False},
        ]

    def get_rom(self, rom_id: int) -> dict:
        if rom_id == 99:
            return {
                "id": 99,
                "fs_name": "FF7 (E)",
                "files": [
                    {"file_name": "FF7 (E) (Disc 1).chd"},
                    {"file_name": "FF7 (E) (Disc 2).chd"},
                    {"file_name": "FF7 (E) (Disc 3).chd"},
                ],
            }
        return {"id": rom_id, "files": []}


def test_plan_m3u_operations_produces_one_op_per_group(tmp_path: Path):
    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _StubClient())
    assert len(ops) == 1
    op = ops[0]
    assert op.category == "m3u"
    assert op.destination.name == "FF7.m3u"
    assert op.destination.parent.name == "psx"


def test_plan_m3u_operations_disc_filenames_ordered(tmp_path: Path):
    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _StubClient())
    assert ops[0].disc_filenames == ("FF7 (Disc 1).bin", "FF7 (Disc 2).bin", "FF7 (Disc 3).bin")


def test_plan_m3u_operations_skips_unknown_platform(tmp_path: Path):
    class _NoSlugClient(_StubClient):
        def list_platforms(self) -> list[PlatformSummary]:
            return [PlatformSummary(id=10, fs_slug=None, name="PlayStation")]

    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _NoSlugClient())
    assert ops == []


def test_plan_m3u_operations_excludes_single_disc_titles(tmp_path: Path):
    class _SingleDiscClient(_StubClient):
        def list_roms_raw(self) -> list[dict]:
            return [
                {"id": 1, "platform_id": 10, "fs_name": "Solo (Disc 1).bin",
                 "has_multiple_files": False},
            ]

    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _SingleDiscClient())
    assert ops == []


def test_plan_m3u_operations_folder_based_generates_m3u(tmp_path: Path):
    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _StubClientFolderBased())
    assert len(ops) == 1
    op = ops[0]
    assert op.destination.name == "FF7 (E).m3u"
    assert op.destination.parent.name == "psx"
    assert op.disc_filenames == (
        "FF7 (E)/FF7 (E) (Disc 1).chd",
        "FF7 (E)/FF7 (E) (Disc 2).chd",
        "FF7 (E)/FF7 (E) (Disc 3).chd",
    )


def test_plan_m3u_operations_folder_based_skips_non_disc_multi_file(tmp_path: Path):
    """A folder with multiple files but no disc markers is not a multi-disc game."""
    class _NonDiscClient(_StubClient):
        def list_roms_raw(self) -> list[dict]:
            return [{"id": 5, "platform_id": 10, "fs_name": "Game", "has_multiple_files": True}]

        def get_rom(self, rom_id: int) -> dict:
            return {"id": rom_id, "files": [
                {"file_name": "Game.bin"},
                {"file_name": "Game.cue"},
            ]}

    config = _make_config(tmp_path)
    ops = plan_m3u_operations(config, _NonDiscClient())
    assert ops == []


def test_detect_folder_multidisc_from_api_returns_disc_files(tmp_path: Path):
    from bifrost.multidisc import detect_folder_multidisc_from_api
    roms_raw = [{"id": 99, "has_multiple_files": True}]
    result = detect_folder_multidisc_from_api(roms_raw, _StubClientFolderBased())
    assert 99 in result
    assert result[99] == ["FF7 (E) (Disc 1).chd", "FF7 (E) (Disc 2).chd", "FF7 (E) (Disc 3).chd"]


def test_detect_folder_multidisc_from_api_excludes_single_disc(tmp_path: Path):
    from bifrost.multidisc import detect_folder_multidisc_from_api

    class _SingleFileClient:
        def get_rom(self, rom_id: int) -> dict:
            return {"id": rom_id, "files": [{"file_name": "Game (Disc 1).bin"}]}

    roms_raw = [{"id": 1, "has_multiple_files": True}]
    result = detect_folder_multidisc_from_api(roms_raw, _SingleFileClient())
    assert result == {}


def test_m3u_operation_target_property_equals_destination(tmp_path: Path):
    op = M3uOperation(
        category="m3u",
        destination=tmp_path / "Game.m3u",
        disc_filenames=("a.bin", "b.bin"),
    )
    assert op.target == op.destination
    assert not op.is_dir
