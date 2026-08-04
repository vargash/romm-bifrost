from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from click.testing import CliRunner

from bifrost.api.client import RommApiClient
from bifrost.cli import main
from bifrost.config import AppConfig, EmudeckConfig, RommConfig
from bifrost.save_sync import build_save_sync_preview, execute_save_sync_preview


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(
            url="http://romm.local",
            client_token="rmm_token",
            device_id="device-1",
        ),
        emudeck=EmudeckConfig(saves_path=str(tmp_path / "saves")),
    )


def test_build_save_sync_preview_negotiates_local_saves(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    save_file = profile_dir / "Mario.sav"
    save_file.write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 10,
                            "name": "Mario",
                            "fs_name": "Mario.zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["device_id"] == "device-1"
            assert len(payload["saves"]) == 1
            assert payload["saves"][0]["rom_id"] == 10
            assert payload["saves"][0]["file_name"] == "Mario.sav"
            return httpx.Response(
                200,
                json={
                    "session_id": 7,
                    "operations": [
                        {
                            "action": "no_op",
                            "rom_id": 10,
                            "file_name": "Mario.sav",
                            "reason": "already in sync",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 1,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)

    assert preview.device_id == "device-1"
    assert preview.scanned_files == 1
    assert preview.mapped_files == 1
    assert preview.skipped_files == 0
    assert preview.session_id == 7
    assert preview.operations[0].action == "no_op"
    client.close()


def test_save_sync_command_prints_preview(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 10,
                            "name": "Mario",
                            "fs_name": "Mario.zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 7,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(main, ["save-sync", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Bifrost Save Sync (preview)" in result.output
    assert "Preview only" in result.output


def test_save_sync_apply_only_file_executes_single_upload(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")
    (saves_root / "retroarch/saves/Zelda.sav").write_bytes(b"save-data")
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    calls: dict[str, int] = {"upload": 0, "track": 0, "complete": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 10, "name": "Mario", "fs_name": "Mario.zip"},
                        {"id": 20, "name": "Zelda", "fs_name": "Zelda.zip"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 9,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 10,
                            "file_name": "Mario.sav",
                            "reason": "Save exists on client but not on server",
                        },
                        {
                            "action": "upload",
                            "rom_id": 20,
                            "file_name": "Zelda.sav",
                            "reason": "Save exists on client but not on server",
                        },
                    ],
                    "total_upload": 2,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves" and request.method == "POST":
            calls["upload"] += 1
            assert request.url.params.get("rom_id") == "10"
            return httpx.Response(
                200,
                json={
                    "id": 100,
                    "rom_id": 10,
                    "user_id": 1,
                    "file_name": "Mario.sav",
                    "updated_at": "2026-06-19T00:00:00Z",
                },
            )
        if request.url.path == "/api/saves/100/track":
            calls["track"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["device_id"] == "device-1"
            return httpx.Response(
                200,
                json={
                    "id": 100,
                    "rom_id": 10,
                    "user_id": 1,
                    "file_name": "Mario.sav",
                    "updated_at": "2026-06-19T00:00:00Z",
                },
            )
        if request.url.path == "/api/sync/sessions/9/complete":
            calls["complete"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["operations_completed"] == 1
            assert payload["operations_failed"] == 0
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 9,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-19T00:00:00Z",
                        "completed_at": "2026-06-19T00:00:02Z",
                        "operations_planned": 2,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-06-19T00:00:00Z",
                        "updated_at": "2026-06-19T00:00:02Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "save-sync",
            "--config",
            str(config_path),
            "--apply",
            "--only-file",
            "Mario.sav",
        ],
    )

    assert result.exit_code == 0
    assert calls["upload"] == 1
    assert calls["track"] == 0  # track_save removed post-upload (redundant)
    assert calls["complete"] == 1
    assert "Save Sync Execution" in result.output


def test_save_sync_apply_upload_fallback_to_existing_save_on_post_failure(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")  # noqa: E501
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    calls: dict[str, int] = {"post_upload": 0, "put_upload": 0, "track": 0, "complete": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 3670,
                            "name": "Monkey Hero (Europe) (En,Fr,De,It)",
                            "fs_name": "Monkey Hero (Europe) (En,Fr,De,It).chd",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 13,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 3670,
                            "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                            "reason": "Save exists on client but not on server",
                        },
                        {
                            "action": "download",
                            "rom_id": 2613,
                            "save_id": 900,
                            "file_name": "Final Fantasy (USA) [2026-06-09_05-08-00].srm",
                            "reason": "Save exists on server but not on client",
                        },
                    ],
                    "total_upload": 1,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves" and request.method == "POST":
            calls["post_upload"] += 1
            return httpx.Response(500, text="Internal Server Error")
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 77,
                        "rom_id": 3670,
                        "user_id": 1,
                        "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                        "updated_at": "2026-06-19T00:00:00Z",
                    }
                ],
            )
        if request.url.path == "/api/saves/77" and request.method == "PUT":
            calls["put_upload"] += 1
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "rom_id": 3670,
                    "user_id": 1,
                    "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                    "updated_at": "2026-06-19T00:01:00Z",
                },
            )
        if request.url.path == "/api/saves/77/track":
            calls["track"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["device_id"] == "device-1"
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "rom_id": 3670,
                    "user_id": 1,
                    "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                    "updated_at": "2026-06-19T00:01:00Z",
                },
            )
        if request.url.path == "/api/sync/sessions/13/complete":
            calls["complete"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["operations_completed"] == 1
            assert payload["operations_failed"] == 0
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 13,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-19T00:00:00Z",
                        "completed_at": "2026-06-19T00:00:03Z",
                        "operations_planned": 2,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-06-19T00:00:00Z",
                        "updated_at": "2026-06-19T00:00:03Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "save-sync",
            "--config",
            str(config_path),
            "--apply",
            "--only-file",
            "Monkey Hero (Europe) (En,Fr,De,It).srm",
        ],
    )

    assert result.exit_code == 0
    assert calls["post_upload"] == 3
    assert calls["put_upload"] == 1
    assert calls["track"] == 0  # track_save removed post-upload (redundant)
    assert calls["complete"] == 1
    assert "Operations" in result.output
    assert "download" not in result.output


