from __future__ import annotations

from bifrost.gui.blocks import Table
from bifrost.gui.inputs import Action, Event
from bifrost.gui.logview import INLINE_ROWS, common_prefix, layout_blocks
from bifrost.gui.table_screen import TableScreen


def _measure(text: str) -> int:
    return 10 * len(text)


def _operations(count: int = 100) -> Table:
    rows = []
    for i in range(count):
        action = "create" if i % 10 == 0 else "ok"
        rows.append(
            [
                "asset",
                action,
                f"/home/u/media/fbneo/covers/game{i}.png",
                f"/mnt/nas/resources/roms/10/{i}/cover.png",
                "",
            ]
        )
    return Table("Ops", ["Category", "Action", "Destination", "Target", "Detail"], rows)


def _press(screen: TableScreen, *actions: Action) -> None:
    for action in actions:
        screen.handle(Event(action))


def test_common_prefix_stops_at_a_directory_boundary() -> None:
    paths = ["/home/u/media/a/x.png", "/home/u/media/b/y.png", "/home/u/media/a/z.png"]

    assert common_prefix(paths) == "/home/u/media/"


def test_common_prefix_ignores_non_paths_and_short_prefixes() -> None:
    assert common_prefix(["create", "ok", "create"]) == ""
    assert common_prefix(["/a/x", "/a/y"]) == ""  # prefix too short to be worth hiding
    assert common_prefix(["/home/u/media/only"]) == ""  # a single value has nothing in common


def test_prefix_is_dropped_from_cells_and_legend_added_inline() -> None:
    lines, _ = layout_blocks([_operations(5)], _measure, 500)

    notes = [line.text for line in lines if line.kind == "note"]
    assert any(n.startswith("Destination: /home/u/media/fbneo/covers/") for n in notes)
    first_row = next(line for line in lines if line.kind == "row")
    assert first_row.cells[2] == "game0.png"


def test_long_table_is_cut_inline_with_a_browse_hint() -> None:
    lines, _ = layout_blocks([_operations(100)], _measure, 500)

    assert sum(line.kind == "row" for line in lines) == INLINE_ROWS
    assert any("more rows" in line.text for line in lines if line.kind == "note")
    assert "(100 rows)" in next(line.text for line in lines if line.kind == "title")


def test_browser_keeps_every_row_and_cycles_the_action_filter() -> None:
    screen = TableScreen(_operations(100))

    assert screen.filter_label == "Action"
    assert screen.filter_values == ["All", "create", "ok"]
    assert len(screen.view) == 100

    _press(screen, Action.RIGHT)
    assert len(screen.view) == 10
    assert all(screen.table.rows[i][1] == "create" for i in screen.view)
    _press(screen, Action.RIGHT, Action.RIGHT)  # ok, then wraps to All
    assert len(screen.view) == 100
    _press(screen, Action.LEFT)
    assert screen.filter_values[screen.filter_index] == "ok"


def test_browser_shows_the_distinguishing_part_and_the_full_row() -> None:
    screen = TableScreen(_operations(100))

    assert screen.row_cells(3)[2] == "game3.png"
    assert screen.current is not None
    assert screen.current[2] == "/home/u/media/fbneo/covers/game0.png"


def test_cursor_moves_pages_and_stays_in_bounds() -> None:
    screen = TableScreen(_operations(25))

    _press(screen, Action.UP)
    assert screen.cursor == 0
    _press(screen, Action.PAGE_DOWN, Action.PAGE_DOWN, Action.PAGE_DOWN)
    assert screen.cursor == 24
    _press(screen, Action.DOWN)
    assert screen.cursor == 24
    _press(screen, Action.PAGE_UP)
    assert screen.cursor == 14


def test_search_filters_all_columns_and_back_clears_then_leaves() -> None:
    screen = TableScreen(_operations(100))

    _press(screen, Action.FIND)
    assert screen.wants_text
    for char in "/10/42/":
        screen.handle(Event(Action.TEXT, char))
    _press(screen, Action.CONFIRM)

    assert len(screen.view) == 1 and screen.current is not None
    assert "/10/42/" in screen.current[3]
    _press(screen, Action.BACK)
    assert len(screen.view) == 100 and not screen.done
    _press(screen, Action.BACK)
    assert screen.done


def test_search_with_no_match_is_empty_and_safe() -> None:
    screen = TableScreen(_operations(30))
    _press(screen, Action.FIND)
    for char in "zzz":
        screen.handle(Event(Action.TEXT, char))
    _press(screen, Action.CONFIRM)

    assert screen.view == [] and screen.current is None
    _press(screen, Action.DOWN, Action.PAGE_DOWN)  # nothing to move over
    assert screen.cursor == 0
