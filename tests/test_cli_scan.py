from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from bifrost.api.client import RommApiClient
from bifrost.cli import EXIT_OK, main
from bifrost.config import AppConfig, RommConfig


def make_config() -> AppConfig:
    return AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_token"))


def test_roms_count_uses_total_from_paged_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            assert request.url.params.get("limit") == "1"
            assert request.url.params.get("matched") == "false"
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "total": 12,
                    "limit": 1,
                    "offset": 0,
                    "char_index": {},
                    "rom_id_index": [],
                    "filter_values": {},
                },
            )
        return httpx.Response(404, json={})

    client = RommApiClient(make_config(), transport=httpx.MockTransport(handler))
    assert client.roms_count(matched=False) == 12
    client.close()


def test_scan_command_reports_anomaly_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"
""".strip(),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stats":
            return httpx.Response(200, json={"PLATFORMS": 3, "ROMS": 120, "SAVES": 4})
        if request.url.path == "/api/roms":
            filters = request.url.params
            if filters.get("matched") == "false":
                total = 2
            elif filters.get("missing") == "true":
                total = 1
            elif filters.get("duplicate") == "true":
                total = 5
            else:
                total = 0
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "total": total,
                    "limit": 1,
                    "offset": 0,
                    "char_index": {},
                    "rom_id_index": [],
                    "filter_values": {},
                },
            )
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(main, ["scan", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    assert "Unmatched ROMs" in result.output
    assert "Duplicate ROMs" in result.output
    assert "Missing ROMs" in result.output
