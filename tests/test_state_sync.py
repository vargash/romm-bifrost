from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from bifrost.api.client import RommApiClient
from bifrost.cli import main
from bifrost.config import AppConfig, EmudeckConfig, RommConfig
from bifrost.state_sync import build_state_sync_preview

# Fase 0: state sync escluso, comando CLI `state-sync` deregistrato.
# I test che invocano la CLI sono marcati skip (non rimossi) finché resta disabilitato.
_STATE_CLI_DISABLED = "state-sync CLI deregistrato (Fase 0 — state sync escluso)"


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


@pytest.mark.skip(reason=_STATE_CLI_DISABLED)
def test_state_sync_apply_only_file_executes_single_upload(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Mario.state1").write_bytes(b"state-data")
    (saves_root / "Zelda.state2").write_bytes(b"state-data")
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

    calls = {"upload": 0}

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
        if request.url.path == "/api/states" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/states" and request.method == "POST":
            calls["upload"] += 1
            assert request.url.params.get("rom_id") == "10"
            return httpx.Response(
                200,
                json={
                    "id": 100,
                    "rom_id": 10,
                    "file_name": "Mario.state1",
                    "updated_at": "2026-06-19T00:00:00Z",
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
            "state-sync",
            "--config",
            str(config_path),
            "--apply",
            "--only-file",
            "Mario.state1",
        ],
    )

    assert result.exit_code == 0
    assert calls["upload"] == 1
    assert "State Sync Execution" in result.output


@pytest.mark.skip(reason=_STATE_CLI_DISABLED)
def test_state_sync_apply_fallback_put_when_post_fails(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    saves_root = tmp_path / "saves"
    saves_root.mkdir(parents=True, exist_ok=True)
    (saves_root / "Monkey Hero.state").write_bytes(b"state-data")
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

    calls = {"post": 0, "put": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 3670, "name": "Monkey Hero", "fs_name": "Monkey Hero.chd"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/states" and request.method == "GET":
            calls["list"] += 1
            if calls["list"] == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 77,
                        "rom_id": 3670,
                        "user_id": 1,
                        "file_name": "Monkey Hero.state",
                        "file_name_no_tags": "Monkey Hero",
                        "file_name_no_ext": "Monkey Hero",
                        "file_extension": ".state",
                        "file_path": "",
                        "file_size_bytes": 3,
                        "full_path": "",
                        "download_path": "",
                        "missing_from_fs": False,
                        "created_at": "2026-06-19T00:00:00Z",
                        "updated_at": "2026-06-19T00:00:00Z",
                        "emulator": None,
                        "screenshot": None,
                    }
                ],
            )
        if request.url.path == "/api/states" and request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500, text="Internal Server Error")
        if request.url.path == "/api/states/77" and request.method == "PUT":
            calls["put"] += 1
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "rom_id": 3670,
                    "file_name": "Monkey Hero.state",
                    "updated_at": "2026-06-19T00:01:00Z",
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
            "state-sync",
            "--config",
            str(config_path),
            "--apply",
            "--only-file",
            "Monkey Hero.state",
        ],
    )

    assert result.exit_code == 0
    assert calls["post"] >= 1
    assert calls["put"] == 1
