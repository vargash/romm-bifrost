"""Production-level tests for state_sync content_hash comparison."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from bifrost.api.client import RommApiClient
from bifrost.config import AppConfig, EmudeckConfig, RommConfig
from bifrost.state_sync import build_state_sync_preview


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(
            url="http://romm.local",
            client_token="rmm_token",
            device_id="device-1",
        ),
        emudeck=EmudeckConfig(saves_path=str(tmp_path / "saves")),
    )


def test_state_sync_skips_upload_when_content_hash_matches(tmp_path: Path) -> None:
    """If remote content_hash matches local hash, no upload operation should be generated."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    saves_root.mkdir(parents=True, exist_ok=True)
    content = b"state-data-identical"
    state_file = saves_root / "Mario.state1"
    state_file.write_bytes(content)
    local_hash = hashlib.md5(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "rom_id": 10,
                        "user_id": 1,
                        "file_name": "Mario.state1",
                        "file_size_bytes": len(content),
                        "content_hash": local_hash,
                        "updated_at": "2026-06-22T00:00:00Z",
                    }
                ],
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_state_sync_preview(config, client)
    client.close()

    # Hash matches → no upload needed
    assert preview.scanned_files == 1
    assert preview.mapped_files == 1
    assert len(preview.operations) == 0


def test_state_sync_generates_upload_when_content_hash_differs(tmp_path: Path) -> None:
    """If remote content_hash differs from local, upload should be generated."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    saves_root.mkdir(parents=True, exist_ok=True)
    content = b"new-local-state-data"
    state_file = saves_root / "Mario.state1"
    state_file.write_bytes(content)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "rom_id": 10,
                        "user_id": 1,
                        "file_name": "Mario.state1",
                        "file_size_bytes": len(content),
                        "content_hash": "different-old-hash",
                        "updated_at": "2026-06-22T00:00:00Z",
                    }
                ],
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_state_sync_preview(config, client)
    client.close()

    assert len(preview.operations) == 1
    assert preview.operations[0].action == "upload"
    assert preview.operations[0].file_name == "Mario.state1"
    assert preview.operations[0].state_id == 55


def test_state_sync_falls_back_to_size_when_no_content_hash(tmp_path: Path) -> None:
    """If server has no content_hash, fall back to size comparison."""
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path)
    saves_root.mkdir(parents=True, exist_ok=True)
    content = b"state-data-size-match"
    (saves_root / "Mario.state1").write_bytes(content)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={"items": [{"id": 10, "name": "Mario", "fs_name": "Mario.zip"}], "total": 1},
            )
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "rom_id": 10,
                        "user_id": 1,
                        "file_name": "Mario.state1",
                        "file_size_bytes": len(content),
                        "updated_at": "2026-06-22T00:00:00Z",
                    }
                ],
            )
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_state_sync_preview(config, client)
    client.close()

    # Same size, no content_hash → skip
    assert len(preview.operations) == 0
