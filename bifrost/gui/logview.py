"""Turn command output blocks into uniform lines the log view can draw and scroll."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from bifrost.gui.blocks import Block, Table, Text

Measure = Callable[[str], int]  # pixel width of a string in the log font
CELL_MAX_CHARS = 48
CELL_PAD = 18
_NUMERIC = re.compile(r"^[-+]?[\d.,]+%?$")


@dataclass(frozen=True)
class VLine:
    kind: Literal["text", "gap", "title", "head", "row"]
    text: str = ""
    cells: tuple[str, ...] = ()
    widths: tuple[int, ...] = ()  # pixel width of each column
    right: tuple[bool, ...] = ()  # right-align the column (numbers)
    alt: bool = False  # shaded row


def wrap(measure: Measure, text: str, width: int) -> list[str]:
    """Split text into pieces that fit width, preferring to break at spaces."""
    lines: list[str] = []
    while text and measure(text) > width:
        low, high = 1, len(text)
        while low < high:  # largest prefix that fits
            mid = (low + high + 1) // 2
            if measure(text[:mid]) <= width:
                low = mid
            else:
                high = mid - 1
        cut = text.rfind(" ", 0, low + 1)
        cut = cut if cut > low // 2 else low
        lines.append(text[:cut].rstrip())
        text = text[cut:].lstrip(" ")
    lines.append(text)
    return lines


def shorten(text: str, limit: int = CELL_MAX_CHARS) -> str:
    """Keep the start and the end of a long value, e.g. a path, and elide the middle."""
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    return text[:head] + "…" + text[-(limit - 1 - head) :]


def _table_lines(measure: Measure, table: Table) -> tuple[list[VLine], int]:
    cells = [[shorten(c) for c in row] for row in table.rows]
    columns = len(table.columns)
    widths = tuple(
        max(
            [measure(table.columns[i])] + [measure(row[i]) for row in cells if i < len(row)]
        )
        + CELL_PAD
        for i in range(columns)
    )
    right = tuple(
        bool(cells) and all(i < len(r) and bool(_NUMERIC.match(r[i])) for r in cells)
        for i in range(columns)
    )
    lines = [VLine("gap")]
    if table.title:
        lines.append(VLine("title", text=table.title))
    lines.append(VLine("head", cells=tuple(table.columns), widths=widths, right=right))
    for index, row in enumerate(cells):
        padded = tuple(row) + ("",) * (columns - len(row))
        shaded = index % 2 == 1
        lines.append(VLine("row", cells=padded[:columns], widths=widths, right=right, alt=shaded))
    lines.append(VLine("gap"))
    return lines, sum(widths)


def layout_blocks(
    blocks: Iterable[Block], measure: Measure, width: int
) -> tuple[list[VLine], int]:
    """Lines for blocks, plus the pixel width of the widest table (for panning)."""
    lines: list[VLine] = []
    widest = 0
    for block in blocks:
        if isinstance(block, Text):
            lines.extend(VLine("text", text=part) for part in wrap(measure, block.text, width))
        elif isinstance(block, Table):
            table_lines, table_width = _table_lines(measure, block)
            lines.extend(table_lines)
            widest = max(widest, table_width)
    return lines, widest
