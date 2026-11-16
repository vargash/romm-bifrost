from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator

import pytest
from click.testing import CliRunner

import bifrost.report as report
from bifrost.cli import main
from bifrost.report import ReportTable


class Events:
    """Reads the JSON events the code under test printed to stdout."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    def read(self) -> list[dict]:  # type: ignore[type-arg]
        return [json.loads(line) for line in self._capsys.readouterr().out.splitlines()]


@pytest.fixture
def events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Events]:
    monkeypatch.setenv("BIFROST_OUTPUT", "jsonl")
    monkeypatch.setattr(report, "_event_console", None)
    yield Events(capsys)
    report._event_console = None  # the console is process-wide; do not leak it to other tests


def test_tables_and_messages_become_events(events: Events) -> None:
    console = report.make_console()
    table = ReportTable(title="Sync")
    table.add_column("Path")
    table.add_column("State")
    table.add_row("/roms/Cuphead [0100A][v0][US]/game.nsp", "[green]ok[/green]")

    console.print("[yellow]careful[/yellow]")
    console.print(table)

    message, table_event = events.read()
    assert message == {"type": "message", "text": "careful"}
    assert table_event["title"] == "Sync"
    assert table_event["columns"] == ["Path", "State"]
    assert table_event["rows"][0][1] == "ok"


def test_escaped_cells_keep_their_brackets(events: Events) -> None:
    from rich.markup import escape

    table = ReportTable(title="t")
    table.add_column("Path")
    table.add_row(escape("/roms/Cuphead [v0][US].nsp"))
    report.make_console().print(table)

    assert events.read()[0]["rows"] == [["/roms/Cuphead [v0][US].nsp"]]


def test_prompts_are_answered_on_stdin(events: Events, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"id": 1, "answer": "yes"}\n{"id": 2, "answer": "d"}\n{"id": 3, "answer": "hello"}\n'
        ),
    )
    report.make_console()

    assert report.is_interactive()
    assert report.confirm("Remove it?") is True
    assert report.ask("Pick", choices=["u", "d", "s"], default="u") == "d"
    assert report.ask("Name?", default="x", password=True) == "hello"

    prompts = events.read()
    assert [p["kind"] for p in prompts] == ["confirm", "choice", "text"]
    assert prompts[1]["choices"] == ["u", "d", "s"]
    assert prompts[2]["secret"] is True and prompts[0]["id"] == 1


def test_unanswered_prompt_takes_the_default(
    events: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    report.make_console()

    assert report.confirm("Sure?", default=True) is True
    assert report.confirm("Sure?") is False
    assert report.ask("Name?", default="kept") == "kept"


def test_plain_mode_is_a_normal_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_OUTPUT", raising=False)
    monkeypatch.setattr(report, "_event_console", None)
    assert not isinstance(report.make_console(), report.EventConsole)


def test_real_command_emits_a_table_event(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BIFROST_OUTPUT", "jsonl")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(report, "_event_console", None)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[romm]\nurl = "http://x"\nclient_token = "rmm_1"\n', encoding="utf-8")
    cfg.chmod(0o600)

    result = CliRunner().invoke(main, ["cache", "status", "--config", str(cfg)])
    report._event_console = None

    assert result.exit_code == 0
    event = json.loads(result.output.splitlines()[0])
    assert event["type"] in ("table", "message")