def test_save_sync_apply_upload_fallback_uses_global_save_lookup(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")  # noqa: E501
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    calls: dict[str, int] = {"post_upload": 0, "put_upload": 0, "track": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 3670,
                            "name": "Monkey Hero (Europe) (En,Fr,De,It)",
                            "fs_name": "Monkey Hero (Europe) (En,Fr,De,It).chd",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 14,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 3670,
                            "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                            "reason": "Save exists on client but not on server",
                        }
                    ],
                    "total_upload": 1,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves" and request.method == "POST":
            calls["post_upload"] += 1
            return httpx.Response(500, text="Internal Server Error")
        if request.url.path == "/api/saves" and request.method == "GET":
            if request.url.params.get("device_id") == "device-1":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 88,
                        "rom_id": 3670,
                        "user_id": 1,
                        "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                        "updated_at": "2026-06-19T00:00:00Z",
                    }
                ],
            )
        if request.url.path == "/api/saves/88" and request.method == "PUT":
            calls["put_upload"] += 1
            return httpx.Response(
                200,
                json={
                    "id": 88,
                    "rom_id": 3670,
                    "user_id": 1,
                    "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                    "updated_at": "2026-06-19T00:01:00Z",
                },
            )
        if request.url.path == "/api/saves/88/track":
            calls["track"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["device_id"] == "device-1"
            return httpx.Response(
                200,
                json={
                    "id": 88,
                    "rom_id": 3670,
                    "user_id": 1,
                    "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                    "updated_at": "2026-06-19T00:01:00Z",
                },
            )
        if request.url.path == "/api/sync/sessions/14/complete":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 14,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-19T00:00:00Z",
                        "completed_at": "2026-06-19T00:00:03Z",
                        "operations_planned": 1,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-06-19T00:00:00Z",
                        "updated_at": "2026-06-19T00:00:03Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "save-sync",
            "--config",
            str(config_path),
            "--apply",
            "--only-file",
            "Monkey Hero (Europe) (En,Fr,De,It).srm",
        ],
    )

    assert result.exit_code == 0
    assert calls["post_upload"] == 3
    assert calls["put_upload"] == 1
    assert calls["track"] == 0  # track_save removed post-upload (redundant)


