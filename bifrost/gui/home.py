"""Home screen state: a grid of tiles leading to the other screens."""

from __future__ import annotations

from dataclasses import dataclass

from bifrost.gui.inputs import Action, Event


@dataclass(frozen=True)
class Tile:
    key: str
    label: str
    description: str
    enabled: bool = True


TILES = (
    Tile("settings", "Settings", "Connection, paths, options"),
    Tile("sync", "Sync", "Library and save sync", enabled=False),
    Tile("saves", "Saves", "Profiles, core mappings", enabled=False),
    Tile("services", "Services", "Timers and ES-DE hooks", enabled=False),
    Tile("maintenance", "Maintenance", "Cache, orphans, diagnostics", enabled=False),
)
COLUMNS = 3


class HomeScreen:
    """Move over the tiles; opening an enabled one sets ``opened`` for the app to act on."""

    mode = "home"

    def __init__(self, tiles: tuple[Tile, ...] = TILES, columns: int = COLUMNS) -> None:
        self.tiles = tiles
        self.columns = columns
        self.index = 0
        self.opened: str | None = None
        self.done = False

    @property
    def current(self) -> Tile:
        return self.tiles[self.index]

    def handle(self, event: Event) -> None:
        action = event.action
        step = {
            Action.LEFT: -1,
            Action.RIGHT: 1,
            Action.UP: -self.columns,
            Action.DOWN: self.columns,
        }.get(action)
        if step is not None:
            moved = self.index + step
            # Horizontal moves stay on their row; vertical moves stop at the grid edge.
            same_row = moved // self.columns == self.index // self.columns
            if 0 <= moved < len(self.tiles) and (abs(step) != 1 or same_row):
                self.index = moved
        elif action in (Action.ACCEPT, Action.CONFIRM):
            if self.current.enabled:
                self.opened = self.current.key
        elif action is Action.BACK:
            self.done = True

    def focus(self, index: int) -> None:
        if 0 <= index < len(self.tiles):
            self.index = index
