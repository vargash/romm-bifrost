"""Folder ROMs (games with extras) are exposed to ES-DE through their inner game file."""

from __future__ import annotations

from pathlib import Path

from bifrost.api.models import PlatformSummary, RomSummary
from bifrost.config import AppConfig, AssetsConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.gamelist import _rom_path_value, _with_launch_names
from bifrost.launch_targets import (
    DEFAULT_INNER_FILE_PLATFORMS,
    launch_entries,
    scan_inner_folders,
    with_game_files,
)
from bifrost.multidisc import plan_m3u_operations
from bifrost.symlink_manager import (
    plan_broken_link_cleanup,
    plan_incremental_symlink_ops,
    plan_stale_removals_from_deleted_ids,
    plan_symlink_operations,
)

GAME = "Final Fantasy XII (v0.23)"


def _config(tmp_path: Path, **esde: object) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://localhost", client_token="rmm_token"),
        nas=NasConfig(
            library_path=str(tmp_path / "library"),
            resources_path=str(tmp_path / "resources"),
        ),
        esde=EsdeConfig(roms_path=str(tmp_path / "esde"), **esde),
        emudeck=EmudeckConfig(
            bios_path=str(tmp_path / "bios"), media_path=str(tmp_path / "media")
        ),
        assets=AssetsConfig(folder_map={"cover": "covers"}),
    )


def _folder_rom(tmp_path: Path, files: list[str], name: str = GAME, slug: str = "ps2") -> Path:
    """Create roms/<slug>/<name>/ on the fake NAS holding files plus an extras subfolder."""
    folder = tmp_path / "library" / "roms" / slug / name
    (folder / "mod" / "textures").mkdir(parents=True)
    (folder / "mod" / "textures" / "pack.7z").write_bytes(b"x")
    for file_name in files:
        (folder / file_name).write_bytes(b"x")
    return folder


def _summary(name: str = GAME, slug: str = "ps2", rom_id: int = 4811, **kw: object) -> RomSummary:
    defaults = dict(
        id=rom_id,
        name=name,
        fs_name=name,
        fs_path=f"roms/{slug}",
        full_path=f"roms/{slug}/{name}",
        fs_extension="",
        platform_id=2,
        path_cover_large=f"/assets/romm/resources/roms/2/{rom_id}/cover/big.png",
    )
    defaults.update(kw)
    return RomSummary(**defaults)


class _Client:
    def __init__(self, roms: list[RomSummary], slug: str = "ps2") -> None:
        self._roms = roms
        self._slug = slug

    def list_platforms(self):
        return [PlatformSummary(id=2, name="Platform", fs_slug=self._slug)]

    def list_roms(self):
        return self._roms

    def list_firmware(self, platform_id=None):
        return []


# --- launch_entries -------------------------------------------------------


def test_file_rom_keeps_its_own_link(tmp_path: Path):
    target = tmp_path / "Game.chd"
    entries = launch_entries("ps2", "Game.chd", "chd", target, DEFAULT_INNER_FILE_PLATFORMS)
    assert [(e.name, e.target) for e in entries] == [("Game.chd", target)]


def test_folder_rom_links_the_inner_game_file_and_ignores_extras(tmp_path: Path):
    folder = _folder_rom(tmp_path, [f"{GAME}.chd", ".hidden"])
    entries = launch_entries("ps2", GAME, "", folder, DEFAULT_INNER_FILE_PLATFORMS)
    assert [(e.name, e.target) for e in entries] == [(f"{GAME}.chd", folder / f"{GAME}.chd")]


def test_folder_rom_on_unlisted_platform_keeps_the_folder_link(tmp_path: Path):
    folder = _folder_rom(tmp_path, ["game.exe"], slug="dos")
    entries = launch_entries("dos", GAME, "", folder, DEFAULT_INNER_FILE_PLATFORMS)
    assert [(e.name, e.target) for e in entries] == [(GAME, folder)]


def test_ps3_folder_keeps_the_folder_extension_rule(tmp_path: Path):
    folder = _folder_rom(tmp_path, ["eboot"], slug="ps3")
    entries = launch_entries("ps3", GAME, "", folder, DEFAULT_INNER_FILE_PLATFORMS)
    assert [e.name for e in entries] == [f"{GAME}.ps3"]


def test_config_extends_inner_file_platforms(tmp_path: Path):
    folder = _folder_rom(tmp_path, ["game.iso"], slug="saturn")
    config = _config(tmp_path, inner_file_platforms=["saturn"])
    rom = _summary(slug="saturn")
    ops = plan_symlink_operations(config, _Client([rom], slug="saturn"))
    assert [op.destination.name for op in ops if op.category == "rom"] == ["game.iso"]
    assert [op.target for op in ops if op.category == "rom"] == [folder / "game.iso"]


def test_disc_files_become_one_entry_each_in_disc_order(tmp_path: Path):
    discs = ["Game (Disc 2).chd", "Game (Disc 1).chd"]
    folder = _folder_rom(tmp_path, discs)
    entries = launch_entries("ps2", GAME, "", folder, DEFAULT_INNER_FILE_PLATFORMS)
    assert [e.name for e in entries] == ["Game (Disc 1).chd", "Game (Disc 2).chd"]


