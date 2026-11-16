"""Device-independent input model: keyboard, gamepad and mouse all become Events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    ACCEPT = auto()  # A button, mouse click
    CONFIRM = auto()  # Enter key, Start button: accept, or commit while typing
    BACK = auto()  # B button, Escape
    BACKSPACE = auto()
    TEXT = auto()  # a typed character, carried in Event.text


@dataclass(frozen=True)
class Event:
    action: Action
    text: str = ""
