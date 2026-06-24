"""Production-level tests for save_sync improvements (conflict, backup, dedup, legacy)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from bifrost.api.client import RommApiClient
from bifrost.api.models import CompleteOutcome, SyncCompletePayload, SyncOperationSchema
from bifrost.cli import main
from bifrost.config import AppConfig, EmudeckConfig, RommConfig, SyncConfig
from bifrost.save_sync import (
    _backup_local_file,
    _is_redundant_download,
    _resolve_conflict_action,
    build_save_sync_preview,
    execute_save_sync_preview,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, conflict_strategy: str = "ask") -> AppConfig:
    return AppConfig(
        romm=RommConfig(
            url="http://romm.local",
            client_token="rmm_token",
            device_id="device-1",
        ),
        emudeck=EmudeckConfig(saves_path=str(tmp_path / "saves")),
        sync=SyncConfig(conflict_strategy=conflict_strategy, direction="push_pull"),
    )


def config_path_for(tmp_path: Path, conflict_strategy: str = "ask") -> Path:
    saves_root = tmp_path / "saves"
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{saves_root}"

[sync]
conflict_strategy = "{conflict_strategy}"
direction = "push_pull"
""".strip(),
        encoding="utf-8",
    )
    cfg.chmod(0o600)
    return cfg


def negotiate_handler_with_conflict(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/roms":
        return httpx.Response(
            200,
            json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
        )
    if request.url.path == "/api/saves" and request.method == "GET":
        return httpx.Response(200, json=[])
    if request.url.path == "/api/sync/negotiate":
        return httpx.Response(
            200,
            json={
                "session_id": 42,
                "operations": [
                    {
                        "action": "conflict",
                        "rom_id": 10,
                        "save_id": 99,
                        "file_name": "Mario.sav",
                        "reason": "Both client and server modified since last sync",
                        "server_content_hash": "aabbcc",
                    }
                ],
                "total_upload": 0,
                "total_download": 0,
                "total_conflict": 1,
                "total_no_op": 0,
            },
        )
    return httpx.Response(404, json={})


# ---------------------------------------------------------------------------
# Unit tests: _resolve_conflict_action
# ---------------------------------------------------------------------------


def test_resolve_conflict_server_wins() -> None:
    result = _resolve_conflict_action("server_wins", False, "Mario.sav", 10)
    assert result == "download"


def test_resolve_conflict_local_wins() -> None:
    result = _resolve_conflict_action("local_wins", False, "Mario.sav", 10)
    assert result == "upload"


def test_resolve_conflict_ask_headless_defaults_to_upload() -> None:
    """Headless mode with ask strategy → local_wins (upload) as safe default."""
    result = _resolve_conflict_action("ask", False, "Mario.sav", 10)
    assert result == "upload"


def test_resolve_conflict_ask_interactive_defaults_to_upload() -> None:
    """Interactive mode without explicit override → upload as fallback."""
    result = _resolve_conflict_action("ask", True, "Mario.sav", 10)
    assert result == "upload"


# ---------------------------------------------------------------------------
# Unit tests: _backup_local_file
# ---------------------------------------------------------------------------


def test_backup_creates_bak_file(tmp_path: Path) -> None:
    original = tmp_path / "Mario.sav"
    original.write_bytes(b"save-content")

    backup = _backup_local_file(original)

    assert backup is not None
    assert backup.name == "Mario.sav.bak"
    assert backup.read_bytes() == b"save-content"
    assert original.exists()  # original untouched


def test_backup_returns_none_when_file_missing(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope.sav"
    result = _backup_local_file(nonexistent)
    assert result is None


# ---------------------------------------------------------------------------
# Unit tests: _is_redundant_download
# ---------------------------------------------------------------------------


def test_is_redundant_download_true_when_hashes_match(tmp_path: Path) -> None:
    content = b"save-data"
    local_file = tmp_path / "Mario.sav"
    local_file.write_bytes(content)
    server_hash = hashlib.md5(content).hexdigest()

    op = SyncOperationSchema(
        action="download",
        rom_id=10,
        save_id=99,
        file_name="Mario.sav",
        reason="test",
        server_content_hash=server_hash,
    )
    assert _is_redundant_download(op, local_file) is True


def test_is_redundant_download_false_when_hashes_differ(tmp_path: Path) -> None:
    local_file = tmp_path / "Mario.sav"
    local_file.write_bytes(b"local-version")

    op = SyncOperationSchema(
        action="download",
        rom_id=10,
        save_id=99,
        file_name="Mario.sav",
        reason="test",
        server_content_hash="deadbeef00000000",
    )
    assert _is_redundant_download(op, local_file) is False


def test_is_redundant_download_false_when_no_server_hash(tmp_path: Path) -> None:
    local_file = tmp_path / "Mario.sav"
    local_file.write_bytes(b"local-version")

    op = SyncOperationSchema(
        action="download",
        rom_id=10,
        save_id=99,
        file_name="Mario.sav",
        reason="test",
    )
    assert _is_redundant_download(op, local_file) is False


def test_is_redundant_download_false_when_file_missing(tmp_path: Path) -> None:
    op = SyncOperationSchema(
        action="download",
        rom_id=10,
        save_id=99,
        file_name="Missing.sav",
        reason="test",
        server_content_hash="aabbcc",
    )
    assert _is_redundant_download(op, tmp_path / "Missing.sav") is False


# ---------------------------------------------------------------------------
# Integration: conflict with server_wins → download
# ---------------------------------------------------------------------------


def test_execute_conflict_server_wins_triggers_download(tmp_path: Path) -> None:
    config = make_config(tmp_path, conflict_strategy="server_wins")
    saves_root = Path(config.emudeck.saves_path)
    profile_dir = saves_root / "retroarch/saves"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Mario.sav").write_bytes(b"local-data")

    calls: dict[str, int] = {"download": 0, "confirm": 0, "complete": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 42,
                    "operations": [
                        {
                            "action": "conflict",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Mario.sav",
                            "reason": "Both sides changed",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 1,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            calls["download"] += 1
            return httpx.Response(200, content=b"server-data")
        if request.url.path == "/api/saves/99/downloaded":
            calls["confirm"] += 1
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            calls["complete"] += 1
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 42,
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
    result = execute_save_sync_preview(config, client, preview, is_interactive=False)
    client.close()

    assert calls["download"] == 1
    assert calls["confirm"] == 1
    assert calls["complete"] == 1
    assert result.executed == 1
    assert result.failed == 0
    # Verify backup was created in the profile directory
    profile_dir = saves_root / "retroarch/saves"
    assert (profile_dir / "Mario.sav.bak").exists()
    assert (profile_dir / "Mario.sav.bak").read_bytes() == b"local-data"
    # Verify server content was written to the profile directory
    assert (profile_dir / "Mario.sav").read_bytes() == b"server-data"


# ---------------------------------------------------------------------------
# Integration: conflict with local_wins → upload
# ---------------------------------------------------------------------------


def test_execute_conflict_local_wins_triggers_upload(tmp_path: Path) -> None:
    config = make_config(tmp_path, conflict_strategy="local_wins")
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"local-data")

    calls: dict[str, int] = {"upload": 0, "track": 0, "complete": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 43,
                    "operations": [
                        {
                            "action": "conflict",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Mario.sav",
                            "reason": "Both sides changed",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 1,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99" and request.method == "PUT":
            calls["upload"] += 1
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "rom_id": 10,
                    "file_name": "Mario.sav",
                    "updated_at": "2026-06-22T00:00:00Z",
                },
            )
        if request.url.path == "/api/saves/99/track":
            calls["track"] += 1
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            calls["complete"] += 1
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 43,
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
    result = execute_save_sync_preview(config, client, preview, is_interactive=False)
    client.close()

    assert calls["upload"] == 1
    assert calls["track"] == 0  # track_save removed post-upload (redundant)
    assert calls["complete"] == 1
    assert result.executed == 1
    assert result.failed == 0


# ---------------------------------------------------------------------------
# Integration: conflict ask + headless → auto-resolves as upload
# ---------------------------------------------------------------------------


def test_execute_conflict_ask_headless_resolves_as_upload(tmp_path: Path) -> None:
    """is_interactive=False with ask strategy should auto-resolve to upload."""
    config = make_config(tmp_path, conflict_strategy="ask")
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"local-data")

    calls: dict[str, int] = {"upload": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 44,
                    "operations": [
                        {
                            "action": "conflict",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Mario.sav",
                            "reason": "Both sides changed",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 1,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99" and request.method == "PUT":
            calls["upload"] += 1
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "rom_id": 10,
                    "file_name": "Mario.sav",
                    "updated_at": "2026-06-22T00:00:00Z",
                },
            )
        if request.url.path == "/api/saves/99/track":
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 44,
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
    result = execute_save_sync_preview(config, client, preview, is_interactive=False)
    client.close()

    assert calls["upload"] == 1
    assert result.executed == 1


# ---------------------------------------------------------------------------
# Integration: download dedup — skip if hash already matches
# ---------------------------------------------------------------------------


def test_execute_download_skipped_when_hash_matches(tmp_path: Path) -> None:
    """If local file already matches server_content_hash, skip the download."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    content = b"identical-content"
    server_hash = hashlib.md5(content).hexdigest()
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(content)

    calls: dict[str, int] = {"download": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 50,
                    "operations": [
                        {
                            "action": "download",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Mario.sav",
                            "reason": "Server version is newer",
                            "server_content_hash": server_hash,
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            calls["download"] += 1
            return httpx.Response(200, content=content)
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 50,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-22T00:00:00Z",
                        "completed_at": "2026-06-22T00:00:01Z",
                        "operations_planned": 1,
                        "operations_completed": 0,
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

    assert calls["download"] == 0  # No actual download made
    assert result.skipped == 1
    assert result.executed == 0


# ---------------------------------------------------------------------------
# Integration: backup created before download overwrites local file
# ---------------------------------------------------------------------------


def test_execute_download_creates_backup_of_existing_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"old-local-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
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
                            "file_name": "Mario.sav",
                            "reason": "Server version is newer",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 1,
                    "total_conflict": 0,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            return httpx.Response(200, content=b"new-server-data")
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
    profile_dir = saves_root / "retroarch/saves"
    # Backup preserves original
    assert (profile_dir / "Mario.sav.bak").read_bytes() == b"old-local-data"
    # New content from server
    assert (profile_dir / "Mario.sav").read_bytes() == b"new-server-data"


# ---------------------------------------------------------------------------
# Integration: legacy fallback when negotiate returns 404
# ---------------------------------------------------------------------------


def test_build_preview_uses_legacy_fallback_on_negotiate_404(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    # Legacy fallback builds upload op for the local save
    assert preview.session_id is None
    assert preview.mapped_files == 1
    assert len(preview.operations) == 1
    assert preview.operations[0].action == "upload"
    assert preview.operations[0].file_name == "Mario.sav"


def test_build_preview_uses_legacy_fallback_on_negotiate_405(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Zelda.sav").write_bytes(b"save-data")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 20, "name": "Zelda", "fs_name": "Zelda.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(405, text="Method Not Allowed")
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert preview.session_id is None
    assert len(preview.operations) == 1
    assert preview.operations[0].action == "upload"
    assert preview.operations[0].file_name == "Zelda.sav"


def test_legacy_fallback_includes_download_in_push_pull_mode(tmp_path: Path) -> None:
    """In push_pull mode legacy fallback should also generate download ops for server-only saves."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    # No local save for "Zelda" — server has one
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"save-data")

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
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 77,
                        "rom_id": 20,
                        "user_id": 1,
                        "file_name": "Zelda.sav",
                        "updated_at": "2026-06-22T00:00:00Z",
                    }
                ],
            )
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_save_sync_preview(config, client)
    client.close()

    assert preview.session_id is None
    actions = {op.action for op in preview.operations}
    assert "upload" in actions  # Mario.sav upload
    assert "download" in actions  # Zelda.sav download from server


# ---------------------------------------------------------------------------
# Integration: CLI save-sync with server_wins via monkeypatch
# ---------------------------------------------------------------------------


def test_cli_save_sync_conflict_server_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_path_for(tmp_path, conflict_strategy="server_wins")

    calls: dict[str, int] = {"download": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 60,
                    "operations": [
                        {
                            "action": "conflict",
                            "rom_id": 10,
                            "save_id": 99,
                            "file_name": "Mario.sav",
                            "reason": "Both sides changed",
                        }
                    ],
                    "total_upload": 0,
                    "total_download": 0,
                    "total_conflict": 1,
                    "total_no_op": 0,
                },
            )
        if request.url.path == "/api/saves/99/content":
            calls["download"] += 1
            return httpx.Response(200, content=b"server-save")
        if request.url.path == "/api/saves/99/downloaded":
            return httpx.Response(200, json={"id": 99})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 60,
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

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(main, ["save-sync", "--config", str(cfg), "--apply"])

    assert result.exit_code == 0
    assert calls["download"] == 1
    assert "Save Sync Execution" in result.output


