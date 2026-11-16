"""Settings screen state machine, independent of any rendering or input device."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from bifrost.config import AppConfig, set_config_value
from bifrost.errors import ConfigError
from bifrost.gui.inputs import Action, Event
from bifrost.gui.schema import Setting, build_settings, get_text

Mode = Literal["sections", "rows", "edit"]

OSK_LOWER = ("1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm./_", ":,~@+=\\!?&")
OSK_UPPER = ("1234567890", "QWERTYUIOP", "ASDFGHJKL-", "ZXCVBNM./_", ":,~@+=\\!?&")
# Last row: shift, space, backspace, done, cancel.
OSK_ACTIONS = ("shift", "space", "back", "done", "cancel")


class SettingsScreen:
    """Browse sections and rows, toggle/cycle values, edit text with an on-screen keyboard.

    Every successful change is validated by :func:`set_config_value` and handed to
    ``save`` straight away; a rejected value leaves the config untouched and sets ``error``.
    """

    def __init__(self, config: AppConfig, save: Callable[[AppConfig], None]) -> None:
        self.config = config
        self._save = save
        self.settings = build_settings()
        self.sections = list(dict.fromkeys(s.section for s in self.settings))
        self.mode: Mode = "sections"
        self.section = 0
        self.row = 0
        self.error = ""
        self.saved = False  # last change was written to disk
        self.buffer = ""
        self.osk_row = 0
        self.osk_col = 0
        self.shift = False
        self.done = False

    # ── queries ──────────────────────────────────────────────────────────
    @property
    def rows(self) -> list[Setting]:
        name = self.sections[self.section]
        return [s for s in self.settings if s.section == name]

    @property
    def current(self) -> Setting:
        return self.rows[self.row]

    @property
    def osk_grid(self) -> tuple[tuple[str, ...], ...]:
        letters = OSK_UPPER if self.shift else OSK_LOWER
        return (*(tuple(row) for row in letters), OSK_ACTIONS)

    # ── input ────────────────────────────────────────────────────────────
    def handle(self, event: Event) -> None:
        if self.mode == "edit":
            self._handle_edit(event)
        elif self.mode == "rows":
            self._handle_rows(event)
        else:
            self._handle_sections(event)

    def _handle_sections(self, event: Event) -> None:
        action = event.action
        if action is Action.UP:
            self.section = (self.section - 1) % len(self.sections)
        elif action is Action.DOWN:
            self.section = (self.section + 1) % len(self.sections)
        elif action in (Action.ACCEPT, Action.CONFIRM, Action.RIGHT):
            self.mode, self.row, self.error = "rows", 0, ""
        elif action is Action.BACK:
            self.done = True

    def _handle_rows(self, event: Event) -> None:
        action, setting = event.action, self.current
        if action is Action.UP:
            self.row = (self.row - 1) % len(self.rows)
        elif action is Action.DOWN:
            self.row = (self.row + 1) % len(self.rows)
        elif action is Action.BACK or (action is Action.LEFT and setting.kind not in _STEPPED):
            self.mode, self.error = "sections", ""
        elif action in (Action.LEFT, Action.RIGHT) and setting.kind in _STEPPED:
            self._step(setting, 1 if action is Action.RIGHT else -1)
        elif action in (Action.ACCEPT, Action.CONFIRM):
            if setting.kind in _STEPPED and setting.kind != "number":
                self._step(setting, 1)
            else:
                self._start_edit(setting)

    def _step(self, setting: Setting, direction: int) -> None:
        current = get_text(self.config, setting)
        if setting.kind == "bool":
            self._apply(setting, "false" if current == "on" else "true")
        elif setting.kind == "choice":
            index = setting.choices.index(current) if current in setting.choices else 0
            self._apply(setting, setting.choices[(index + direction) % len(setting.choices)])
        elif setting.kind == "number":
            try:
                number = float(current) + direction
            except ValueError:
                return
            self._apply(setting, str(int(number)) if "." not in current else str(number))

    def _start_edit(self, setting: Setting) -> None:
        self.mode, self.error = "edit", ""
        self.buffer = "" if setting.kind == "secret" else get_text(self.config, setting)
        self.osk_row = self.osk_col = 0
        self.shift = False

    def _handle_edit(self, event: Event) -> None:
        action = event.action
        if action is Action.TEXT:
            self.buffer += event.text
        elif action is Action.BACKSPACE:
            self.buffer = self.buffer[:-1]
        elif action is Action.CONFIRM:
            self._commit()
        elif action is Action.BACK:
            self.mode, self.error = "rows", ""
        elif action is Action.ACCEPT:
            self._press_osk()
        elif action is Action.UP:
            self.osk_row = (self.osk_row - 1) % len(self.osk_grid)
            self._clamp_osk()
        elif action is Action.DOWN:
            self.osk_row = (self.osk_row + 1) % len(self.osk_grid)
            self._clamp_osk()
        elif action is Action.LEFT:
            self.osk_col = (self.osk_col - 1) % len(self.osk_grid[self.osk_row])
        elif action is Action.RIGHT:
            self.osk_col = (self.osk_col + 1) % len(self.osk_grid[self.osk_row])

    def _clamp_osk(self) -> None:
        self.osk_col = min(self.osk_col, len(self.osk_grid[self.osk_row]) - 1)

    def click_osk(self, row: int, col: int) -> None:
        """Mouse click on an on-screen keyboard key."""
        if 0 <= row < len(self.osk_grid) and 0 <= col < len(self.osk_grid[row]):
            self.osk_row, self.osk_col = row, col
            self._press_osk()

    def _press_osk(self) -> None:
        key = self.osk_grid[self.osk_row][self.osk_col]
        if self.osk_row == len(self.osk_grid) - 1:
            if key == "shift":
                self.shift = not self.shift
            elif key == "space":
                self.buffer += " "
            elif key == "back":
                self.buffer = self.buffer[:-1]
            elif key == "done":
                self._commit()
            else:
                self.mode, self.error = "rows", ""
        else:
            self.buffer += key

    def _commit(self) -> None:
        if self._apply(self.current, self.buffer):
            self.mode = "rows"

    def _apply(self, setting: Setting, value: str) -> bool:
        try:
            updated, _ = set_config_value(self.config, setting.key, value)
        except ConfigError as exc:
            self.error = _short(str(exc))
            self.saved = False
            return False
        self.config = updated
        self.error = ""
        self._save(updated)
        self.saved = True
        return True

    # ── mouse helpers ────────────────────────────────────────────────────
    def focus_section(self, index: int) -> None:
        if self.mode != "edit" and 0 <= index < len(self.sections):
            self.section = index
            self.mode = "sections"

    def focus_row(self, index: int) -> None:
        if self.mode != "edit" and 0 <= index < len(self.rows):
            self.row = index
            self.mode = "rows"


_STEPPED = ("bool", "choice", "number")


def _short(message: str) -> str:
    """First line of a validation error, trimmed for a one-line status bar."""
    first = next((line.strip() for line in message.splitlines() if line.strip()), message)
    return first if len(first) <= 120 else first[:117] + "…"