def test_build_save_sync_preview_uses_default_slot_for_unslotted_save(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Mario.sav").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["saves"][0]["slot"] == "autosave"
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    build_save_sync_preview(config, client)
    client.close()


def test_build_save_sync_preview_respects_custom_default_slot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.sync.slot = "main"
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Mario.sav").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["saves"][0]["slot"] == "main"
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    build_save_sync_preview(config, client)
    client.close()


def test_build_save_sync_preview_duckstation_primary_slot_uses_default(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "duckstation/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Mario_1.mcd").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["saves"][0]["file_name"] == "Mario_1.mcd"
            assert payload["saves"][0]["slot"] == "autosave"
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    build_save_sync_preview(config, client)
    client.close()


def test_build_save_sync_preview_duckstation_secondary_slot_keeps_numeric_value(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "duckstation/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Mario_2.mcd").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["saves"][0]["slot"] == "2"
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    build_save_sync_preview(config, client)
    client.close()


def test_build_save_sync_preview_matches_tagged_save_name(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    save_file = profile_dir / "Final Fantasy (USA) [2026-06-09_05-08-00].srm"
    save_file.write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 2613,
                            "name": "Final Fantasy (USA)",
                            "fs_name": "Final Fantasy (USA).zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert len(payload["saves"]) == 1
            assert payload["saves"][0]["rom_id"] == 2613
            assert payload["saves"][0]["file_name"] == save_file.name
            return httpx.Response(
                200,
                json={
                    "session_id": 8,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)

    assert preview.mapped_files == 1
    assert preview.skipped_files == 0
    client.close()


def test_build_save_sync_preview_filters_redundant_upload_when_hash_matches(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    save_file = profile_dir / "Monkey Hero (Europe) (En,Fr,De,It).srm"
    payload = b"save-data"
    save_file.write_bytes(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 3670,
                            "name": "Monkey Hero (Europe) (En,Fr,De,It)",
                            "fs_name": "Monkey Hero (Europe) (En,Fr,De,It).chd",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 15,
                        "rom_id": 3670,
                        "user_id": 1,
                        "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                        "file_size_bytes": len(payload),
                        "content_hash": hashlib.md5(payload).hexdigest(),
                        "updated_at": "2026-06-19T00:00:00Z",
                    }
                ],
            )
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 9,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 3670,
                            "file_name": "Monkey Hero (Europe) (En,Fr,De,It).srm",
                            "reason": "Save exists on client but not on server",
                        }
                    ],
                    "total_upload": 1,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)

    assert preview.mapped_files == 1
    assert len(preview.operations) == 0
    client.close()


