"""Directory-based saves (PCSX2 folder memory cards) through save sync."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bifrost.api.client import RommApiClient
from bifrost.config import AppConfig, EmudeckConfig, RommConfig
from bifrost.save_sync import build_save_sync_preview, execute_save_sync_preview
from bifrost.saves import dir_map
from bifrost.saves.folder_pack import pack_dir, pack_dirs
from bifrost.saves.layout import EmudeckEsdeLayout

SERIAL = "BASLUS-20000"
_SESSION_DONE = {
    "session": {
        "id": 9,
        "device_id": "device-1",
        "user_id": 1,
        "status": "completed",
        "initiated_at": "2026-06-30T00:00:00Z",
        "completed_at": "2026-06-30T00:00:01Z",
        "operations_planned": 1,
        "operations_completed": 1,
        "operations_failed": 0,
        "error_message": None,
        "created_at": "2026-06-30T00:00:00Z",
        "updated_at": "2026-06-30T00:00:01Z",
    }
}


@pytest.fixture(autouse=True)
def isolated_dir_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dir_map, "_MAP_FILE", tmp_path / "state" / "dir_saves.json")
    from bifrost import play_sessions

    monkeypatch.setattr(play_sessions, "_STATE_DIR", tmp_path / "play")
    monkeypatch.setattr(play_sessions, "_PENDING_FILE", tmp_path / "play" / "pending.jsonl")


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_token", device_id="device-1"),
        emudeck=EmudeckConfig(saves_path=str(tmp_path / "saves")),
    )


def make_card(tmp_path: Path) -> Path:
    card = tmp_path / "saves" / "pcsx2" / "saves" / "Mcd001.ps2"
    game = card / SERIAL
    game.mkdir(parents=True)
    (game / "icon.sys").write_bytes(b"icon")
    (game / "save.bin").write_bytes(b"progress-1")
    return game


class Server:
    """Mock RomM recording uploads; negotiate returns the configured operations."""

    def __init__(self, operations: list[dict[str, Any]], saves_content: bytes = b"") -> None:
        self.operations = operations
        self.saves_content = saves_content
        self.uploads: list[httpx.Request] = []
        self.negotiated: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 5, "name": "Some PS2 Game", "fs_name": "Some PS2 Game.iso"}],
                      "total": 1},
            )
        if path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/saves" and request.method == "POST":
            self.uploads.append(request)
            return httpx.Response(200, json={"id": 77})
        if path == "/api/sync/negotiate":
            self.negotiated = json.loads(request.content)["saves"]
            ops = self.operations
            return httpx.Response(
                200,
                json={
                    "session_id": 9,
                    "operations": ops,
                    "total_upload": sum(o["action"] == "upload" for o in ops),
                    "total_download": sum(o["action"] == "download" for o in ops),
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if path == "/api/saves/88/content":
            return httpx.Response(200, content=self.saves_content)
        if path == "/api/sync/sessions/9/complete":
            return httpx.Response(200, json=_SESSION_DONE)
        return httpx.Response(404, json={})


def upload_op() -> dict[str, Any]:
    return {
        "action": "upload",
        "rom_id": 5,
        "file_name": f"{SERIAL}.zip",
        "emulator": "pcsx2",
        "slot": "autosave",
        "reason": "new",
    }


def test_layout_scans_game_dirs_of_configured_card(tmp_path: Path) -> None:
    game = make_card(tmp_path)
    (game.parent / "_pcsx2_superblock").write_bytes(b"x")
    (game.parent / f"{SERIAL}.bak").mkdir()

    results = EmudeckEsdeLayout().scan_saves(tmp_path / "saves", card_dirs={"pcsx2": "Mcd001.ps2"})

    assert [sf.path for sf in results if sf.profile.emulator == "pcsx2"] == [game]
    assert EmudeckEsdeLayout().scan_saves(tmp_path / "saves") == []


def test_unmapped_dir_is_skipped_outside_a_play_session(tmp_path: Path) -> None:
    make_card(tmp_path)
    config = make_config(tmp_path)
    server = Server([])
    client = RommApiClient(config, transport=httpx.MockTransport(server))

    preview = build_save_sync_preview(config, client, file_filters=["Some PS2 Game"])

    assert preview.mapped_files == 0
    assert preview.skipped_files == 1
    assert dir_map.load_dir_map("pcsx2") == {}


def test_game_end_attributes_changed_dir_to_targeted_rom_and_uploads_zip(tmp_path: Path) -> None:
    game = make_card(tmp_path)
    config = make_config(tmp_path)
    server = Server([upload_op()])
    client = RommApiClient(config, transport=httpx.MockTransport(server))

    preview = build_save_sync_preview(
        config,
        client,
        file_filters=["Some PS2 Game"],
        session_start=datetime.now(UTC) - timedelta(minutes=5),
    )

    assert dir_map.load_dir_map("pcsx2") == {SERIAL: 5}
    assert server.negotiated[0]["rom_id"] == 5
    assert server.negotiated[0]["file_name"] == f"{SERIAL}.zip"
    assert server.negotiated[0]["content_hash"] == hashlib.md5(pack_dir(game)).hexdigest()

    result = execute_save_sync_preview(
        config, client, preview, file_filters=["Some PS2 Game"]
    )

    assert result.executed == 1 and result.failed == 0
    assert len(server.uploads) == 1
    assert f'filename="{SERIAL}.zip"'.encode() in server.uploads[0].content
    assert pack_dir(game) in server.uploads[0].content


def test_dir_not_changed_during_session_is_not_attributed(tmp_path: Path) -> None:
    make_card(tmp_path)
    config = make_config(tmp_path)
    client = RommApiClient(config, transport=httpx.MockTransport(Server([])))

    preview = build_save_sync_preview(
        config,
        client,
        file_filters=["Some PS2 Game"],
        session_start=datetime.now(UTC) + timedelta(hours=1),
    )

    assert preview.mapped_files == 0
    assert dir_map.load_dir_map("pcsx2") == {}


def test_mapped_dir_upload_is_dropped_without_a_play_session(tmp_path: Path) -> None:
    make_card(tmp_path)
    dir_map.remember_dir("pcsx2", SERIAL, 5)
    config = make_config(tmp_path)
    client = RommApiClient(config, transport=httpx.MockTransport(Server([upload_op()])))

    preview = build_save_sync_preview(config, client)

    assert preview.mapped_files == 1
    assert preview.operations == []


def test_download_unpacks_game_dir_into_configured_card_and_maps_rom(tmp_path: Path) -> None:
    src = tmp_path / "other" / SERIAL
    src.mkdir(parents=True)
    (src / "save.bin").write_bytes(b"from-server")
    blob = pack_dir(src)
    config = make_config(tmp_path)
    op = {
        "action": "download",
        "rom_id": 5,
        "save_id": 88,
        "file_name": f"{SERIAL} [2026-06-30_10-00-00].zip",
        "emulator": "pcsx2",
        "slot": "autosave",
        "reason": "server newer",
    }
    client = RommApiClient(config, transport=httpx.MockTransport(Server([op], blob)))

    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)

    dest = tmp_path / "saves" / "pcsx2" / "saves" / "Mcd001.ps2" / SERIAL
    assert result.executed == 1, result.details
    assert (dest / "save.bin").read_bytes() == b"from-server"
    assert dir_map.load_dir_map("pcsx2") == {SERIAL: 5}


def test_download_replaces_existing_dir_keeping_backup(tmp_path: Path) -> None:
    game = make_card(tmp_path)
    src = tmp_path / "other" / SERIAL
    src.mkdir(parents=True)
    (src / "save.bin").write_bytes(b"newer")
    config = make_config(tmp_path)
    op = {
        "action": "download",
        "rom_id": 5,
        "save_id": 88,
        "file_name": f"{SERIAL}.zip",
        "emulator": "pcsx2",
        "slot": "autosave",
        "reason": "server newer",
    }
    client = RommApiClient(config, transport=httpx.MockTransport(Server([op], pack_dir(src))))

    preview = build_save_sync_preview(config, client)
    execute_save_sync_preview(config, client, preview)

    assert (game / "save.bin").read_bytes() == b"newer"
    assert (game.parent / f"{SERIAL}.bak" / "save.bin").read_bytes() == b"progress-1"


def make_extra_slots(game: Path, *names: str) -> list[Path]:
    """Add further game directories (e.g. one per in-game save slot) to the card."""
    extra = []
    for name in names:
        slot = game.parent / name
        slot.mkdir()
        (slot / "save.bin").write_bytes(name.encode())
        extra.append(slot)
    return extra


def test_all_dirs_of_a_rom_are_bundled_into_one_save(tmp_path: Path) -> None:
    game = make_card(tmp_path)
    slots = [game, *make_extra_slots(game, "BASLUS-20000-1", "BASLUS-20000-2")]
    for slot in slots:
        dir_map.remember_dir("pcsx2", slot.name, 5)
    config = make_config(tmp_path)
    server = Server([upload_op()])
    client = RommApiClient(config, transport=httpx.MockTransport(server))

    preview = build_save_sync_preview(
        config,
        client,
        file_filters=["Some PS2 Game"],
        session_start=datetime.now(UTC) - timedelta(minutes=5),
    )

    assert preview.mapped_files == 1
    assert len(server.negotiated) == 1
    assert server.negotiated[0]["file_name"] == f"{SERIAL}.zip"
    assert server.negotiated[0]["content_hash"] == hashlib.md5(pack_dirs(slots)).hexdigest()

    result = execute_save_sync_preview(config, client, preview, file_filters=["Some PS2 Game"])

    assert result.executed == 1 and result.failed == 0
    assert len(server.uploads) == 1
    assert pack_dirs(slots) in server.uploads[0].content


def test_dirs_of_different_roms_stay_separate_saves(tmp_path: Path) -> None:
    game = make_card(tmp_path)
    (other,) = make_extra_slots(game, "BASLUS-99999")
    dir_map.remember_dir("pcsx2", game.name, 5)
    dir_map.remember_dir("pcsx2", other.name, 6)
    config = make_config(tmp_path)
    client = RommApiClient(config, transport=httpx.MockTransport(Server([])))

    preview = build_save_sync_preview(config, client)

    assert preview.mapped_files == 2
    assert {s.rom_id for s in preview.local_saves} == {5, 6}


def test_download_unpacks_every_dir_of_a_bundle_and_maps_them(tmp_path: Path) -> None:
    src = tmp_path / "other"
    slots = []
    for name in (SERIAL, "BASLUS-20000-1"):
        (src / name).mkdir(parents=True)
        (src / name / "save.bin").write_bytes(name.encode())
        slots.append(src / name)
    config = make_config(tmp_path)
    op = {
        "action": "download",
        "rom_id": 5,
        "save_id": 88,
        "file_name": f"{SERIAL}.zip",
        "emulator": "pcsx2",
        "slot": "autosave",
        "reason": "server newer",
    }
    client = RommApiClient(
        config, transport=httpx.MockTransport(Server([op], pack_dirs(slots)))
    )

    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)

    card = tmp_path / "saves" / "pcsx2" / "saves" / "Mcd001.ps2"
    assert result.executed == 1, result.details
    assert (card / SERIAL / "save.bin").read_bytes() == SERIAL.encode()
    assert (card / "BASLUS-20000-1" / "save.bin").read_bytes() == b"BASLUS-20000-1"
    assert dir_map.load_dir_map("pcsx2") == {SERIAL: 5, "BASLUS-20000-1": 5}
