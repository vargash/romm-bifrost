from __future__ import annotations

from bifrost.gui.blocks import Table, Text
from bifrost.gui.logview import layout_blocks, shorten, wrap


def measure(text: str) -> int:
    return 10 * len(text)  # fixed-width font, 10px per character


def test_wrap_breaks_at_spaces_and_splits_long_words() -> None:
    assert wrap(measure, "alpha beta gamma", 100) == ["alpha beta", "gamma"]
    assert wrap(measure, "x" * 25, 100) == ["x" * 10, "x" * 10, "x" * 5]
    assert wrap(measure, "short", 100) == ["short"]


def test_shorten_keeps_both_ends_of_a_long_value() -> None:
    long = "/home/u/Emulation/tools/downloaded_media/fbneo/backcovers/" + "a" * 30 + ".png"

    cut = shorten(long, 20)

    assert len(cut) == 20 and "…" in cut
    assert cut.startswith("/home/u") and cut.endswith(".png")
    assert shorten("small") == "small"


def test_table_becomes_aligned_lines_with_numbers_on_the_right() -> None:
    table = Table("Summary", ["Metric", "Value"], [["ROM symlinks", "2040"], ["Errors", "0"]])

    lines, widest = layout_blocks([Text("before"), table], measure, 500)

    kinds = [line.kind for line in lines]
    assert kinds == ["text", "gap", "title", "head", "row", "row", "gap"]
    head = lines[3]
    assert head.cells == ("Metric", "Value")
    assert head.right == (False, True)
    assert head.widths == lines[4].widths == lines[5].widths
    assert widest == sum(head.widths)
    assert lines[5].alt and not lines[4].alt


def test_ragged_rows_are_padded_and_long_cells_shortened() -> None:
    table = Table(None, ["a", "b"], [["x" * 100], ["1", "2", "extra"]])

    lines, _ = layout_blocks([table], measure, 500)

    rows = [line for line in lines if line.kind == "row"]
    assert len(rows[0].cells) == 2 and rows[0].cells[1] == ""
    assert len(rows[0].cells[0]) <= 48
    assert rows[1].cells == ("1", "2")