def test_build_save_sync_preview_excludes_state_files_from_save_payload(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")
    # .state files are excluded by the retroarch profile globs — not scanned at all
    (profile_dir / "Monkey Hero (Europe) (En,Fr,De,It).state").write_bytes(b"state-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 3670,
                            "name": "Monkey Hero (Europe) (En,Fr,De,It)",
                            "fs_name": "Monkey Hero (Europe) (En,Fr,De,It).chd",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert len(payload["saves"]) == 1
            assert payload["saves"][0]["file_name"].endswith(".srm")
            return httpx.Response(
                200,
                json={
                    "session_id": 10,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)

    assert preview.scanned_files == 1  # .state excluded by profile globs, not counted
    assert preview.mapped_files == 1
    assert preview.skipped_files == 0
    client.close()


def test_download_strips_romm_timestamp(monkeypatch, tmp_path: Path) -> None:
    """Server filename [YYYY-MM-DD_HH-MM-SS] tag is stripped; file lands as canonical name."""
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    server_file_name = "Castlevania - Harmony of Dissonance (USA) [2026-06-30_07-19-47].srm"
    canonical_name = "Castlevania - Harmony of Dissonance (USA).srm"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/device-1":
            return httpx.Response(200, json={"device_id": "device-1"})
        if request.url.path == "/api/roms":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 5,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 1,
                            "save_id": 42,
                            "file_name": server_file_name,
                            "emulator": "retroarch",
                            "reason": "server newer",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/42/content":
            return httpx.Response(200, content=b"save-content")
        if request.url.path == "/api/sync/sessions/5/complete":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 5,
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
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["save-sync", "--config", str(config_path), "--apply"],
    )

    assert result.exit_code == 0, result.output
    expected = saves_root / "retroarch/saves" / canonical_name
    assert expected.exists(), f"Expected {expected}. Output:\n{result.output}"
    assert expected.read_bytes() == b"save-content"
    assert not (saves_root / "retroarch/saves" / server_file_name).exists()


def test_download_resolves_emulator_subdir_when_no_local_file(
    monkeypatch, tmp_path: Path
) -> None:
    """Download without pre-existing local file uses emulator field to pick the right subdir."""
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/device-1":
            return httpx.Response(200, json={"device_id": "device-1"})
        if request.url.path == "/api/roms":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 6,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 2,
                            "save_id": 55,
                            "file_name": "Mario.sav",
                            "emulator": "retroarch",
                            "reason": "server newer",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/55/content":
            return httpx.Response(200, content=b"mario-save")
        if request.url.path == "/api/sync/sessions/6/complete":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 6,
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
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["save-sync", "--config", str(config_path), "--apply"],
    )

    assert result.exit_code == 0, result.output
    expected = saves_root / "retroarch/saves/Mario.sav"
    assert expected.exists(), f"Expected {expected}. Output:\n{result.output}"
    assert expected.read_bytes() == b"mario-save"
    assert not (saves_root / "Mario.sav").exists(), "Must not land in bare save_root"


def test_build_save_sync_preview_warns_on_unlinked_cross_core_saves(tmp_path: Path) -> None:
    """Same ROM, saves scanned from two emulators with no shared matching key.

    Bifrost has no way to know a DuckStation .mcd and a RetroArch .srm are the
    same game's save family unless the pair is explicitly opted into
    sync.cross_core_compat — so this should surface as an advisory notice
    rather than fail silently.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.srm").write_bytes(b"retroarch-save")
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "duckstation/saves/Mario_1.mcd").write_bytes(b"duckstation-save")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert len(preview.cross_core_warnings) == 1
    notice = preview.cross_core_warnings[0]
    assert "Mario" in notice
    assert "duckstation" in notice
    assert "retroarch" in notice


def test_build_save_sync_preview_no_warning_when_single_emulator(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert preview.cross_core_warnings == []


def test_build_save_sync_preview_warns_on_unrouted_remote_emulator(tmp_path: Path) -> None:
    """The realistic case: a mobile RetroArch client uploaded a mednafen_psx_hw
    save straight to RomM. This device only has a duckstation profile locally
    — the two save files were never scanned together on this filesystem — so
    _detect_unlinked_cross_core_saves (local-scan-only) can't see the clash.
    The preview must still warn, from the negotiate response's pending
    download, that this rom has a server save this device can't place.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 10, "name": "Crash Bandicoot", "fs_name": "Crash Bandicoot.zip"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Crash Bandicoot.srm",
                            "emulator": "mednafen_psx_hw",
                            "reason": "Save exists on server but not on client",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert len(preview.cross_core_warnings) == 1
    notice = preview.cross_core_warnings[0]
    assert "Crash Bandicoot" in notice
    assert "mednafen_psx_hw" in notice
    assert "duckstation" in notice
    assert "cross_core_compat" in notice


def test_build_save_sync_preview_no_remote_warning_once_opted_in(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.sync.cross_core_compat = ["mednafen_psx_hw"]
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 10, "name": "Crash Bandicoot", "fs_name": "Crash Bandicoot.zip"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Crash Bandicoot.srm",
                            "emulator": "mednafen_psx_hw",
                            "reason": "Save exists on server but not on client",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert preview.cross_core_warnings == []


def _mednafen_download_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/roms":
        return httpx.Response(
            200,
            json={"items": [{"id": 10, "name": "Crash Bandicoot", "fs_name": "Crash Bandicoot.zip"}], "total": 1},
        )
    if request.url.path == "/api/saves" and request.method == "GET":
        return httpx.Response(200, json=[])
    if request.url.path == "/api/sync/negotiate":
        return httpx.Response(
            200,
            json={
                "session_id": 51,
                "operations": [
                    {
                        "action": "download",
                        "rom_id": 10,
                        "save_id": 99,
                        "file_name": "Crash Bandicoot.srm",
                        "emulator": "mednafen_psx_hw",
                        "reason": "Save exists on server but not on client",
                    }
                ],
                "total_upload": 0,
                "total_download": 1,
                "total_conflict": 0,
                "total_no_op": 0,
            },
        )
    if request.url.path == "/api/saves/99/content":
        return httpx.Response(200, content=b"psx-memcard-bytes")
    if request.url.path == "/api/saves/99/downloaded":
        return httpx.Response(200, json={"id": 99})
    if "/api/sync/sessions/" in request.url.path:
        return httpx.Response(
            200,
            json={
                "session": {
                    "id": 51,
                    "device_id": "device-1",
                    "user_id": 1,
                    "status": "completed",
                    "initiated_at": "2026-06-22T00:00:00Z",
                    "completed_at": "2026-06-22T00:00:01Z",
                    "operations_planned": 1,
                    "operations_completed": 1,
                    "operations_failed": 0,
                    "error_message": None,
                    "created_at": "2026-06-22T00:00:00Z",
                    "updated_at": "2026-06-22T00:00:01Z",
                }
            },
        )
    return httpx.Response(404, json={})


def test_execute_download_ignores_unmapped_foreign_emulator_by_default(tmp_path: Path) -> None:
    """Baseline: without opting into sync.cross_core_compat, a save tagged with a
    foreign core (mednafen_psx_hw, as a mobile RetroArch client would report it)
    lands in the bare save_root, not inside a local profile directory — this is
    the existing fallback behavior and must not change unless the user opts in.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    client = RommApiClient(config, transport=httpx.MockTransport(_mednafen_download_handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1
    assert (saves_root / "Crash Bandicoot.srm").exists()
    assert not (saves_root / "duckstation/saves/Crash Bandicoot_1.mcd").exists()


def test_execute_download_applies_opted_in_cross_core_compat(tmp_path: Path) -> None:
    """With sync.cross_core_compat = ["mednafen_psx_hw"], a save tagged with that
    core is treated as DuckStation-compatible (per CROSS_CORE_COMPAT) and lands
    in duckstation/saves renamed to DuckStation's "<game>_<slot>.mcd" convention.
    """
    config = make_config(tmp_path)
    config.sync.cross_core_compat = ["mednafen_psx_hw"]
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    client = RommApiClient(config, transport=httpx.MockTransport(_mednafen_download_handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1
    destination = saves_root / "duckstation/saves/Crash Bandicoot_1.mcd"
    assert destination.exists(), list((saves_root / "duckstation/saves").iterdir())
    assert destination.read_bytes() == b"psx-memcard-bytes"
    assert not (saves_root / "Crash Bandicoot.srm").exists()


def test_build_save_sync_preview_resync_bypasses_server_negotiate(tmp_path: Path) -> None:
    """A save was deleted locally after already being synced. The server still
    thinks this device has it (a stateful is_current flag on its side) and a
    normal negotiate would return no operation for it — force_resync=True must
    skip /api/sync/negotiate entirely and reconcile purely from what's on disk
    right now, which should surface it as a download.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    # Klonoa.srm intentionally absent: simulates the user having deleted it.

    negotiate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal negotiate_calls
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Klonoa", "fs_name": "Klonoa.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 99,
                        "rom_id": 10,
                        "file_name": "Klonoa.srm",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "content_hash": "abc123",
                        "device_syncs": [
                            {
                                "device_id": "device-1",
                                "device_name": None,
                                "last_synced_at": "2026-01-01T00:00:00Z",
                                "is_untracked": False,
                                "is_current": True,
                            }
                        ],
                    }
                ],
            )
        if request.url.path == "/api/sync/negotiate":
            negotiate_calls += 1
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client, force_resync=True)
    client.close()

    assert negotiate_calls == 0, "force_resync must bypass /api/sync/negotiate entirely"
    assert preview.session_id is None
    assert len(preview.operations) == 1
    op = preview.operations[0]
    assert op.action == "download"
    assert op.rom_id == 10
    assert op.file_name == "Klonoa.srm"


