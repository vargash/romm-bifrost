from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from click.testing import CliRunner

from bifrost.cli import main
from bifrost.config import load_config


def test_device_enroll_writes_device_id_and_preserves_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = ""
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["name"] == "Living Room Rig"
            assert payload["platform"] == "linux"
            assert payload["client"] == "bifrost"
            assert payload["client_version"] == "0.1.0"
            assert payload["hostname"] == "living-room"
            assert payload["sync_mode"] == "api"
            assert payload["allow_existing"] is True
            assert payload["allow_duplicate"] is False
            assert payload["reset_syncs"] is False
            return httpx.Response(
                200,
                json={
                    "device_id": "device-123",
                    "name": "Living Room Rig",
                    "created_at": "2026-06-19T10:00:00Z",
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
            "device-enroll",
            "--config",
            str(config_path),
            "--name",
            "Living Room Rig",
            "--platform",
            "linux",
            "--client",
            "bifrost",
            "--client-version",
            "0.1.0",
            "--hostname",
            "living-room",
        ],
    )

    assert result.exit_code == 0
    cfg = load_config(config_path)
    assert cfg.romm.device_id == "device-123"
    assert "Device enrollment saved to config" in result.output