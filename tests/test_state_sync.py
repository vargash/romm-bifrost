from __future__ import annotations

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


def test_build_state_sync_preview_finds_upload_operation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    saves_root.mkdir(parents=True, exist_ok=True)
    state_file = saves_root / "Mario.state1"
    state_file.write_bytes(b"state-data")

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
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_state_sync_preview(config, client)

    assert preview.scanned_files == 1
    assert preview.mapped_files == 1
    assert preview.skipped_files == 0
    assert len(preview.operations) == 1
    assert preview.operations[0].action == "upload"
    assert preview.operations[0].file_name == "Mario.state1"
    client.close()


def test_state_sync_preview_ignores_state_screenshot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    saves_root = Path(config.emudeck.saves_path).expanduser()
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Mario.state1.png").write_bytes(b"png")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    client = RommApiClient(config, transport=httpx.MockTransport(handler))
    preview = build_state_sync_preview(config, client)

    assert preview.scanned_files == 0
    assert preview.mapped_files == 0
    assert preview.skipped_files == 0
    assert preview.operations == []
    client.close()
