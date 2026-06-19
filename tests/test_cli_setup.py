from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from bifrost.cli import EXIT_API_ERROR, EXIT_AUTH_ERROR, EXIT_CONFIG_ERROR, EXIT_OK, main
from bifrost.config import (
    AppConfig,
    EmudeckConfig,
    EsdeConfig,
    NasConfig,
    RommConfig,
    load_config,
    save_config,
)
from bifrost.errors import ApiError


def test_setup_success_writes_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
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
            "setup",
            "--url",
            "http://romm.local/",
            "--token",
            "rmm_token",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    assert config_file.exists()

    cfg = load_config(config_file)
    assert cfg.romm.url == "http://romm.local"


def test_setup_auth_error_does_not_write_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--url",
            "http://romm.local",
            "--token",
            "rmm_token",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_AUTH_ERROR
    assert not config_file.exists()


def test_setup_skip_verify_writes_config_without_http(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--url",
            "http://romm.local",
            "--token",
            "rmm_token",
            "--skip-verify",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    assert config_file.exists()


def test_setup_invalid_token_returns_config_error(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--url",
            "http://romm.local",
            "--token",
            "bad_token",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert not config_file.exists()


def test_setup_pair_success_writes_exchanged_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"

    def fake_exchange(base_url: str, code: str) -> str:
        assert base_url == "http://romm.local"
        assert code == "MCM9FDSQ"
        return "rmm_from_pair"

    monkeypatch.setattr("bifrost.cli.exchange_pairing_code", fake_exchange)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--pair",
            "--pair-code",
            "MCM9-FDSQ",
            "--url",
            "http://romm.local",
            "--skip-verify",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.romm.client_token == "rmm_from_pair"


def test_setup_pair_invalid_code_returns_config_error(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--pair",
            "--pair-code",
            "BAD",
            "--url",
            "http://romm.local",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert not config_file.exists()


def test_setup_pair_with_token_returns_config_error(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--pair",
            "--url",
            "http://romm.local",
            "--token",
            "rmm_token",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert not config_file.exists()


def test_setup_pair_exchange_failure_returns_api_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"

    def fake_exchange(base_url: str, code: str) -> str:
        raise ApiError("exchange failed")

    monkeypatch.setattr("bifrost.cli.exchange_pairing_code", fake_exchange)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--pair",
            "--pair-code",
            "MCM9-FDSQ",
            "--url",
            "http://romm.local",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_API_ERROR
    assert not config_file.exists()


def test_setup_pair_accepts_nested_exchange_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"

    def fake_exchange(base_url: str, code: str) -> str:
        assert base_url == "http://romm.local"
        assert code == "R3DQZHHN"
        return "rmm_nested"

    monkeypatch.setattr("bifrost.cli.exchange_pairing_code", fake_exchange)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--pair",
            "--pair-code",
            "R3DQ-ZHHN",
            "--url",
            "http://romm.local",
            "--skip-verify",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.romm.client_token == "rmm_nested"


def test_setup_preserves_existing_paths_when_not_configuring_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    existing = AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_old"),
        nas=NasConfig(library_path="/data/library", resources_path="/data/resources"),
        esde=EsdeConfig(roms_path="~/Emulation/roms"),
        emudeck=EmudeckConfig(
            bios_path="~/Emulation/bios",
            media_path="~/Emulation/tools/downloaded_media",
        ),
    )
    save_config(existing, config_file)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--url",
            "http://romm.local",
            "--token",
            "rmm_new",
            "--skip-verify",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.romm.client_token == "rmm_new"
    assert cfg.nas.library_path == "/data/library"
    assert cfg.nas.resources_path == "/data/resources"
    assert cfg.esde.roms_path == "~/Emulation/roms"
    assert cfg.emudeck.media_path == "~/Emulation/tools/downloaded_media"


def test_setup_allows_explicit_path_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--url",
            "http://romm.local",
            "--token",
            "rmm_token",
            "--skip-verify",
            "--nas-library-path",
            "/mnt/gaming/romm/library",
            "--nas-resources-path",
            "/mnt/gaming/romm/resources",
            "--esde-roms-path",
            "~/Emulation/roms",
            "--emudeck-bios-path",
            "~/Emulation/bios",
            "--emudeck-media-path",
            "~/Emulation/tools/downloaded_media",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.nas.library_path == "/mnt/gaming/romm/library"
    assert cfg.nas.resources_path == "/mnt/gaming/romm/resources"
    assert cfg.esde.roms_path == "~/Emulation/roms"
    assert cfg.emudeck.bios_path == "~/Emulation/bios"
    assert cfg.emudeck.media_path == "~/Emulation/tools/downloaded_media"


def test_setup_wizard_reuses_existing_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    existing = AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_old", device_id="device-1"),
        nas=NasConfig(library_path="/data/library", resources_path="/data/resources"),
        esde=EsdeConfig(roms_path="~/Emulation/roms"),
        emudeck=EmudeckConfig(
            bios_path="~/Emulation/bios",
            media_path="~/Emulation/tools/downloaded_media",
        ),
    )
    save_config(existing, config_file)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    confirm_answers = iter([False, True, False])

    def fake_confirm(*_: Any, **__: Any) -> bool:
        return next(confirm_answers)

    def fake_prompt(_message: str, default: str | None = None, **_: Any) -> str:
        return default or ""

    monkeypatch.setattr("bifrost.cli.Confirm.ask", fake_confirm)
    monkeypatch.setattr("bifrost.cli.Prompt.ask", fake_prompt)

    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.romm.url == "http://romm.local"
    assert cfg.romm.client_token == "rmm_old"
    assert cfg.romm.device_id == "device-1"
    assert cfg.nas.library_path == "/data/library"
    assert cfg.emudeck.media_path == "~/Emulation/tools/downloaded_media"


def test_setup_wizard_can_change_selected_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.toml"
    existing = AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_old"),
        nas=NasConfig(library_path="/data/library", resources_path="/data/resources"),
        esde=EsdeConfig(roms_path="~/Emulation/roms"),
        emudeck=EmudeckConfig(
            bios_path="~/Emulation/bios",
            media_path="~/Emulation/tools/downloaded_media",
        ),
    )
    save_config(existing, config_file)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={})

    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    confirm_answers = iter([False, True, True])

    def fake_confirm(*_: Any, **__: Any) -> bool:
        return next(confirm_answers)

    def fake_prompt(message: str, default: str | None = None, **_: Any) -> str:
        if message == "NAS library path":
            return "/new/library"
        return default or ""

    monkeypatch.setattr("bifrost.cli.Confirm.ask", fake_confirm)
    monkeypatch.setattr("bifrost.cli.Prompt.ask", fake_prompt)

    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    cfg = load_config(config_file)
    assert cfg.nas.library_path == "/new/library"
    assert cfg.nas.resources_path == "/data/resources"
    assert cfg.romm.client_token == "rmm_old"