def test_cue_bin_set_keeps_the_folder_link(tmp_path: Path):
    folder = _folder_rom(tmp_path, ["Game.cue", "Game (Track 1).bin"])
    entries = launch_entries("ps2", GAME, "", folder, DEFAULT_INNER_FILE_PLATFORMS)
    assert [(e.name, e.target) for e in entries] == [(GAME, folder)]


def test_unreadable_folder_is_skipped(tmp_path: Path):
    missing = tmp_path / "library" / "roms" / "ps2" / GAME
    assert launch_entries("ps2", GAME, "", missing, DEFAULT_INNER_FILE_PLATFORMS) is None


# --- symlink planning -----------------------------------------------------


def test_promoted_rom_keeps_its_es_de_name_and_media_stem(tmp_path: Path):
    """Single file → folder: the link name, media names and target follow the inner file."""
    folder = _folder_rom(tmp_path, [f"{GAME}.chd"])
    config = _config(tmp_path)
    ops = plan_symlink_operations(config, _Client([_summary()]))

    rom_ops = [op for op in ops if op.category == "rom"]
    assert [(op.destination, op.target) for op in rom_ops] == [
        (tmp_path / "esde" / "ps2" / f"{GAME}.chd", folder / f"{GAME}.chd")
    ]
    asset_ops = [op for op in ops if op.category == "asset"]
    assert [op.destination.name for op in asset_ops] == [f"{GAME}.png"]


def test_incremental_plan_matches_full_plan(tmp_path: Path):
    _folder_rom(tmp_path, [f"{GAME}.chd"])
    config = _config(tmp_path)
    client = _Client([_summary()])
    assert plan_incremental_symlink_ops(config, client, client.list_roms()) == (
        plan_symlink_operations(config, client)
    )


def test_two_folders_with_the_same_game_file_name_do_not_share_a_link(tmp_path: Path):
    first = _folder_rom(tmp_path, ["game.iso"], name="A")
    second = _folder_rom(tmp_path, ["game.iso"], name="B")
    config = _config(tmp_path)
    roms = [_summary("A", rom_id=1), _summary("B", rom_id=2)]
    ops = [op for op in plan_symlink_operations(config, _Client(roms)) if op.category == "rom"]
    assert [(op.destination.name, op.target) for op in ops] == [
        ("game.iso", first / "game.iso"),
        ("B", second),
    ]


def test_skips_a_folder_rom_when_the_nas_is_unreadable(tmp_path: Path):
    config = _config(tmp_path)
    assert plan_symlink_operations(config, _Client([_summary()])) == []


# --- stale links ----------------------------------------------------------


def _link(config: AppConfig, name: str, target: Path) -> Path:
    link = Path(config.esde.roms_path) / "ps2" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link


def test_broken_link_cleanup_removes_only_dangling_unplanned_nas_links(tmp_path: Path):
    folder = _folder_rom(tmp_path, [f"{GAME}.chd", "Other.chd"])
    config = _config(tmp_path)
    old_name = f"{GAME}.iso"
    dangling = _link(config, old_name, tmp_path / "library" / "roms" / "ps2" / old_name)
    healthy = _link(config, "Unrelated.chd", folder / "Other.chd")
    foreign = _link(config, "foreign.chd", tmp_path / "elsewhere.chd")

    ops = plan_incremental_symlink_ops(config, _Client([_summary()]), [_summary()])
    removals = plan_broken_link_cleanup(config, ops)

    assert [r.destination for r in removals] == [dangling]
    assert healthy.exists() and foreign.is_symlink()


def test_broken_link_cleanup_does_nothing_while_the_nas_is_down(tmp_path: Path):
    config = _config(tmp_path)
    link = _link(config, "old.chd", tmp_path / "library" / "roms" / "ps2" / "old.chd")
    ops = plan_incremental_symlink_ops(config, _Client([_summary()]), [_summary()])
    assert not (tmp_path / "library").exists()
    assert plan_broken_link_cleanup(config, ops) == []
    assert link.is_symlink()


def test_deleted_rom_removes_every_link_pointing_into_its_path(tmp_path: Path):
    config = _config(tmp_path)
    rom_dir = tmp_path / "library" / "roms" / "ps2" / GAME
    disc1 = _link(config, "Game (Disc 1).chd", rom_dir / "Game (Disc 1).chd")
    disc2 = _link(config, "Game (Disc 2).chd", rom_dir / "Game (Disc 2).chd")
    other = _link(config, "Other.chd", tmp_path / "library" / "roms" / "ps2" / "Other.chd")
    cached = [{"id": 4811, "fs_name": GAME, "platform_id": 2, "full_path": f"roms/ps2/{GAME}"}]

    removals = plan_stale_removals_from_deleted_ids(config, {4811}, cached, {2: "ps2"})

    assert sorted(r.destination for r in removals) == sorted([disc1, disc2])
    assert other.is_symlink()