# ---------------------------------------------------------------------------
# F2: complete_sync_session resilience (404/409/410 → ALREADY_FINALIZED)
# ---------------------------------------------------------------------------


def _make_client_for_complete(tmp_path: Path, status_code: int) -> tuple[RommApiClient, AppConfig]:
    from bifrost.api.client import RetryConfig

    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(status_code, json={"detail": "gone"})
        return httpx.Response(404, json={})

    # attempts=1 avoids retries+sleep for 5xx responses
    client = RommApiClient(  # noqa: E501
        config, transport=httpx.MockTransport(handler), retry=RetryConfig(attempts=1)
    )
    return client, config


def test_complete_sync_session_404_returns_already_finalized(tmp_path: Path) -> None:
    client, config = _make_client_for_complete(tmp_path, 404)
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.ALREADY_FINALIZED


def test_complete_sync_session_409_returns_already_finalized(tmp_path: Path) -> None:
    client, config = _make_client_for_complete(tmp_path, 409)
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.ALREADY_FINALIZED


def test_complete_sync_session_410_returns_already_finalized(tmp_path: Path) -> None:
    client, config = _make_client_for_complete(tmp_path, 410)
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.ALREADY_FINALIZED


def test_complete_sync_session_other_4xx_returns_client_error(tmp_path: Path) -> None:
    client, config = _make_client_for_complete(tmp_path, 422)
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.CLIENT_ERROR


