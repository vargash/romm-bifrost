"""What a running command produces: text, tables and questions (see bifrost.report)."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Table:
    title: str | None
    columns: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class Prompt:
    id: int
    kind: str  # "confirm" | "choice" | "text"
    question: str
    choices: list[str]
    default: str
    secret: bool = False


Block = Text | Table | Prompt


def parse_line(line: str) -> Block:
    """One output line of a command run with BIFROST_OUTPUT=jsonl.

    Anything that is not a recognised event (a traceback, a stray print) is plain text.
    """
    if line.startswith("{"):
        try:
            event = json.loads(line)
            kind = event["type"]
            if kind == "message":
                return Text(str(event["text"]))
            if kind == "table":
                return Table(
                    event.get("title"),
                    [str(c) for c in event["columns"]],
                    [[str(c) for c in row] for row in event["rows"]],
                )
            if kind == "prompt":
                return Prompt(
                    int(event["id"]),
                    str(event["kind"]),
                    str(event["question"]),
                    [str(c) for c in event.get("choices", [])],
                    str(event.get("default", "")),
                    bool(event.get("secret", False)),
                )
        except (ValueError, KeyError, TypeError):
            pass
    return Text(line)
