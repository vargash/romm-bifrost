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
from bifrost.save_sync import build_save_sync_preview


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
    saves_root.mkdir(parents=True, exist_ok=True)
    save_file = saves_root / "Mario.sav"
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
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Mario.sav").write_bytes(b"save-data")
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
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Mario.sav").write_bytes(b"save-data")
    (saves_root / "Zelda.sav").write_bytes(b"save-data")
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
    assert calls["track"] == 1
    assert calls["complete"] == 1
    assert "Save Sync Execution" in result.output


def test_save_sync_apply_upload_fallback_to_existing_save_on_post_failure(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")
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
    assert calls["track"] == 1
    assert calls["complete"] == 1
    assert "Operations" in result.output
    assert "download" not in result.output


def test_save_sync_apply_upload_fallback_uses_global_save_lookup(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")
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
    assert calls["track"] == 1


def test_build_save_sync_preview_matches_tagged_save_name(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    saves_root.mkdir(parents=True, exist_ok=True)
    save_file = saves_root / "Final Fantasy (USA) [2026-06-09_05-08-00].srm"
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
    saves_root.mkdir(parents=True, exist_ok=True)
    save_file = saves_root / "Monkey Hero (Europe) (En,Fr,De,It).srm"
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
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Monkey Hero (Europe) (En,Fr,De,It).srm").write_bytes(b"save-data")
    (saves_root / "Monkey Hero (Europe) (En,Fr,De,It).state").write_bytes(b"state-data")

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

    assert preview.scanned_files == 2
    assert preview.mapped_files == 1
    assert preview.skipped_files == 1
    client.close()