# --- M3U and gamelist -----------------------------------------------------


class _RawClient:
    def __init__(self, roms: list[dict]) -> None:
        self._roms = roms

    def list_platforms(self):
        return [PlatformSummary(id=2, name="PS2", fs_slug="ps2")]

    def list_roms_raw(self):
        return self._roms

    def get_rom(self, rom_id: int):
        raise AssertionError("inner-file platforms are read from the NAS, not the API")


def _raw(rom_id: int = 4811, name: str = GAME) -> dict:
    return {
        "id": rom_id,
        "platform_id": 2,
        "fs_name": name,
        "fs_extension": "",
        "full_path": f"roms/ps2/{name}",
        "has_multiple_files": True,
    }


def test_multidisc_folder_playlist_lists_the_linked_disc_names(tmp_path: Path):
    _folder_rom(tmp_path, ["Game (Disc 1).chd", "Game (Disc 2).chd"])
    config = _config(tmp_path)

    ops = plan_m3u_operations(config, _RawClient([_raw()]))

    assert [(op.destination.name, op.disc_filenames) for op in ops] == [
        (f"{GAME}.m3u", ("Game (Disc 1).chd", "Game (Disc 2).chd"))
    ]


def test_single_game_file_folder_needs_no_playlist_or_api_lookup(tmp_path: Path):
    _folder_rom(tmp_path, [f"{GAME}.chd"])
    assert plan_m3u_operations(_config(tmp_path), _RawClient([_raw()])) == []


def test_gamelist_path_follows_the_inner_game_file(tmp_path: Path):
    _folder_rom(tmp_path, ["Disc.iso"])
    config = _config(tmp_path)
    cached = [_raw()]

    tagged, disc_ids = _with_launch_names(config, _RawClient(cached), cached, {2: "ps2"})

    assert _rom_path_value(tagged[0]) == "./Disc.iso"
    assert disc_ids == set()
    assert "_bifrost_launch_name" not in cached[0]


# --- saves and states -----------------------------------------------------


class _PlatformsClient:
    def __init__(self) -> None:
        self.platform_calls = 0

    def list_platforms(self):
        self.platform_calls += 1
        return [PlatformSummary(id=2, name="PS2", fs_slug="ps2")]


def test_game_files_name_the_save_of_a_folder_rom(tmp_path: Path):
    """The folder is named differently from the file the emulator launches."""
    _folder_rom(tmp_path, ["Disc.iso"], name="FFXII v1.2")
    rom = _summary("FFXII v1.2")

    [tagged] = with_game_files(_config(tmp_path), _PlatformsClient(), [rom])

    assert tagged.game_files == ["Disc.iso"]
    assert tagged.launch_stem == "Disc"
    assert "Disc" in tagged.name_candidates
    assert rom.game_files == []


def test_save_and_state_files_match_the_folder_rom_by_its_game_file(tmp_path: Path):
    from bifrost.save_sync import _build_remote_rom_index, _normalize_name
    from bifrost.state_sync import _build_rom_index

    _folder_rom(tmp_path, ["Disc.iso"], name="FFXII v1.2")
    [tagged] = with_game_files(_config(tmp_path), _PlatformsClient(), [_summary("FFXII v1.2")])

    assert _build_remote_rom_index([tagged])[_normalize_name("Disc")] is tagged
    assert _build_rom_index([tagged])[_normalize_name("Disc")] is tagged


def test_folder_name_with_dots_is_not_cut_at_a_fake_extension():
    assert _summary("FFXII v1.2").launch_stem == "FFXII v1.2"
    assert _summary("Game.chd", fs_extension="chd").launch_stem == "Game"


def test_game_files_name_each_disc_as_a_candidate(tmp_path: Path):
    _folder_rom(tmp_path, ["Game (Disc 1).chd", "Game (Disc 2).chd"])

    [tagged] = with_game_files(_config(tmp_path), _PlatformsClient(), [_summary()])

    assert tagged.launch_stem == GAME  # several files: the folder name stays the stem
    assert {"Game (Disc 1)", "Game (Disc 2)"} <= set(tagged.name_candidates)


def test_platforms_are_only_fetched_when_the_library_has_folder_roms(tmp_path: Path):
    client = _PlatformsClient()
    file_rom = _summary("Game.chd", fs_extension="chd")
    unknown = _summary("Game.chd", fs_extension=None)

    assert with_game_files(_config(tmp_path), client, [file_rom, unknown]) == [file_rom, unknown]
    assert client.platform_calls == 0


def test_scan_ignores_file_roms_and_unlisted_platforms(tmp_path: Path):
    _folder_rom(tmp_path, ["game.iso"])
    config = _config(tmp_path)
    roms = [
        {**_raw(1), "fs_extension": "chd"},
        {**_raw(2), "platform_id": 9},
    ]
    scan = scan_inner_folders(config, roms, {2: "ps2", 9: "dos"})
    assert scan.handled_ids == frozenset()
