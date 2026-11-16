"""On-screen keyboard with a text buffer, shared by every screen that needs typed text."""

from __future__ import annotations

from typing import Literal

from bifrost.gui.inputs import Action, Event

LOWER = ("1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm./_", ":,~@+=\\!?&")
UPPER = ("1234567890", "QWERTYUIOP", "ASDFGHJKL-", "ZXCVBNM./_", ":,~@+=\\!?&")
# Last row: shift, space, backspace, done, cancel.
ACTIONS = ("shift", "space", "back", "done", "cancel")

Result = Literal["commit", "cancel"] | None


class OnScreenKeyboard:
    """Edit a buffer by keyboard typing or by moving over a key grid.

    ``handle`` and ``click`` return ``"commit"`` when the user accepts the text and
    ``"cancel"`` when they back out; the owner decides what that means.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.row = 0
        self.col = 0
        self.shift = False

    def start(self, initial: str = "") -> None:
        self.buffer = initial
        self.row = self.col = 0
        self.shift = False

    @property
    def grid(self) -> tuple[tuple[str, ...], ...]:
        letters = UPPER if self.shift else LOWER
        return (*(tuple(row) for row in letters), ACTIONS)

    def handle(self, event: Event) -> Result:
        action = event.action
        if action is Action.TEXT:
            self.buffer += event.text
        elif action is Action.BACKSPACE:
            self.buffer = self.buffer[:-1]
        elif action is Action.CONFIRM:
            return "commit"
        elif action is Action.BACK:
            return "cancel"
        elif action is Action.ACCEPT:
            return self._press()
        elif action is Action.UP:
            self.row = (self.row - 1) % len(self.grid)
            self.col = min(self.col, len(self.grid[self.row]) - 1)
        elif action is Action.DOWN:
            self.row = (self.row + 1) % len(self.grid)
            self.col = min(self.col, len(self.grid[self.row]) - 1)
        elif action is Action.LEFT:
            self.col = (self.col - 1) % len(self.grid[self.row])
        elif action is Action.RIGHT:
            self.col = (self.col + 1) % len(self.grid[self.row])
        return None

    def click(self, row: int, col: int) -> Result:
        """Mouse click on a key."""
        if 0 <= row < len(self.grid) and 0 <= col < len(self.grid[row]):
            self.row, self.col = row, col
            return self._press()
        return None

    def _press(self) -> Result:
        key = self.grid[self.row][self.col]
        if self.row < len(self.grid) - 1:
            self.buffer += key
        elif key == "shift":
            self.shift = not self.shift
        elif key == "space":
            self.buffer += " "
        elif key == "back":
            self.buffer = self.buffer[:-1]
        elif key == "done":
            return "commit"
        else:
            return "cancel"
        return None
