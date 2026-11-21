"""Table browser state: move over rows, filter by a column's values, search, read a full row."""

from __future__ import annotations

from bifrost.gui.blocks import Table
from bifrost.gui.inputs import Action, Event
from bifrost.gui.logview import common_prefix, shorten
from bifrost.gui.osk import OnScreenKeyboard

PAGE = 10
MAX_FILTER_VALUES = 12
ALL = "All"
STATUS_WORDS = ("action", "status", "result", "state")


class TableScreen:
    """Browse every row of a table.

    One column with few distinct values (e.g. an action) becomes a filter cycled with
    Left/Right; Find searches all cells. Long paths lose the directory prefix shared by
    the visible rows, and the full row is always shown in a detail panel.
    """

    def __init__(self, table: Table) -> None:
        self.table = table
        self.done = False
        self.mode = "browse"  # or "search"
        self.osk = OnScreenKeyboard()
        self.query = ""
        self.cursor = 0
        self.filter_column = self._pick_filter_column()
        self.filter_values = [ALL]
        self.filter_counts: dict[str, int] = {}
        if self.filter_column is not None:
            counts: dict[str, int] = {}
            for row in table.rows:
                counts[self._cell(row, self.filter_column)] = (
                    counts.get(self._cell(row, self.filter_column), 0) + 1
                )
            self.filter_counts = counts
            self.filter_values += list(counts)
        self.filter_index = 0
        self._haystack = ["\t".join(row).lower() for row in table.rows]
        self.view: list[int] = []
        self.prefixes: list[str] = []
        self._refilter()

    @staticmethod
    def _cell(row: list[str], column: int) -> str:
        return row[column] if column < len(row) else ""

    def _pick_filter_column(self) -> int | None:
        """A column with few distinct values; one that reads like a status wins."""
        candidates = [
            column
            for column in range(len(self.table.columns))
            if 2 <= len({self._cell(row, column) for row in self.table.rows}) <= MAX_FILTER_VALUES
        ]
        for column in candidates:
            if any(word in self.table.columns[column].lower() for word in STATUS_WORDS):
                return column
        return candidates[0] if candidates else None

    # ── queries ──────────────────────────────────────────────────────────
    @property
    def wants_text(self) -> bool:
        return self.mode == "search"

    @property
    def total(self) -> int:
        return len(self.table.rows)

    @property
    def current(self) -> list[str] | None:
        """The full cells of the selected row."""
        if not self.view:
            return None
        return self.table.rows[self.view[self.cursor]]

    @property
    def filter_label(self) -> str:
        if self.filter_column is None:
            return ""
        return self.table.columns[self.filter_column]

    def count_for(self, value: str) -> int:
        return self.total if value == ALL else self.filter_counts.get(value, 0)

    def row_cells(self, position: int) -> list[str]:
        """Cells of the row at a position in the filtered view, with shared prefixes removed."""
        row = self.table.rows[self.view[position]]
        out = []
        for column, cell in enumerate(row):
            prefix = self.prefixes[column] if column < len(self.prefixes) else ""
            out.append(shorten(cell[len(prefix) :] if prefix and cell.startswith(prefix) else cell))
        return out

    # ── input ────────────────────────────────────────────────────────────
    def handle(self, event: Event) -> None:
        if self.mode == "search":
            self._finish_search(self.osk.handle(event))
            return
        action = event.action
        if action is Action.UP:
            self.cursor = max(0, self.cursor - 1)
        elif action is Action.DOWN:
            self.cursor = min(max(0, len(self.view) - 1), self.cursor + 1)
        elif action is Action.PAGE_UP:
            self.cursor = max(0, self.cursor - PAGE)
        elif action is Action.PAGE_DOWN:
            self.cursor = min(max(0, len(self.view) - 1), self.cursor + PAGE)
        elif action in (Action.LEFT, Action.RIGHT) and self.filter_column is not None:
            step = 1 if action is Action.RIGHT else -1
            self.filter_index = (self.filter_index + step) % len(self.filter_values)
            self._refilter()
        elif action is Action.FIND:
            self.mode = "search"
            self.osk.start(self.query)
        elif action is Action.BACK:
            if self.query:  # first back clears the search, the next one leaves
                self.query = ""
                self._refilter()
            else:
                self.done = True

    def click_osk(self, row: int, col: int) -> None:
        if self.mode == "search":
            self._finish_search(self.osk.click(row, col))

    def _finish_search(self, result: str | None) -> None:
        if result == "commit":
            self.query = self.osk.buffer.strip()
            self.mode = "browse"
            self._refilter()
        elif result == "cancel":
            self.mode = "browse"

    def focus(self, position: int) -> None:
        if 0 <= position < len(self.view):
            self.cursor = position

    # ── filtering ────────────────────────────────────────────────────────
    def _refilter(self) -> None:
        value = self.filter_values[self.filter_index]
        needle = self.query.lower()
        column = self.filter_column
        self.view = [
            i
            for i, row in enumerate(self.table.rows)
            if (value == ALL or column is None or self._cell(row, column) == value)
            and (not needle or needle in self._haystack[i])
        ]
        self.cursor = 0
        self.prefixes = [
            common_prefix(self._cell(self.table.rows[i], c) for i in self.view)
            for c in range(len(self.table.columns))
        ]
