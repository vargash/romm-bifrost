from __future__ import annotations

import sys
import time

import pytest
from click.testing import CliRunner

from bifrost.cli import main
from bifrost.gui.actions_screen import ActionScreen, Command, Link
from bifrost.gui.catalog import build_screens
from bifrost.gui.inputs import Action, Event
from bifrost.gui.runner import CommandRunner, cli_argv


class FakeRunner:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pending: list[str] = []
        self.cancelled = False

    def poll(self) -> list[str]:
        out, self.pending = self.pending, []
        return out

    def cancel(self) -> None:
        self.cancelled = True
        self.returncode = -15


def _screen(runners: list[FakeRunner]) -> ActionScreen:
    def start(_command: Command) -> FakeRunner:
        runner = FakeRunner()
        runners.append(runner)
        return runner

    entries = (
        Command("Look", "Read only", ("status",)),
        Command("Change", "Writes files", ("sync", "--apply"), confirm=True),
        Link("Edit", "Open settings", "settings:romm"),
    )
    return ActionScreen("Test", entries, start)


def _press(screen: ActionScreen, *actions: Action) -> None:
    for action in actions:
        screen.handle(Event(action))


def test_command_without_confirmation_runs_streams_and_finishes() -> None:
    runners: list[FakeRunner] = []
    screen = _screen(runners)

    _press(screen, Action.ACCEPT)
    assert screen.mode == "running" and screen.busy
    runners[0].pending = ["one", "two"]
    assert screen.tick()
    assert list(screen.lines) == ["one", "two"]
    assert not screen.tick()  # nothing new

    runners[0].returncode = 0
    assert screen.tick()
    assert screen.mode == "done" and screen.exit_code == 0
    _press(screen, Action.BACK)
    assert screen.mode == "list"


def test_changing_command_asks_before_running() -> None:
    runners: list[FakeRunner] = []
    screen = _screen(runners)

    _press(screen, Action.DOWN, Action.ACCEPT)
    assert screen.mode == "confirm" and runners == []
    _press(screen, Action.BACK)
    assert screen.mode == "list" and runners == []
    _press(screen, Action.ACCEPT, Action.CONFIRM)
    assert screen.mode == "running" and len(runners) == 1


def test_back_cancels_a_running_command_once() -> None:
    runners: list[FakeRunner] = []
    screen = _screen(runners)
    _press(screen, Action.ACCEPT)

    _press(screen, Action.BACK, Action.BACK)

    assert runners[0].cancelled and screen.cancelled
    assert list(screen.lines).count("Cancelling...") == 1
    screen.tick()
    assert screen.mode == "done"


def test_link_opens_target_and_back_leaves_screen() -> None:
    screen = _screen([])

    _press(screen, Action.UP, Action.ACCEPT)  # wraps to the last entry
    assert screen.opened == "settings:romm"
    _press(screen, Action.BACK)
    assert screen.done


def test_log_scrolls_and_follows() -> None:
    runners: list[FakeRunner] = []
    screen = _screen(runners)
    _press(screen, Action.ACCEPT)

    _press(screen, Action.UP, Action.UP)
    assert screen.scroll == 2
    _press(screen, Action.DOWN, Action.DOWN, Action.DOWN)
    assert screen.scroll == 0


def test_runner_streams_output_and_exit_code() -> None:
    runner = CommandRunner(
        [sys.executable, "-c", "import sys; print('\\x1b[31mred\\x1b[0m'); print('err', file=sys.stderr); sys.exit(3)"]
    )
    lines: list[str] = []
    deadline = time.monotonic() + 10
    while runner.returncode is None and time.monotonic() < deadline:
        lines += runner.poll()
        time.sleep(0.01)

    assert runner.returncode == 3
    assert lines == ["red", "err"]


def test_runner_cancel_stops_the_process() -> None:
    runner = CommandRunner([sys.executable, "-c", "import time; time.sleep(30)"])
    runner.cancel()
    deadline = time.monotonic() + 10
    while runner.returncode is None and time.monotonic() < deadline:
        runner.poll()
        time.sleep(0.01)

    assert runner.returncode is not None and runner.returncode != 0


def test_cli_argv_appends_config_only_when_given(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert cli_argv(("sync",), None)[-1] == "sync"
    assert cli_argv(("sync",), tmp_path / "c.toml")[-2:] == ["--config", str(tmp_path / "c.toml")]


def _commands() -> list[Command]:
    return [
        e
        for screen in build_screens(lambda c: FakeRunner()).values()
        for e in screen.entries
        if isinstance(e, Command)
    ]


@pytest.mark.parametrize("command", _commands(), ids=lambda c: " ".join(c.args))
def test_catalog_commands_match_the_cli(command: Command) -> None:
    """Every offered command and flag must exist, and --config only where accepted."""
    path = [a for a in command.args if not a.startswith("-")]
    flags = [a for a in command.args if a.startswith("-")]
    result = CliRunner().invoke(main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    for flag in flags:
        assert flag in result.output
    assert ("--config" in result.output) == command.takes_config


def test_catalog_links_point_to_known_screens() -> None:
    screens = build_screens(lambda c: FakeRunner())
    targets = {
        e.target.split(":")[0]
        for s in screens.values()
        for e in s.entries
        if isinstance(e, Link)
    }

    assert targets <= {"settings"}