def test_build_save_sync_preview_resync_still_warns_on_unrouted_remote_emulator(
    tmp_path: Path,
) -> None:
    """_legacy_negotiate (the path --resync forces) must propagate SaveSummary.emulator
    onto the download operations it builds — otherwise both the cross-core warning and
    the opt-in cross-core routing in execute_save_sync_preview silently stop working
    whenever resync (or the old-server 404/405 fallback) is used instead of the real
    /api/sync/negotiate.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 10, "name": "Crash Bandicoot", "fs_name": "Crash Bandicoot.zip"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 99,
                        "rom_id": 10,
                        "file_name": "Crash Bandicoot.srm",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "emulator": "mednafen_psx_hw",
                        "device_syncs": [],
                    }
                ],
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client, force_resync=True)
    client.close()

    assert len(preview.operations) == 1
    op = preview.operations[0]
    assert op.action == "download"
    assert op.emulator == "mednafen_psx_hw"

    assert len(preview.cross_core_warnings) == 1
    notice = preview.cross_core_warnings[0]
    assert "Crash Bandicoot" in notice
    assert "mednafen_psx_hw" in notice
    assert "duckstation" in notice


def test_execute_download_truncates_cross_core_trailer_to_expected_size(tmp_path: Path) -> None:
    """Real-world observed case: a mednafen_psx_hw save uploaded by a mobile client
    carries a 28-byte trailer (a JSON blob + magic-string footer) appended after the
    raw 131072-byte PS1 memory card image. DuckStation rejects the file outright if
    handed all 131100 bytes verbatim ("expected 131072, got 131100"). The opt-in
    compat rule's expected_size_bytes must truncate the trailer before writing.
    """
    config = make_config(tmp_path)
    config.sync.cross_core_compat = ["mednafen_psx_hw"]
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    raw_memcard = bytes(range(256)) * 512  # 131072 bytes of deterministic filler
    assert len(raw_memcard) == 131072
    trailer = b'{"h":true,"v":1}' + b"\x10\x00\x00\x00" + b"ARGOSY\x01\x00"
    assert len(trailer) == 28
    uploaded_content = raw_memcard + trailer
    assert len(uploaded_content) == 131100

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 10, "name": "Klonoa - Door to Phantomile", "fs_name": "Klonoa.zip"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 51,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Klonoa.srm",
                            "emulator": "mednafen_psx_hw",
                            "reason": "Save exists on server but not on client",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            return httpx.Response(200, content=uploaded_content)
        if request.url.path == "/api/saves/99/downloaded":
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 51,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-22T00:00:00Z",
                        "completed_at": "2026-06-22T00:00:01Z",
                        "operations_planned": 1,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-06-22T00:00:00Z",
                        "updated_at": "2026-06-22T00:00:01Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1
    destination = saves_root / "duckstation/saves/Klonoa_1.mcd"
    written = destination.read_bytes()
    assert len(written) == 131072
    assert written == raw_memcard


def test_execute_upload_matches_local_file_by_rom_and_slot_when_name_diverges(
    tmp_path: Path,
) -> None:
    """Real-world case: this device previously downloaded a cross-core-compat save
    (mednafen_psx_hw -> duckstation), renamed on write to the local naming
    convention ("Klonoa - Door to Phantomile (USA)_1.mcd"). The user then played
    and saved again locally, so the negotiated "upload" operation's file_name still
    echoes the *original* server-side save name from the mobile client
    ("Klonoa - Door to Phantomile (USA) [2026-08-03_21-19-47].srm") — per RomM's
    own docs, saves are paired on (rom_id, slot), not filename, and the echoed
    name is informational, not a local lookup key. Upload must not be skipped
    just because no local file has that literal echoed name.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)
    local_save = saves_root / "duckstation/saves/Klonoa - Door to Phantomile (USA)_1.mcd"
    local_save.write_bytes(b"fresh-local-save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 4762,
                            "name": "Klonoa - Door to Phantomile (USA)",
                            "fs_name": "Klonoa - Door to Phantomile (USA).zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["saves"][0]["slot"] == "autosave"
            return httpx.Response(
                200,
                json={
                    "session_id": 5,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 4762,
                            "save_id": 200,
                            "file_name": "Klonoa - Door to Phantomile (USA) "
                            "[2026-08-03_21-19-47].srm",
                            "slot": "autosave",
                            "reason": "Client save is newer than last sync",
                        }
                    ],
                    "total_upload": 1,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": 200,
                    "rom_id": 4762,
                    "file_name": "Klonoa - Door to Phantomile (USA)_1.mcd",
                    "updated_at": "2026-08-04T00:00:00Z",
                },
            )
        if "/track" in request.url.path:
            return httpx.Response(200, json={"id": 200})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 5,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-08-04T00:00:00Z",
                        "completed_at": "2026-08-04T00:00:01Z",
                        "operations_planned": 1,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-08-04T00:00:00Z",
                        "updated_at": "2026-08-04T00:00:01Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1, result.details
    assert result.details[0] == (
        "upload",
        "Klonoa - Door to Phantomile (USA) [2026-08-03_21-19-47].srm",
        "ok",
    )


