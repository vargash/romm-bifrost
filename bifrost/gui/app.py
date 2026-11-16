"""pygame front end: window, input mapping and drawing for the GUI screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bifrost.config import AppConfig, save_config
from bifrost.gui import theme as t
from bifrost.gui.actions_screen import ActionScreen, Command
from bifrost.gui.blocks import Text
from bifrost.gui.catalog import build_screens
from bifrost.gui.home import HomeScreen
from bifrost.gui.inputs import Action, Event
from bifrost.gui.logview import layout_blocks, wrap
from bifrost.gui.osk import OnScreenKeyboard
from bifrost.gui.runner import CommandRunner, cli_argv
from bifrost.gui.schema import display_text
from bifrost.gui.settings_screen import SettingsScreen

Screen = HomeScreen | SettingsScreen | ActionScreen

STICK_DEADZONE = 0.6
STICK_REPEAT_MS = 180
STICK_POLL_MS = 30
LOG_POLL_MS = 100

FONT_FILE = Path(__file__).resolve().parent.parent / "data" / "fonts" / "NotoSans-Regular.ttf"
# Tried in order when the bundled font lacks a glyph (CJK titles and the like).
MONO_FONTS = "dejavusansmono,liberationmono,notosansmono,freemono"
FALLBACK_FONTS = "notosanscjksc,notosanscjk,notosanssc,wenquanyi,droidsansfallback,unifont"

# Footer hints per context: (button badge, what it does). Keyboard equivalents follow the dot.
_HINTS: dict[str, list[tuple[str, str]]] = {
    "home": [("A", "Open · Enter"), ("B", "Quit · Esc")],
    "sections": [("A", "Open · Enter"), ("B", "Home · Esc")],
    "rows": [("A", "Change · Enter"), ("<  >", "Adjust"), ("B", "Back · Esc")],
    "edit": [("A", "Key · Enter"), ("Start", "Save · Enter"), ("B", "Cancel · Esc")],
    "list": [("A", "Run · Enter"), ("B", "Back · Esc")],
    "confirm": [("A", "Run · Enter"), ("B", "Cancel · Esc")],
    "running": [("Up/Down", "Scroll"), ("< >", "Pan"), ("B", "Cancel · Esc")],
    "done": [("Up/Down", "Scroll"), ("< >", "Pan"), ("B", "Back · Esc")],
    "ask_confirm": [("A", "Yes · Enter"), ("B", "No · Esc")],
    "ask_choice": [("< >", "Choose"), ("A", "Select · Enter"), ("B", "Default · Esc")],
}


def pygame_available() -> bool:
    try:
        import pygame  # noqa: F401
    except ImportError:
        return False
    return True


class UiFont:
    """The bundled font with a system fallback for glyphs it does not contain."""

    def __init__(self, pygame: Any, size: int, *, monospace: bool = False) -> None:
        mono = pygame.font.match_font(MONO_FONTS) if monospace else None
        self._main = pygame.font.Font(mono or str(FONT_FILE), size)
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


@dataclass
class _Fonts:
    title: UiFont
    body: UiFont
    note: UiFont
    log: UiFont  # monospace when the system has one, so command tables line up


def run(config: AppConfig, config_path: Path, *, fullscreen: bool = False) -> None:
    """Open the window and block until the user quits."""
    import pygame
    from pygame._sdl2 import controller

    def save(updated: AppConfig) -> None:
        save_config(updated, config_path)

    def start(command: Command) -> CommandRunner:
        args = command.args
        return CommandRunner(cli_argv(args, config_path if command.takes_config else None))

    home = HomeScreen()
    settings = SettingsScreen(config, save)
    screens = build_screens(start)
    active: Screen = home
    previous: Screen = home  # where the settings screen returns to
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
    fonts = _Fonts(
        UiFont(pygame, t.TITLE_SIZE),
        UiFont(pygame, t.BODY_SIZE),
        UiFont(pygame, t.NOTE_SIZE),
        UiFont(pygame, t.NOTE_SIZE, monospace=True),
    )
    controllers: dict[int, Any] = {}
    stick = _StickRepeater()

    def open_controller(index: int) -> None:
        if controller.is_controller(index):
            pad = controller.Controller(index)
            controllers[pad.as_joystick().get_instance_id()] = pad

    for index in range(controller.get_count()):
        open_controller(index)

    def go(target: str, origin: Screen) -> None:
        nonlocal active, previous
        previous = origin
        if target.startswith("settings"):
            settings.restrict(target.partition(":")[2] or None)
            active = settings
        else:
            active = screens[target]

    def dispatch(event: Event) -> None:
        nonlocal active
        active.handle(event)
        if isinstance(active, HomeScreen):
            if active.opened:
                target, active.opened = active.opened, None
                go(target, home)
        elif isinstance(active, ActionScreen):
            if active.opened:
                target, active.opened = active.opened, None
                go(target, active)
            elif active.done:
                active.done = False
                active = home
        elif settings.done:
            settings.done = False
            settings.restrict(None)
            active = previous

    layout = _Layout()
    dirty = True
    while not home.done:
        if dirty:
            layout = _Layout()
            _draw(pygame, surface, fonts, active, layout)
            pygame.display.flip()
            dirty = False
        # Sleep until something happens; poll quickly only while the stick is tilted.
        busy = isinstance(active, ActionScreen) and active.busy
        timeout = STICK_POLL_MS if stick.active else LOG_POLL_MS if busy else 0
        events = [pygame.event.wait(timeout)]
        events += pygame.event.get()
        if isinstance(active, ActionScreen) and active.tick():
            dirty = True
        for raw in events:
            if raw.type == pygame.NOEVENT:
                continue
            dirty = True
            if raw.type == pygame.QUIT:
                home.done = True
            elif raw.type == pygame.CONTROLLERDEVICEADDED:
                open_controller(raw.device_index)
            elif raw.type == pygame.CONTROLLERDEVICEREMOVED:
                controllers.pop(raw.instance_id, None)
            else:
                for event in _translate(pygame, raw, active, layout, stick):
                    dispatch(event)
        for event in stick.due(pygame.time.get_ticks()):
            dispatch(event)
            dirty = True
    for screen in screens.values():
        screen.shutdown()
    pygame.quit()


class _Layout:
    """Hit rectangles recorded while drawing, used to resolve mouse events."""

    def __init__(self) -> None:
        self.tiles: list[Any] = []
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
    pygame: Any,
    raw: Any,
    state: Screen,
    layout: _Layout,
    stick: _StickRepeater,
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


def _mouse(pygame: Any, raw: Any, state: Screen, layout: _Layout) -> list[Event]:
    click = raw.type == pygame.MOUSEBUTTONDOWN
    if click and raw.button != 1:
        return [Event(Action.BACK)] if raw.button == 3 else []
    if isinstance(state, HomeScreen):
        for index, rect in enumerate(layout.tiles):
            if rect.collidepoint(raw.pos):
                state.focus(index)
                return [Event(Action.ACCEPT)] if click else []
        return []
    if isinstance(state, ActionScreen):
        if state.pending is not None:
            if click and state.pending.kind == "text":
                for row, col, rect in layout.keys:
                    if rect.collidepoint(raw.pos):
                        state.click_osk(row, col)
            return []
        for index, rect in layout.rows:
            if rect.collidepoint(raw.pos):
                state.focus_row(index)
                return [Event(Action.ACCEPT)] if click else []
        return []
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
    pygame: Any,
    surface: Any,
    fonts: _Fonts,
    state: Screen,
    layout: _Layout,
) -> None:
    surface.fill(t.BG)
    if isinstance(state, ActionScreen):
        _draw_actions(pygame, surface, fonts, state, layout)
        return
    if isinstance(state, HomeScreen):
        _draw_header(pygame, surface, fonts, "Home", "", t.MUTED)
        _draw_home(pygame, surface, fonts, state, layout)
        _draw_footer(pygame, surface, fonts, _HINTS["home"])
        return
    status, color = ("Saved", t.OK) if state.saved else ("", t.MUTED)
    crumb = f"Settings / {state.sections[state.section]}"
    _draw_header(pygame, surface, fonts, crumb, status, color)
    _draw_settings(pygame, surface, fonts, state, layout)
    _draw_footer(pygame, surface, fonts, _HINTS[state.mode])
    if state.mode == "edit":
        _draw_editor(pygame, surface, fonts, state, layout)


def _draw_header(
    pygame: Any, surface: Any, fonts: _Fonts, crumb: str, status: str, status_color: t.Color
) -> None:
    width = surface.get_width()
    pygame.draw.rect(surface, t.SURFACE, pygame.Rect(0, 0, width, t.HEADER_H))
    pygame.draw.line(surface, t.BORDER, (0, t.HEADER_H), (width, t.HEADER_H))
    brand = fonts.title.render("Bifrost", t.TEXT)
    y = (t.HEADER_H - brand.get_height()) // 2
    surface.blit(brand, (t.MARGIN, y))
    x = t.MARGIN + brand.get_width() + t.GAP * 2
    pygame.draw.line(surface, t.BORDER, (x - t.GAP, 18), (x - t.GAP, t.HEADER_H - 18))
    label = fonts.body.render(crumb, t.MUTED)
    surface.blit(label, (x, (t.HEADER_H - label.get_height()) // 2))
    if status:
        tag = fonts.note.render(status, status_color)
        tag_y = (t.HEADER_H - tag.get_height()) // 2
        surface.blit(tag, (width - t.MARGIN - tag.get_width(), tag_y))


def _draw_footer(
    pygame: Any, surface: Any, fonts: _Fonts, hints: list[tuple[str, str]]
) -> None:
    width, height = surface.get_size()
    top = height - t.FOOTER_H
    pygame.draw.rect(surface, t.SURFACE, pygame.Rect(0, top, width, t.FOOTER_H))
    pygame.draw.line(surface, t.BORDER, (0, top), (width, top))
    x = t.MARGIN
    for badge, label in hints:
        x += _badge(pygame, surface, fonts.note, badge, x, top + t.FOOTER_H // 2) + 8
        text = fonts.note.render(label, t.MUTED)
        surface.blit(text, (x, top + (t.FOOTER_H - text.get_height()) // 2))
        x += text.get_width() + t.GAP * 3


def _badge(pygame: Any, surface: Any, font: UiFont, text: str, x: int, cy: int) -> int:
    """Draw a button badge centred on cy; returns its width."""
    label = font.render(text, t.TEXT)
    height = label.get_height() + 6
    width = max(height, label.get_width() + 16)
    rect = pygame.Rect(x, cy - height // 2, width, height)
    pygame.draw.rect(surface, t.SURFACE_HI, rect, border_radius=height // 2)
    pygame.draw.rect(surface, t.BORDER, rect, width=1, border_radius=height // 2)
    surface.blit(label, label.get_rect(center=rect.center))
    return int(width)


def _draw_home(
    pygame: Any, surface: Any, fonts: _Fonts, state: HomeScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    area = pygame.Rect(
        t.MARGIN,
        t.HEADER_H + t.MARGIN,
        width - 2 * t.MARGIN,
        height - t.HEADER_H - t.FOOTER_H - 2 * t.MARGIN,
    )
    cols = state.columns
    tile_w = (area.width - t.GAP * (cols - 1)) // cols
    tile_h = 170
    for index, tile in enumerate(state.tiles):
        row, col = divmod(index, cols)
        rect = pygame.Rect(
            area.x + col * (tile_w + t.GAP), area.y + row * (tile_h + t.GAP), tile_w, tile_h
        )
        layout.tiles.append(rect)
        focused = index == state.index
        pygame.draw.rect(
            surface, t.SURFACE_HI if focused else t.SURFACE, rect, border_radius=t.RADIUS
        )
        pygame.draw.rect(
            surface,
            t.ACCENT if focused else t.BORDER,
            rect,
            width=3 if focused else 1,
            border_radius=t.RADIUS,
        )
        label_color = t.TEXT if tile.enabled else t.FAINT
        surface.blit(fonts.title.render(tile.label, label_color), (rect.x + 22, rect.y + 20))
        note = _fit_end(fonts.note, tile.description, rect.width - 44)
        note_color = t.MUTED if tile.enabled else t.FAINT
        surface.blit(fonts.note.render(note, note_color), (rect.x + 22, rect.y + 84))
        if not tile.enabled:
            soon = fonts.note.render("Coming soon", t.FAINT)
            surface.blit(soon, (rect.x + 22, rect.bottom - soon.get_height() - 14))


def _draw_settings(
    pygame: Any, surface: Any, fonts: _Fonts, state: SettingsScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    top = t.HEADER_H + t.MARGIN
    bottom = height - t.FOOTER_H - 44  # room for the message line
    side_w = 240

    # Sections
    item_h = fonts.body.linesize + 16
    for index, name in enumerate(state.sections):
        rect = pygame.Rect(t.MARGIN, top + index * (item_h + 4), side_w, item_h)
        layout.sections.append(rect)
        if index == state.section:
            fill = t.ACCENT_DIM if state.mode == "sections" else t.SURFACE_HI
            pygame.draw.rect(surface, fill, rect, border_radius=t.RADIUS)
            if state.mode == "sections":
                pygame.draw.rect(surface, t.ACCENT, rect, width=2, border_radius=t.RADIUS)
        surface.blit(fonts.body.render(name, t.TEXT), (rect.x + 16, rect.y + 8))

    # Rows: label with its description underneath, value on the right
    rows_x = t.MARGIN + side_w + t.GAP * 2
    area = pygame.Rect(rows_x, top, width - rows_x - t.MARGIN - 14, bottom - top)
    row_h = fonts.body.linesize + fonts.note.linesize + 22
    visible = max(1, area.height // (row_h + 6))
    total = len(state.rows)
    first = max(0, min(state.row - visible // 2, total - visible))
    for position, setting in enumerate(state.rows[first : first + visible]):
        index = first + position
        rect = pygame.Rect(area.x, area.y + position * (row_h + 6), area.width, row_h)
        layout.rows.append((index, rect))
        focused = index == state.row and state.mode != "sections"
        pygame.draw.rect(
            surface, t.SURFACE_HI if focused else t.SURFACE, rect, border_radius=t.RADIUS
        )
        if focused:
            pygame.draw.rect(surface, t.ACCENT, rect, width=2, border_radius=t.RADIUS)
        value = display_text(state.config, setting) or "-"
        value_w = rect.width // 2 - 24
        value_color = t.TEXT if focused else t.MUTED
        rendered = fonts.body.render(_fit(fonts.body, value, value_w), value_color)
        surface.blit(rendered, (rect.right - rendered.get_width() - 16, rect.y + 10))
        label_w = rect.width - value_w - 56
        label = _fit_end(fonts.body, setting.label, label_w)
        surface.blit(fonts.body.render(label, t.TEXT), (rect.x + 16, rect.y + 8))
        note = _fit_end(fonts.note, setting.description, rect.width - 32)
        note_y = rect.y + 12 + fonts.body.linesize
        surface.blit(fonts.note.render(note, t.MUTED), (rect.x + 16, note_y))

    if total > visible:
        track = pygame.Rect(area.right + 6, area.y, 6, area.height)
        pygame.draw.rect(surface, t.SURFACE, track, border_radius=3)
        thumb_h = max(24, track.height * visible // total)
        thumb_y = track.y + (track.height - thumb_h) * first // max(1, total - visible)
        thumb = pygame.Rect(track.x, thumb_y, 6, thumb_h)
        pygame.draw.rect(surface, t.FAINT, thumb, border_radius=3)

    if state.error:
        text = _fit_end(fonts.note, state.error, width - 2 * t.MARGIN)
        message = fonts.note.render(text, t.ERROR)
        surface.blit(message, (t.MARGIN, height - t.FOOTER_H - 34))


def _draw_actions(
    pygame: Any, surface: Any, fonts: _Fonts, state: ActionScreen, layout: _Layout
) -> None:
    width, height = surface.get_size()
    command = state.command
    running = state.mode in ("running", "done")
    crumb = f"{state.title} / {command.label}" if running and command else state.title
    status, color = "", t.MUTED
    if state.mode == "running":
        status, color = ("Cancelling..." if state.cancelled else "Running..."), t.MUTED
    elif state.mode == "done":
        ok = state.exit_code == 0
        status = "Done" if ok else f"Failed (exit {state.exit_code})"
        color = t.OK if ok else t.ERROR
    _draw_header(pygame, surface, fonts, crumb, status, color)
    prompt = state.pending
    hints: str = state.mode
    if prompt is not None:
        hints = {"confirm": "ask_confirm", "choice": "ask_choice"}.get(prompt.kind, "edit")
    _draw_footer(pygame, surface, fonts, _HINTS[hints])

    top = t.HEADER_H + t.MARGIN
    if running:
        _draw_log(pygame, surface, fonts, state, pygame.Rect(
            t.MARGIN, top, width - 2 * t.MARGIN, height - t.FOOTER_H - t.MARGIN - top
        ))
        if prompt is not None:
            _draw_prompt(pygame, surface, fonts, state, layout)
        return

    area = pygame.Rect(t.MARGIN, top, width - 2 * t.MARGIN - 14, height - t.FOOTER_H - top - 20)
    row_h = fonts.body.linesize + fonts.note.linesize + 22
    visible = max(1, area.height // (row_h + 6))
    total = len(state.entries)
    first = max(0, min(state.index - visible // 2, total - visible))
    for position, entry in enumerate(state.entries[first : first + visible]):
        index = first + position
        rect = pygame.Rect(area.x, area.y + position * (row_h + 6), area.width, row_h)
        layout.rows.append((index, rect))
        focused = index == state.index
        pygame.draw.rect(
            surface, t.SURFACE_HI if focused else t.SURFACE, rect, border_radius=t.RADIUS
        )
        if focused:
            pygame.draw.rect(surface, t.ACCENT, rect, width=2, border_radius=t.RADIUS)
        label = _fit_end(fonts.body, entry.label, rect.width - 32)
        surface.blit(fonts.body.render(label, t.TEXT), (rect.x + 16, rect.y + 8))
        note = _fit_end(fonts.note, entry.description, rect.width - 32)
        note_y = rect.y + 12 + fonts.body.linesize
        surface.blit(fonts.note.render(note, t.MUTED), (rect.x + 16, note_y))
    if total > visible:
        _scrollbar(pygame, surface, pygame.Rect(area.right + 6, area.y, 6, area.height),
                   visible, total, first)
    if state.mode == "confirm" and command is not None:
        _draw_confirm(pygame, surface, fonts, command.label, command.description)


def _scrollbar(pygame: Any, surface: Any, track: Any, visible: int, total: int, first: int) -> None:
    pygame.draw.rect(surface, t.SURFACE, track, border_radius=3)
    thumb_h = max(24, track.height * visible // total)
    thumb_y = track.y + (track.height - thumb_h) * first // max(1, total - visible)
    thumb = pygame.Rect(track.x, thumb_y, track.width, thumb_h)
    pygame.draw.rect(surface, t.FAINT, thumb, border_radius=3)


def _draw_dialog(
    pygame: Any,
    surface: Any,
    fonts: _Fonts,
    title: str,
    body: list[str],
    buttons: list[tuple[str, str]],
    height: int = 250,
) -> Any:
    """Dimmed backdrop with a centred panel; returns the panel rect."""
    width, full_height = surface.get_size()
    shade = pygame.Surface((width, full_height), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 150))
    surface.blit(shade, (0, 0))
    panel = pygame.Rect(0, 0, min(760, width - 2 * t.MARGIN), height)
    panel.center = (width // 2, full_height // 2)
    pygame.draw.rect(surface, t.SURFACE, panel, border_radius=t.RADIUS + 2)
    pygame.draw.rect(surface, t.ACCENT, panel, width=2, border_radius=t.RADIUS + 2)
    surface.blit(fonts.title.render(title, t.TEXT), (panel.x + 28, panel.y + 22))
    y = panel.y + 88
    for index, line in enumerate(body):
        font = fonts.body if index == 0 else fonts.note
        color = t.TEXT if index == 0 else t.MUTED
        surface.blit(font.render(_fit_end(font, line, panel.width - 56), color), (panel.x + 28, y))
        y += font.linesize + 6
    x, cy = panel.x + 28, panel.bottom - 36
    for badge, label in buttons:
        x += _badge(pygame, surface, fonts.note, badge, x, cy) + 8
        x += surface.blit(fonts.note.render(label, t.TEXT), (x, cy - 12)).width + 36
    return panel


def _draw_confirm(pygame: Any, surface: Any, fonts: _Fonts, label: str, detail: str) -> None:
    _draw_dialog(
        pygame, surface, fonts, "Run this?", [label, detail], [("A", "Run"), ("B", "Cancel")]
    )


def _draw_prompt(
    pygame: Any, surface: Any, fonts: _Fonts, state: ActionScreen, layout: _Layout
) -> None:
    """The question the running command is waiting on."""
    prompt = state.pending
    if prompt is None:
        return
    measure = _measurer(fonts.body)
    lines = wrap(measure, prompt.question, min(760, surface.get_width() - 2 * t.MARGIN) - 56)
    if prompt.kind == "text":
        _draw_text_editor(
            pygame, surface, fonts, " ".join(lines), state.osk, prompt.secret, "", layout
        )
    elif prompt.kind == "confirm":
        _draw_dialog(
            pygame, surface, fonts, "Question", lines[:3], [("A", "Yes"), ("B", "No")], 260
        )
    else:
        panel = _draw_dialog(pygame, surface, fonts, "Question", lines[:3], [("A", "Select")], 290)
        x, y = panel.x + 28, panel.y + 170
        for index, choice in enumerate(prompt.choices):
            chip = fonts.body.render(choice, t.TEXT)
            rect = pygame.Rect(x, y, chip.get_width() + 32, chip.get_height() + 12)
            focused = index == state.choice
            fill = t.ACCENT_DIM if focused else t.SURFACE_HI
            pygame.draw.rect(surface, fill, rect, border_radius=8)
            if focused:
                pygame.draw.rect(surface, t.ACCENT, rect, width=2, border_radius=8)
            surface.blit(chip, (rect.x + 16, rect.y + 6))
            x = rect.right + 10


def _measurer(font: UiFont) -> Any:
    return lambda text: font.size(text)[0]


def _draw_log(pygame: Any, surface: Any, fonts: _Fonts, state: ActionScreen, area: Any) -> None:
    pygame.draw.rect(surface, t.SURFACE, area, border_radius=t.RADIUS)
    pygame.draw.rect(surface, t.BORDER, area, width=1, border_radius=t.RADIUS)
    inner = area.inflate(-32, -24)
    font = fonts.log
    line_h = font.linesize
    visible = max(1, inner.height // line_h)
    lines, widest = layout_blocks(list(state.lines)[-300:], _measurer(font), inner.width - 12)
    if not lines:
        lines = layout_blocks([Text("Starting...")], _measurer(font), inner.width)[0]
    state.scroll = min(state.scroll, max(0, len(lines) - visible))
    state.pan = min(state.pan, max(0, widest - inner.width))
    end = len(lines) - state.scroll
    previous_clip = surface.get_clip()
    surface.set_clip(inner)
    for position, line in enumerate(lines[max(0, end - visible) : end]):
        y = inner.y + position * line_h
        if line.kind == "text":
            surface.blit(font.render(line.text or " ", t.TEXT), (inner.x, y))
        elif line.kind == "title":
            surface.blit(font.render(line.text, t.TEXT), (inner.x - state.pan, y))
        elif line.kind in ("head", "row"):
            _draw_table_line(pygame, surface, font, line, inner.x - state.pan, y, line_h)
    surface.set_clip(previous_clip)
    if len(lines) > visible:
        track = pygame.Rect(area.right - 12, inner.y, 6, inner.height)
        _scrollbar(pygame, surface, track, visible, len(lines), max(0, end - visible))


def _draw_table_line(
    pygame: Any, surface: Any, font: UiFont, line: Any, x: int, y: int, line_h: int
) -> None:
    if line.kind == "head" or line.alt:
        band = pygame.Rect(x - 6, y, sum(line.widths), line_h)
        pygame.draw.rect(surface, t.SURFACE_HI if line.kind == "head" else t.BG, band)
    color = t.ACCENT if line.kind == "head" else t.TEXT
    for cell, width, right in zip(line.cells, line.widths, line.right, strict=False):
        text = font.render(cell, color)
        offset = width - 18 - text.get_width() if right else 0
        surface.blit(text, (x + offset, y))
        x += width


def _fit(font: UiFont, text: str, max_width: int) -> str:
    """Keep the tail of text, e.g. the end of a long path."""
    if font.size(text)[0] <= max_width:
        return text
    while text and font.size("…" + text)[0] > max_width:
        text = text[1:]
    return "…" + text


def _fit_end(font: UiFont, text: str, max_width: int) -> str:
    """Keep the head of text, e.g. the start of a description."""
    if font.size(text)[0] <= max_width:
        return text
    while text and font.size(text + "…")[0] > max_width:
        text = text[:-1]
    return text + "…"


def _draw_editor(
    pygame: Any, surface: Any, fonts: _Fonts, state: SettingsScreen, layout: _Layout
) -> None:
    layout.sections.clear()
    layout.rows.clear()
    setting = state.current
    secret = setting.kind == "secret"
    _draw_text_editor(pygame, surface, fonts, setting.key, state.osk, secret, state.error, layout)


def _draw_text_editor(
    pygame: Any,
    surface: Any,
    fonts: _Fonts,
    label: str,
    osk: OnScreenKeyboard,
    secret: bool,
    error: str,
    layout: _Layout,
) -> None:
    """Dimmed backdrop with the text box and on-screen keyboard."""
    width, height = surface.get_size()
    shade = pygame.Surface((width, height), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 150))
    surface.blit(shade, (0, 0))
    panel = pygame.Rect(0, 0, min(880, width - 2 * t.MARGIN), min(520, height - 2 * t.MARGIN))
    panel.center = (width // 2, height // 2)
    pygame.draw.rect(surface, t.SURFACE, panel, border_radius=t.RADIUS + 2)
    pygame.draw.rect(surface, t.BORDER, panel, width=1, border_radius=t.RADIUS + 2)
    surface.blit(
        fonts.note.render(_fit_end(fonts.note, label, panel.width - 48), t.MUTED),
        (panel.x + 24, panel.y + 18),
    )
    shown = "•" * len(osk.buffer) if secret else osk.buffer
    box = pygame.Rect(panel.x + 24, panel.y + 52, panel.width - 48, 52)
    pygame.draw.rect(surface, t.BG, box, border_radius=t.RADIUS)
    pygame.draw.rect(surface, t.ACCENT, box, width=2, border_radius=t.RADIUS)
    typed = fonts.body.render(_fit(fonts.body, shown, box.width - 44), t.TEXT)
    surface.blit(typed, (box.x + 14, box.y + (box.height - typed.get_height()) // 2))
    caret_x = box.x + 16 + typed.get_width()
    pygame.draw.rect(surface, t.TEXT, pygame.Rect(caret_x, box.y + 12, 2, box.height - 24))
    if error:
        message = fonts.note.render(_fit_end(fonts.note, error, box.width), t.ERROR)
        surface.blit(message, (box.x, box.bottom + 8))

    grid = osk.grid
    gap, key_h = 6, 50
    top = box.bottom + 44
    key_h = min(key_h, (panel.bottom - 24 - top - gap * (len(grid) - 1)) // len(grid))
    for row_index, keys in enumerate(grid):
        key_w = (panel.width - 48 - gap * (len(keys) - 1)) // len(keys)
        for col_index, key in enumerate(keys):
            x = panel.x + 24 + col_index * (key_w + gap)
            rect = pygame.Rect(x, top + row_index * (key_h + gap), key_w, key_h)
            layout.keys.append((row_index, col_index, rect))
            focused = (row_index, col_index) == (osk.row, osk.col)
            pygame.draw.rect(
                surface, t.ACCENT_DIM if focused else t.SURFACE_HI, rect, border_radius=8
            )
            if focused:
                pygame.draw.rect(surface, t.ACCENT, rect, width=2, border_radius=8)
            label_surface = fonts.body.render(key, t.TEXT)
            surface.blit(label_surface, label_surface.get_rect(center=rect.center))
