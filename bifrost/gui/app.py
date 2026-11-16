"""pygame front end: window, input mapping and drawing for the settings screen."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bifrost.config import AppConfig, save_config
from bifrost.gui.inputs import Action, Event
from bifrost.gui.schema import display_text
from bifrost.gui.settings_screen import SettingsScreen

BG = (24, 26, 32)
PANEL = (34, 37, 46)
FOCUS = (74, 108, 196)
DIM_FOCUS = (52, 60, 84)
TEXT = (230, 232, 238)
MUTED = (140, 146, 160)
ERROR = (240, 110, 110)

STICK_DEADZONE = 0.6
STICK_REPEAT_MS = 180
STICK_POLL_MS = 30

FONT_FILE = Path(__file__).resolve().parent.parent / "data" / "fonts" / "NotoSans-Regular.ttf"
# Tried in order when the bundled font lacks a glyph (CJK titles and the like).
FALLBACK_FONTS = "notosanscjksc,notosanscjk,notosanssc,wenquanyi,droidsansfallback,unifont"

_HINTS = {
    "sections": "A/Enter open     B/Esc quit     Up/Down move",
    "rows": "A/Enter change     Left/B back     Up/Down move     Left/Right adjust",
    "edit": "A type key     Start/Enter save     Ctrl+V paste     B cancel",
}


def pygame_available() -> bool:
    try:
        import pygame  # noqa: F401
    except ImportError:
        return False
    return True


class UiFont:
    """The bundled font with a system fallback for glyphs it does not contain."""

    def __init__(self, pygame: Any, size: int) -> None:
        self._main = pygame.font.Font(str(FONT_FILE), size)
        found = pygame.font.match_font(FALLBACK_FONTS)
        self._fallback = pygame.font.Font(found, size) if found else None
        # A glyph the font lacks renders as its .notdef box, so it shares these metrics.
        self._notdef = self._main.metrics("\uffff")[0]

    def _pick(self, text: str) -> Any:
        if self._fallback is not None and any(
            m is None or m == self._notdef for m in self._main.metrics(text)
        ):
            return self._fallback
        return self._main

    def render(self, text: str, color: tuple[int, int, int]) -> Any:
        return self._pick(text).render(text, True, color)

    def size(self, text: str) -> tuple[int, int]:
        width, height = self._pick(text).size(text)
        return int(width), int(height)

    @property
    def linesize(self) -> int:
        return int(self._main.get_linesize())


def run(config: AppConfig, config_path: Path, *, fullscreen: bool = False) -> None:
    """Open the settings window and block until the user quits."""
    import pygame
    from pygame._sdl2 import controller

    def save(updated: AppConfig) -> None:
        save_config(updated, config_path)

    state = SettingsScreen(config, save)
    pygame.init()
    controller.init()
    pygame.key.set_repeat(350, 60)
    flags = pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
    surface = pygame.display.set_mode((1280, 720), flags)
    pygame.display.set_caption("Bifrost")
    pygame.mouse.set_visible(not fullscreen)
    try:
        pygame.scrap.init()
    except pygame.error:
        pass
    font, small = UiFont(pygame, 30), UiFont(pygame, 24)
    controllers: dict[int, Any] = {}
    stick = _StickRepeater()

    def open_controller(index: int) -> None:
        if controller.is_controller(index):
            pad = controller.Controller(index)
            controllers[pad.as_joystick().get_instance_id()] = pad

    for index in range(controller.get_count()):
        open_controller(index)

    dirty = True
    while not state.done:
        if dirty:
            layout = _Layout()
            _draw(pygame, surface, font, small, state, layout)
            pygame.display.flip()
            dirty = False
        # Sleep until something happens; poll quickly only while the stick is tilted.
        events = [pygame.event.wait(STICK_POLL_MS if stick.active else 0)]
        events += pygame.event.get()
        for raw in events:
            if raw.type == pygame.NOEVENT:
                continue
            dirty = True
            if raw.type == pygame.QUIT:
                state.done = True
            elif raw.type == pygame.CONTROLLERDEVICEADDED:
                open_controller(raw.device_index)
            elif raw.type == pygame.CONTROLLERDEVICEREMOVED:
                controllers.pop(raw.instance_id, None)
            else:
                for event in _translate(pygame, raw, state, layout, stick):
                    state.handle(event)
        for event in stick.due(pygame.time.get_ticks()):
            state.handle(event)
            dirty = True
    pygame.quit()


class _Layout:
    """Hit rectangles recorded while drawing, used to resolve mouse events."""

    def __init__(self) -> None:
        self.sections: list[Any] = []
        self.rows: list[tuple[int, Any]] = []  # (row index, rect)
        self.keys: list[tuple[int, int, Any]] = []  # (osk row, osk col, rect)


class _StickRepeater:
    """Turns analog stick tilt into repeated directional events."""

    def __init__(self) -> None:
        self._axes = [0.0, 0.0]
        self._last = 0

    @property
    def active(self) -> bool:
        return max(abs(self._axes[0]), abs(self._axes[1])) >= STICK_DEADZONE

    def move(self, axis: int, value: float) -> None:
        if axis in (0, 1):
            self._axes[axis] = value

    def due(self, now: int) -> list[Event]:
        x, y = self._axes
        if not self.active:
            self._last = 0
            return []
        if self._last and now - self._last < STICK_REPEAT_MS:
            return []
        self._last = now
        if abs(x) > abs(y):
            return [Event(Action.RIGHT if x > 0 else Action.LEFT)]
        return [Event(Action.DOWN if y > 0 else Action.UP)]


def _clipboard_text(pygame: Any) -> str:
    """Clipboard contents as one line of text; empty when unavailable."""
    try:
        data = pygame.scrap.get("text/plain;charset=utf-8")
    except (pygame.error, AttributeError):
        return ""
    if not data:
        return ""
    text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
    return "".join(text.replace("\x00", "").splitlines()).strip()


def _translate(
    pygame: Any, raw: Any, state: SettingsScreen, layout: _Layout, stick: _StickRepeater
) -> list[Event]:
    if raw.type == pygame.KEYDOWN:
        keys = {
            pygame.K_UP: Action.UP,
            pygame.K_DOWN: Action.DOWN,
            pygame.K_LEFT: Action.LEFT,
            pygame.K_RIGHT: Action.RIGHT,
            pygame.K_RETURN: Action.CONFIRM,
            pygame.K_KP_ENTER: Action.CONFIRM,
            pygame.K_ESCAPE: Action.BACK,
            pygame.K_BACKSPACE: Action.BACKSPACE,
        }
        if raw.key in keys:
            return [Event(keys[raw.key])]
        if state.mode != "edit":
            return []
        pasting = (raw.key == pygame.K_v and raw.mod & pygame.KMOD_CTRL) or (
            raw.key == pygame.K_INSERT and raw.mod & pygame.KMOD_SHIFT
        )
        if pasting:
            pasted = _clipboard_text(pygame)
            return [Event(Action.TEXT, pasted)] if pasted else []
        if raw.unicode and raw.unicode.isprintable():
            return [Event(Action.TEXT, raw.unicode)]
        return []
    if raw.type == pygame.CONTROLLERBUTTONDOWN:
        buttons = {
            pygame.CONTROLLER_BUTTON_DPAD_UP: Action.UP,
            pygame.CONTROLLER_BUTTON_DPAD_DOWN: Action.DOWN,
            pygame.CONTROLLER_BUTTON_DPAD_LEFT: Action.LEFT,
            pygame.CONTROLLER_BUTTON_DPAD_RIGHT: Action.RIGHT,
            pygame.CONTROLLER_BUTTON_A: Action.ACCEPT,
            pygame.CONTROLLER_BUTTON_B: Action.BACK,
            pygame.CONTROLLER_BUTTON_START: Action.CONFIRM,
        }
        return [Event(buttons[raw.button])] if raw.button in buttons else []
    if raw.type == pygame.CONTROLLERAXISMOTION:
        axes = {pygame.CONTROLLER_AXIS_LEFTX: 0, pygame.CONTROLLER_AXIS_LEFTY: 1}
        if raw.axis in axes:
            stick.move(axes[raw.axis], raw.value / 32768)
        return []
    if raw.type == pygame.MOUSEWHEEL:
        return [Event(Action.UP if raw.y > 0 else Action.DOWN)]
    if raw.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
        return _mouse(pygame, raw, state, layout)
    return []


def _mouse(pygame: Any, raw: Any, state: SettingsScreen, layout: _Layout) -> list[Event]:
    click = raw.type == pygame.MOUSEBUTTONDOWN
    if click and raw.button != 1:
        return [Event(Action.BACK)] if raw.button == 3 else []
    if state.mode == "edit":
        if click:
            for row, col, rect in layout.keys:
                if rect.collidepoint(raw.pos):
                    state.click_osk(row, col)
        return []
    for index, rect in enumerate(layout.sections):
        if rect.collidepoint(raw.pos):
            state.focus_section(index)
            return [Event(Action.ACCEPT)] if click else []
    for index, rect in layout.rows:
        if rect.collidepoint(raw.pos):
            state.focus_row(index)
            return [Event(Action.ACCEPT)] if click else []
    return []


def _draw(
    pygame: Any, surface: Any, font: UiFont, small: UiFont, state: SettingsScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    surface.fill(BG)
    side, top, bottom = 260, 20, 100
    line = font.linesize + 14

    # Sections
    for index, name in enumerate(state.sections):
        rect = pygame.Rect(20, top + index * line, side - 20, line - 6)
        layout.sections.append(rect)
        if index == state.section:
            color = FOCUS if state.mode == "sections" else DIM_FOCUS
            pygame.draw.rect(surface, color, rect, border_radius=8)
        surface.blit(font.render(name, TEXT), (rect.x + 14, rect.y + 8))

    # Rows, scrolled so the focused row stays visible
    area = pygame.Rect(side + 20, top, width - side - 40, height - top - bottom)
    visible = max(1, area.height // line)
    first = max(0, min(state.row - visible // 2, len(state.rows) - visible))
    for position, setting in enumerate(state.rows[first : first + visible]):
        index = first + position
        rect = pygame.Rect(area.x, area.y + position * line, area.width, line - 6)
        layout.rows.append((index, rect))
        if index == state.row and state.mode != "sections":
            pygame.draw.rect(surface, FOCUS, rect, border_radius=8)
        else:
            pygame.draw.rect(surface, PANEL, rect, border_radius=8)
        surface.blit(font.render(setting.label, TEXT), (rect.x + 14, rect.y + 8))
        value = display_text(state.config, setting) or "-"
        color = TEXT if index == state.row else MUTED
        rendered = font.render(_fit(font, value, rect.width // 2 - 20), color)
        surface.blit(rendered, (rect.right - rendered.get_width() - 14, rect.y + 8))

    # Description, error, hints
    info_y = height - bottom + 10
    if state.error:
        surface.blit(small.render(state.error, ERROR), (side + 20, info_y))
    elif state.mode != "sections":
        surface.blit(small.render(state.current.description, MUTED), (side + 20, info_y))
    surface.blit(small.render(_HINTS[state.mode], MUTED), (side + 20, height - 34))

    if state.mode == "edit":
        _draw_editor(pygame, surface, font, small, state, layout)


def _fit(font: UiFont, text: str, max_width: int) -> str:
    if font.size(text)[0] <= max_width:
        return text
    while text and font.size("…" + text)[0] > max_width:
        text = text[1:]
    return "…" + text


def _draw_editor(
    pygame: Any, surface: Any, font: UiFont, small: UiFont, state: SettingsScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    layout.sections.clear()
    layout.rows.clear()
    panel = pygame.Rect(width // 2 - 420, height // 2 - 250, 840, 500)
    pygame.draw.rect(surface, BG, panel, border_radius=12)
    pygame.draw.rect(surface, FOCUS, panel, width=2, border_radius=12)
    surface.blit(font.render(state.current.key, MUTED), (panel.x + 20, panel.y + 16))
    shown = "•" * len(state.buffer) if state.current.kind == "secret" else state.buffer
    box = pygame.Rect(panel.x + 20, panel.y + 56, panel.width - 40, 48)
    pygame.draw.rect(surface, PANEL, box, border_radius=8)
    typed = font.render(_fit(font, shown, box.width - 40), TEXT)
    surface.blit(typed, (box.x + 12, box.y + 8))
    caret_x = box.x + 14 + typed.get_width()
    pygame.draw.rect(surface, TEXT, pygame.Rect(caret_x, box.y + 8, 2, box.height - 16))
    if state.error:
        surface.blit(small.render(state.error, ERROR), (panel.x + 20, panel.y + 112))

    grid = state.osk_grid
    key_h, gap, top = 48, 6, panel.y + 150
    for row_index, keys in enumerate(grid):
        key_w = (panel.width - 40 - gap * (len(keys) - 1)) // len(keys)
        for col_index, key in enumerate(keys):
            x = panel.x + 20 + col_index * (key_w + gap)
            rect = pygame.Rect(x, top + row_index * (key_h + gap), key_w, key_h)
            layout.keys.append((row_index, col_index, rect))
            focused = (row_index, col_index) == (state.osk_row, state.osk_col)
            pygame.draw.rect(surface, FOCUS if focused else PANEL, rect, border_radius=6)
            label = font.render(key, TEXT)
            surface.blit(label, label.get_rect(center=rect.center))