def test_execute_upload_strips_local_slot_suffix_for_duckstation(tmp_path: Path) -> None:
    """DuckStation's local "_N" slot suffix is this device's own on-disk naming
    convention, not something other RomM clients share (RomM pairs on
    (rom_id, slot), not filename) — the file uploaded to the server must be the
    bare ROM name, not the local "_1.mcd" filename, so other clients (e.g.
    Argosy Launcher) recognize it as the ROM's autosave/latest save by name too.
    The local file on disk must NOT be renamed.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)
    local_save = saves_root / "duckstation/saves/Klonoa - Door to Phantomile (USA)_1.mcd"
    local_save.write_bytes(b"local-save-data")

    uploaded_names: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 10,
                            "name": "Klonoa - Door to Phantomile (USA)",
                            "fs_name": "Klonoa - Door to Phantomile (USA).zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 10,
                            "save_id": None,
                            "file_name": "Klonoa - Door to Phantomile (USA)_1.mcd",
                            "slot": "autosave",
                            "reason": "New save",
                        }
                    ],
                    "total_upload": 1,
                    "total_download": 0,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves" and request.method == "POST":
            uploaded_names.append(request.content)
            return httpx.Response(
                200,
                json={
                    "id": 55,
                    "rom_id": 10,
                    "file_name": "Klonoa - Door to Phantomile (USA).mcd",
                    "updated_at": "2026-08-04T00:00:00Z",
                },
            )
        if "/track" in request.url.path:
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1, result.details
    assert len(uploaded_names) == 1
    body = uploaded_names[0]
    assert b'filename="Klonoa - Door to Phantomile (USA).mcd"' in body
    assert b"_1.mcd" not in body
    # local file on disk untouched
    assert local_save.exists()
    assert local_save.read_bytes() == b"local-save-data"


def test_execute_download_restores_local_slot_suffix_for_duckstation(tmp_path: Path) -> None:
    """The read-back side of the upload-naming fix: a same-emulator (non-cross-core)
    download whose server file_name is bare (uploaded by another duckstation
    device, or by bifrost itself post-fix) must be written locally with the
    "_1" suffix DuckStation's per-game memory card convention expects.
    """
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "duckstation/saves").mkdir(parents=True, exist_ok=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 10,
                            "name": "Klonoa - Door to Phantomile (USA)",
                            "fs_name": "Klonoa - Door to Phantomile (USA).zip",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 51,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Klonoa - Door to Phantomile (USA) [2026-08-04_00-00-00].mcd",
                            "emulator": "duckstation",
                            "reason": "Save exists on server but not on client",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            return httpx.Response(200, content=b"remote-save-data")
        if request.url.path == "/api/saves/99/downloaded":
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 51,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-08-04T00:00:00Z",
                        "completed_at": "2026-08-04T00:00:01Z",
                        "operations_planned": 1,
                        "operations_completed": 1,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-08-04T00:00:00Z",
                        "updated_at": "2026-08-04T00:00:01Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    result = execute_save_sync_preview(config, client, preview)
    client.close()

    assert result.executed == 1, result.details
    destination = saves_root / "duckstation/saves/Klonoa - Door to Phantomile (USA)_1.mcd"
    assert destination.exists(), list((saves_root / "duckstation/saves").iterdir())
    assert destination.read_bytes() == b"remote-save-data"