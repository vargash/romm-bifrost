from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bifrost.cli import EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_OK, main


def write_valid_config(path: Path, device_id: str = "") -> None:
    device_line = f'device_id = "{device_id}"\n' if device_id else ""
    path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
{device_line}""".strip()
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_save_untrack_returns_config_error_for_missing_file(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["save", "untrack", "55", "--config", str(tmp_path / "missing.toml")]
    )
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_save_untrack_requires_device_id(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(
        main, ["save", "untrack", "55", "--config", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "No device_id" in result.output


def test_save_untrack_calls_untrack_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    write_valid_config(tmp_path / "config.toml", device_id="device-1")

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/saves/55/untrack":
            return httpx.Response(200, json={"id": 55, "device_syncs": []})
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main, ["save", "untrack", "55", "--config", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == EXIT_OK, result.output
    assert calls == [("POST", "/api/saves/55/untrack")]
    assert "untracked" in result.output


def test_save_untrack_returns_api_error_on_server_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    write_valid_config(tmp_path / "config.toml", device_id="device-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "error"})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main, ["save", "untrack", "55", "--config", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == EXIT_API_ERROR
