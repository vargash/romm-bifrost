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

JOY_A, JOY_B, JOY_START = 0, 1, 7
STICK_DEADZONE = 0.6
STICK_REPEAT_MS = 180

_HINTS = {
    "sections": "A/Enter open   B/Esc quit   ↑↓ move",
    "rows": "A/Enter change   ←/B back   ↑↓ move   ←→ adjust",
    "edit": "A type key   Start/Enter save   B cancel",
}


def pygame_available() -> bool:
    try:
        import pygame  # noqa: F401
    except ImportError:
        return False
    return True


def run(config: AppConfig, config_path: Path, *, fullscreen: bool = False) -> None:
    """Open the settings window and block until the user quits."""
    import pygame

    def save(updated: AppConfig) -> None:
        save_config(updated, config_path)

    screen_state = SettingsScreen(config, save)
    pygame.init()
    pygame.key.set_repeat(350, 60)
    flags = pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
    surface = pygame.display.set_mode((1280, 720), flags)
    pygame.display.set_caption("Bifrost")
    pygame.mouse.set_visible(not fullscreen)
    font = pygame.font.Font(None, 30)
    small = pygame.font.Font(None, 24)
    joysticks: dict[int, Any] = {}
    stick = _StickRepeater()
    clock = pygame.time.Clock()

    while not screen_state.done:
        layout = _Layout()
        _draw(pygame, surface, font, small, screen_state, layout)
        pygame.display.flip()
        for raw in pygame.event.get():
            if raw.type == pygame.QUIT:
                screen_state.done = True
            elif raw.type == pygame.JOYDEVICEADDED:
                joy = pygame.joystick.Joystick(raw.device_index)
                joysticks[joy.get_instance_id()] = joy
            elif raw.type == pygame.JOYDEVICEREMOVED:
                joysticks.pop(raw.instance_id, None)
            else:
                for event in _translate(pygame, raw, screen_state, layout, stick):
                    screen_state.handle(event)
        for event in stick.due(pygame.time.get_ticks()):
            screen_state.handle(event)
        clock.tick(60)
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

    def move(self, axis: int, value: float) -> None:
        if axis in (0, 1):
            self._axes[axis] = value

    def due(self, now: int) -> list[Event]:
        x, y = self._axes
        if max(abs(x), abs(y)) < STICK_DEADZONE:
            self._last = 0
            return []
        if self._last and now - self._last < STICK_REPEAT_MS:
            return []
        self._last = now
        if abs(x) > abs(y):
            return [Event(Action.RIGHT if x > 0 else Action.LEFT)]
        return [Event(Action.DOWN if y > 0 else Action.UP)]


def _translate(
    pygame: Any, raw: Any, state: SettingsScreen, layout: _Layout, stick: _StickRepeater
) -> list[Event]:
    editing = state.mode == "edit"
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
        if editing and raw.unicode and raw.unicode.isprintable():
            return [Event(Action.TEXT, raw.unicode)]
        return []
    if raw.type == pygame.JOYHATMOTION:
        x, y = raw.value
        if x or y:
            return [Event(_hat_action(x, y))]
        return []
    if raw.type == pygame.JOYAXISMOTION:
        stick.move(raw.axis, raw.value)
        return []
    if raw.type == pygame.JOYBUTTONDOWN:
        if raw.button == JOY_A:
            return [Event(Action.ACCEPT)]
        if raw.button == JOY_B:
            return [Event(Action.BACK)]
        if raw.button == JOY_START:
            return [Event(Action.CONFIRM)]
        return []
    if raw.type == pygame.MOUSEWHEEL:
        return [Event(Action.UP if raw.y > 0 else Action.DOWN)]
    if raw.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
        return _mouse(pygame, raw, state, layout)
    return []


def _hat_action(x: int, y: int) -> Action:
    if y:
        return Action.UP if y > 0 else Action.DOWN
    return Action.RIGHT if x > 0 else Action.LEFT


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
    pygame: Any, surface: Any, font: Any, small: Any, state: SettingsScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    surface.fill(BG)
    side, top, bottom = 260, 20, 100
    line = font.get_linesize() + 14

    # Sections
    for index, name in enumerate(state.sections):
        rect = pygame.Rect(20, top + index * line, side - 20, line - 6)
        layout.sections.append(rect)
        if index == state.section:
            color = FOCUS if state.mode == "sections" else DIM_FOCUS
            pygame.draw.rect(surface, color, rect, border_radius=8)
        surface.blit(font.render(name, True, TEXT), (rect.x + 14, rect.y + 8))

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
        surface.blit(font.render(setting.label, True, TEXT), (rect.x + 14, rect.y + 8))
        value = display_text(state.config, setting) or "—"
        color = TEXT if index == state.row else MUTED
        rendered = font.render(_fit(font, value, rect.width // 2 - 20), True, color)
        surface.blit(rendered, (rect.right - rendered.get_width() - 14, rect.y + 8))

    # Description, error, hints
    info_y = height - bottom + 10
    if state.error:
        surface.blit(small.render(state.error, True, ERROR), (side + 20, info_y))
    elif state.mode != "sections":
        surface.blit(small.render(state.current.description, True, MUTED), (side + 20, info_y))
    surface.blit(small.render(_HINTS[state.mode], True, MUTED), (side + 20, height - 34))

    if state.mode == "edit":
        _draw_editor(pygame, surface, font, small, state, layout)


def _fit(font: Any, text: str, max_width: int) -> str:
    if font.size(text)[0] <= max_width:
        return text
    while text and font.size("…" + text)[0] > max_width:
        text = text[1:]
    return "…" + text


def _draw_editor(
    pygame: Any, surface: Any, font: Any, small: Any, state: SettingsScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    layout.sections.clear()
    layout.rows.clear()
    panel = pygame.Rect(width // 2 - 420, height // 2 - 250, 840, 500)
    pygame.draw.rect(surface, BG, panel, border_radius=12)
    pygame.draw.rect(surface, FOCUS, panel, width=2, border_radius=12)
    surface.blit(font.render(state.current.key, True, MUTED), (panel.x + 20, panel.y + 16))
    shown = "•" * len(state.buffer) if state.current.kind == "secret" else state.buffer
    box = pygame.Rect(panel.x + 20, panel.y + 56, panel.width - 40, 48)
    pygame.draw.rect(surface, PANEL, box, border_radius=8)
    typed = font.render(_fit(font, shown, box.width - 24) + "▏", True, TEXT)
    surface.blit(typed, (box.x + 12, box.y + 12))
    if state.error:
        surface.blit(small.render(state.error, True, ERROR), (panel.x + 20, panel.y + 112))

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
            label = font.render(key, True, TEXT)
            surface.blit(label, label.get_rect(center=rect.center))
