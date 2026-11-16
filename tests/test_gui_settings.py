from __future__ import annotations

import os
from pathlib import Path

import pytest

from bifrost.config import AppConfig, RommConfig, set_config_value
from bifrost.errors import ConfigError
from bifrost.gui.inputs import Action, Event
from bifrost.gui.schema import build_settings, display_text, get_text
from bifrost.gui.settings_screen import SettingsScreen


def _config() -> AppConfig:
    return AppConfig(romm=RommConfig(url="http://romm.local", client_token="rmm_secret_token"))


def _screen(saved: list[AppConfig]) -> SettingsScreen:
    return SettingsScreen(_config(), saved.append)


def _press(screen: SettingsScreen, *actions: Action) -> None:
    for action in actions:
        screen.handle(Event(action))


def _type(screen: SettingsScreen, text: str) -> None:
    for char in text:
        screen.handle(Event(Action.TEXT, char))


def _open(screen: SettingsScreen, key: str) -> None:
    """Focus the row for key, in rows mode."""
    section = key.split(".")[0]
    screen.focus_section(screen.sections.index(section))
    screen.focus_row([s.key for s in screen.rows].index(key))


def test_schema_covers_config_and_skips_structured_values() -> None:
    keys = {s.key for s in build_settings()}

    assert {"romm.url", "esde.extras_path", "sync.direction", "sync.profiles.enabled"} <= keys
    assert "sync.core_mappings" not in keys
    assert "assets.folder_map" not in keys
    kinds = {s.key: s.kind for s in build_settings()}
    assert kinds["romm.client_token"] == "secret"
    assert kinds["esde.roms_path"] == "path"
    assert kinds["sync.direction"] == "choice"
    assert kinds["sync.parallel_workers"] == "number"
    assert kinds["sync.profiles.enabled"] == "list"
    assert kinds["cache.enabled"] == "bool"


def test_secret_is_masked_for_display_only() -> None:
    setting = next(s for s in build_settings() if s.key == "romm.client_token")

    assert display_text(_config(), setting) == "rmm_…"
    assert get_text(_config(), setting) == "rmm_secret_token"


def test_toggle_bool_saves_immediately() -> None:
    saved: list[AppConfig] = []
    screen = _screen(saved)
    _open(screen, "cache.enabled")

    _press(screen, Action.ACCEPT)

    assert screen.config.cache.enabled is False
    assert saved[-1].cache.enabled is False


def test_choice_cycles_with_left_right() -> None:
    screen = _screen([])
    _open(screen, "sync.direction")

    _press(screen, Action.RIGHT)
    assert screen.config.sync.direction == "push_only"
    _press(screen, Action.LEFT, Action.LEFT)
    assert screen.config.sync.direction == "pull_only"


def test_number_steps_and_rejects_below_minimum() -> None:
    screen = _screen([])
    _open(screen, "sync.parallel_workers")

    _press(screen, Action.LEFT)
    assert screen.config.sync.parallel_workers == 15
    for _ in range(20):
        _press(screen, Action.LEFT)
    assert screen.config.sync.parallel_workers == 1
    assert screen.error


def test_text_edit_with_keyboard_and_on_screen_keys() -> None:
    saved: list[AppConfig] = []
    screen = _screen(saved)
    _open(screen, "esde.extras_path")

    _press(screen, Action.ACCEPT)
    assert screen.mode == "edit"
    _type(screen, "~/ex")
    _press(screen, Action.ACCEPT)  # on-screen cursor starts on the "1" key
    _press(screen, Action.BACKSPACE, Action.CONFIRM)

    assert screen.mode == "rows"
    assert screen.config.esde.extras_path == "~/ex"
    assert saved[-1].esde.extras_path == "~/ex"


def test_on_screen_keyboard_done_commits_and_cancel_discards() -> None:
    screen = _screen([])
    _open(screen, "esde.extras_path")
    _press(screen, Action.ACCEPT)
    _press(screen, Action.DOWN, Action.ACCEPT)  # "q"
    last = len(screen.osk_grid) - 1
    screen.click_osk(last, screen.osk_grid[last].index("done"))
    assert screen.config.esde.extras_path == "q"

    _press(screen, Action.ACCEPT)
    _type(screen, "zzz")
    screen.click_osk(last, screen.osk_grid[last].index("cancel"))
    assert screen.mode == "rows"
    assert screen.config.esde.extras_path == "q"


def test_invalid_value_keeps_config_and_edit_open() -> None:
    saved: list[AppConfig] = []
    screen = _screen(saved)
    _open(screen, "romm.client_token")
    _press(screen, Action.ACCEPT)
    _type(screen, "bad")

    _press(screen, Action.CONFIRM)

    assert screen.mode == "edit"
    assert "rmm_" in screen.error
    assert screen.config.romm.client_token == "rmm_secret_token"
    assert saved == []


def test_list_value_is_comma_separated() -> None:
    screen = _screen([])
    _open(screen, "sync.profiles.enabled")
    _press(screen, Action.ACCEPT)
    _type(screen, "retroarch, mgba")
    _press(screen, Action.CONFIRM)

    assert screen.config.sync.profiles.enabled == ["retroarch", "mgba"]


def test_navigation_and_quit() -> None:
    screen = _screen([])

    _press(screen, Action.DOWN)
    assert screen.sections[screen.section] == screen.sections[1]
    _press(screen, Action.ACCEPT)
    assert screen.mode == "rows"
    _press(screen, Action.BACK)
    assert screen.mode == "sections"
    _press(screen, Action.BACK)
    assert screen.done


def test_set_config_value_errors() -> None:
    config = _config()
    with pytest.raises(ConfigError, match="dot notation"):
        set_config_value(config, "url", "x")
    with pytest.raises(ConfigError, match="Unknown key"):
        set_config_value(config, "esde.nope", "x")
    with pytest.raises(ConfigError, match="core-mapping"):
        set_config_value(config, "sync.core_mappings", "x")
    updated, value = set_config_value(config, "romm.url", "http://other/")
    assert updated.romm.url == "http://other"
    assert value == "http://other"


def test_pygame_window_renders_and_quits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    from bifrost.gui import app

    pygame.init()
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, unicode="\r"))
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode=""))
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode=""))
    app.run(_config(), tmp_path / "config.toml")