def test_complete_sync_session_5xx_returns_retry_later(tmp_path: Path) -> None:
    client, config = _make_client_for_complete(tmp_path, 500)
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.RETRY_LATER


def test_complete_sync_session_200_returns_accepted(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 99,
                        "device_id": "device-1",
                        "user_id": 1,
                        "status": "completed",
                        "initiated_at": "2026-06-22T00:00:00Z",
                        "completed_at": "2026-06-22T00:00:01Z",
                        "operations_planned": 0,
                        "operations_completed": 0,
                        "operations_failed": 0,
                        "error_message": None,
                        "created_at": "2026-06-22T00:00:00Z",
                        "updated_at": "2026-06-22T00:00:01Z",
                    }
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    outcome = client.complete_sync_session(99, SyncCompletePayload())
    client.close()
    assert outcome == CompleteOutcome.ACCEPTED


# ---------------------------------------------------------------------------
# F2: execute does NOT call track after upload
# ---------------------------------------------------------------------------


def test_execute_upload_does_not_call_track(tmp_path: Path) -> None:
    """track_save not called after upload — upload already sets DeviceSaveSync."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    (saves_root / "retroarch/saves").mkdir(parents=True, exist_ok=True)
    (saves_root / "retroarch/saves/Mario.sav").write_bytes(b"local-data")

    track_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/saves" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/sync/negotiate":
            return httpx.Response(
                200,
                json={
                    "session_id": 77,
                    "operations": [
                        {
                            "action": "upload",
                            "rom_id": 10,
                            "save_id": None,
                            "file_name": "Mario.sav",
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
            return httpx.Response(
                200,
                json={"id": 55, "rom_id": 10, "file_name": "Mario.sav", "updated_at": "2026-06-22T00:00:00Z"},  # noqa: E501
            )
        if "/track" in request.url.path:
            track_calls.append(request.url.path)
            return httpx.Response(200, json={"id": 55})
        if "/api/sync/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "session": {
                        "id": 77,
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
    result = execute_save_sync_preview(config, client, preview, is_interactive=False)
    client.close()

    assert result.executed == 1
    assert track_calls == [], f"track was called unexpectedly: {track_calls}"
