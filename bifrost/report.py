"""Output and prompts for the CLI, separate from the commands that produce them.

Commands talk to a console (``make_console``) and ask questions through ``ask`` and
``confirm``. For a person at a terminal these are rich tables and rich prompts. With
``BIFROST_OUTPUT=jsonl`` they become a stream of JSON events on stdout, and questions are
answered on stdin, so another program (the GUI) can render tables itself and answer prompts:

    {"type": "message", "text": "..."}
    {"type": "table", "title": "...", "columns": ["..."], "rows": [["..."]]}
    {"type": "prompt", "id": 1, "kind": "confirm"|"choice"|"text",
     "question": "...", "choices": ["..."], "default": "...", "secret": false}

and the answer to a prompt is one line on stdin: ``{"id": 1, "answer": "..."}``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

OUTPUT_ENV = "BIFROST_OUTPUT"
JSONL = "jsonl"


class ReportTable:
    """A table that renders as a rich table or as a structured event."""

    def __init__(self, title: str | None = None, **options: Any) -> None:
        self.title = title
        self._options = options
        self._columns: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[tuple[Any, ...]] = []

    def add_column(self, header: str, **options: Any) -> None:
        self._columns.append((header, options))

    def add_row(self, *cells: Any) -> None:
        self.rows.append(cells)

    @property
    def columns(self) -> list[str]:
        return [header for header, _ in self._columns]

    def __rich__(self) -> Table:
        table = Table(title=self.title, **self._options)
        for header, options in self._columns:
            table.add_column(header, **options)
        for row in self.rows:
            table.add_row(*row)
        return table


def plain(value: Any) -> str:
    """Text of a rich-markup string without its styling tags."""
    if "[" not in str(value):  # nothing to strip; this is the common, hot path
        return str(value)
    try:
        return Text.from_markup(str(value)).plain
    except Exception:  # malformed markup: show it as written
        return str(value)


class EventConsole(Console):
    """Console that emits JSON events instead of drawing to a terminal."""

    def __init__(self) -> None:
        super().__init__(file=io.StringIO(), width=110, force_terminal=False, color_system=None)
        self._prompt_id = 0

    def _emit(self, event: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def print(self, *objects: Any, **kwargs: Any) -> None:  # type: ignore[override]
        for obj in objects:
            if isinstance(obj, ReportTable):
                self._emit(
                    {
                        "type": "table",
                        "title": plain(obj.title) if obj.title else None,
                        "columns": obj.columns,
                        "rows": [[plain(cell) for cell in row] for row in obj.rows],
                    }
                )
            elif isinstance(obj, str):
                self._emit({"type": "message", "text": plain(obj)})
            else:  # any other rich renderable: render it as plain text
                buffer = Console(file=io.StringIO(), width=110, color_system=None)
                buffer.print(obj)
                text = buffer.file.getvalue().rstrip()  # type: ignore[attr-defined]
                self._emit({"type": "message", "text": text})

    def question(
        self,
        kind: str,
        question: str,
        choices: list[str] | None,
        default: str,
        secret: bool = False,
    ) -> str:
        self._prompt_id += 1
        self._emit(
            {
                "type": "prompt",
                "id": self._prompt_id,
                "kind": kind,
                "question": plain(question),
                "choices": choices or [],
                "default": default,
                "secret": secret,
            }
        )
        line = sys.stdin.readline()
        if not line:  # nobody left to answer: take the default
            return default
        try:
            return str(json.loads(line).get("answer", default))
        except (ValueError, AttributeError):
            return default


_event_console: EventConsole | None = None


def make_console() -> Console:
    """A rich console, or the event console when BIFROST_OUTPUT=jsonl."""
    global _event_console
    if os.environ.get(OUTPUT_ENV) == JSONL:
        _event_console = _event_console or EventConsole()
        return _event_console
    return Console()


def is_interactive() -> bool:
    """Whether someone can answer a question: a terminal, or a program reading events."""
    if _event_console is not None:
        return True
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(
    question: str,
    *,
    choices: list[str] | None = None,
    default: str | None = None,
    password: bool = False,
) -> str:
    """Ask for a line of text or, with choices, one of them."""
    if _event_console is not None:
        kind = "choice" if choices else "text"
        return _event_console.question(kind, question, choices, default or "", secret=password)
    options: dict[str, Any] = {"choices": choices, "password": password}
    if default is not None:
        options["default"] = default
    return Prompt.ask(question, **options)


def confirm(question: str, *, default: bool = False) -> bool:
    """Ask a yes/no question."""
    if _event_console is not None:
        answer = _event_console.question(
            "confirm", question, ["yes", "no"], "yes" if default else "no"
        )
        return answer.strip().lower() in ("y", "yes", "true", "1")
    return Confirm.ask(question, default=default)


def row_limit(default: int = 25) -> int | None:
    """How many rows of a long listing to print: a screenful for a terminal, all for a program."""
    return None if _event_console is not None else default
