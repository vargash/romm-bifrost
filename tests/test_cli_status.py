from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bifrost.cli import EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_OK, main


def write_valid_config(path: Path) -> None:
    path.write_text(
        """
[romm]
url = "http://romm.local"
client_token = "rmm_token"
""".strip(),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_status_returns_config_error_for_missing_file(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["status", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_status_ok_with_mock_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import httpx

    write_valid_config(tmp_path / "config.toml")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/platforms":
            return httpx.Response(200, json=[{"id": 1, "fs_slug": "psx"}])
        if request.url.path == "/api/stats":
            assert request.url.params.get("include_platform_stats") == "false"
            return httpx.Response(200, json={"PLATFORMS": 1, "ROMS": 1, "SAVES": 0})
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(main, ["status", "--config", str(tmp_path / "config.toml")])
    assert result.exit_code == EXIT_OK


def test_status_returns_api_error_on_server_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    write_valid_config(tmp_path / "config.toml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "error"})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(main, ["status", "--config", str(tmp_path / "config.toml")])
    assert result.exit_code == EXIT_API_ERROR
