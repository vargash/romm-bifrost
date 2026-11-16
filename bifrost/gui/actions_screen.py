"""Screens that list commands, run one and show its live output."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from bifrost.gui.blocks import Block, Prompt, Text
from bifrost.gui.inputs import Action, Event
from bifrost.gui.osk import OnScreenKeyboard

MAX_LOG_BLOCKS = 2000
PAN_STEP = 80
Mode = Literal["list", "confirm", "running", "done"]


@dataclass(frozen=True)
class Command:
    """A bifrost CLI invocation."""

    label: str
    description: str
    args: tuple[str, ...]
    confirm: bool = False  # ask before running (anything that changes files or services)
    takes_config: bool = True  # whether the command accepts --config


@dataclass(frozen=True)
class Link:
    """An entry that opens another screen, e.g. ``settings:romm``."""

    label: str
    description: str
    target: str


Entry = Command | Link


class Runner(Protocol):
    returncode: int | None

    def poll(self) -> list[Block]: ...

    def answer(self, prompt_id: int, answer: str) -> None: ...

    def cancel(self) -> None: ...


class ActionScreen:
    """Pick a command, confirm if needed, watch it run, read the result."""

    def __init__(
        self, title: str, entries: tuple[Entry, ...], start: Callable[[Command], Runner]
    ) -> None:
        self.title = title
        self.entries = entries
        self._start = start
        self.mode: Mode = "list"
        self.index = 0
        self.scroll = 0  # lines above the bottom of the log; 0 follows new output
        self.lines: deque[Block] = deque(maxlen=MAX_LOG_BLOCKS)
        self.pending: Prompt | None = None  # a question the running command is waiting on
        self.choice = 0  # highlighted answer of a pending "choice" prompt
        self.osk = OnScreenKeyboard()  # answer editor of a pending "text" prompt
        self.pan = 0  # pixels the log is scrolled to the right
        self.exit_code: int | None = None
        self.opened: str | None = None  # a Link target for the app to open
        self.done = False  # leave this screen
        self.cancelled = False
        self._runner: Runner | None = None
        self._command: Command | None = None

    @property
    def current(self) -> Entry:
        return self.entries[self.index]

    @property
    def command(self) -> Command | None:
        """The command being confirmed or run."""
        return self._command

    @property
    def busy(self) -> bool:
        return self.mode == "running"

    def handle(self, event: Event) -> None:
        action = event.action
        if self.mode == "list":
            self._handle_list(action)
        elif self.mode == "confirm":
            if action in (Action.ACCEPT, Action.CONFIRM) and self._command is not None:
                self._run(self._command)
            elif action is Action.BACK:
                self.mode = "list"
        elif self.pending is not None:
            self._handle_answer(event)
        else:  # running or done: scroll the log
            if action is Action.UP:
                self.scroll += 1
            elif action is Action.DOWN:
                self.scroll = max(0, self.scroll - 1)
            elif action is Action.LEFT:
                self.pan = max(0, self.pan - PAN_STEP)
            elif action is Action.RIGHT:
                self.pan += PAN_STEP
            elif action is Action.BACK:
                self._leave_log()
            elif action in (Action.ACCEPT, Action.CONFIRM) and self.mode == "done":
                self.mode = "list"

    def _handle_list(self, action: Action) -> None:
        if action is Action.UP:
            self.index = (self.index - 1) % len(self.entries)
        elif action is Action.DOWN:
            self.index = (self.index + 1) % len(self.entries)
        elif action in (Action.ACCEPT, Action.CONFIRM):
            entry = self.current
            if isinstance(entry, Link):
                self.opened = entry.target
            elif entry.confirm:
                self._command, self.mode = entry, "confirm"
            else:
                self._run(entry)
        elif action is Action.BACK:
            self.done = True

    def _run(self, command: Command) -> None:
        self._command = command
        self.lines.clear()
        self.scroll = 0
        self.exit_code = None
        self.cancelled = False
        self.pending = None
        self.pan = 0
        self._runner = self._start(command)
        self.mode = "running"

    def _leave_log(self) -> None:
        if self.mode == "running" and self._runner is not None:
            if not self.cancelled:
                self.cancelled = True
                self.lines.append(Text("Cancelling..."))
                self.pending = None
                self._runner.cancel()
        else:
            self.mode = "list"

    def tick(self) -> bool:
        """Pull new output from the running command; True when something changed."""
        if self.mode != "running" or self._runner is None:
            return False
        new = self._runner.poll()
        for block in new:
            self.lines.append(block)
            if isinstance(block, Prompt):
                self._ask(block)
        if self._runner.returncode is not None:
            self.exit_code = self._runner.returncode
            self.mode = "done"
            return True
        return bool(new)

    # ── questions from the running command ───────────────────────────────
    def _ask(self, prompt: Prompt) -> None:
        self.pending = prompt
        default = prompt.default
        self.choice = prompt.choices.index(default) if default in prompt.choices else 0
        if prompt.kind == "text":
            self.osk.start(prompt.default if not prompt.secret else "")

    def _handle_answer(self, event: Event) -> None:
        prompt, action = self.pending, event.action
        if prompt is None:
            return
        if prompt.kind == "text":
            result = self.osk.handle(event)
            if result == "commit":
                self._answer(self.osk.buffer or prompt.default)
            elif result == "cancel":
                self._answer(prompt.default)
            return
        if prompt.kind == "confirm":
            if action in (Action.ACCEPT, Action.CONFIRM):
                self._answer("yes")
            elif action is Action.BACK:
                self._answer("no")
            return
        if action in (Action.LEFT, Action.UP):
            self.choice = (self.choice - 1) % len(prompt.choices)
        elif action in (Action.RIGHT, Action.DOWN):
            self.choice = (self.choice + 1) % len(prompt.choices)
        elif action in (Action.ACCEPT, Action.CONFIRM):
            self._answer(prompt.choices[self.choice])
        elif action is Action.BACK:
            self._answer(prompt.default)

    def click_osk(self, row: int, col: int) -> None:
        prompt = self.pending
        if prompt is not None and prompt.kind == "text":
            result = self.osk.click(row, col)
            if result == "commit":
                self._answer(self.osk.buffer or prompt.default)
            elif result == "cancel":
                self._answer(prompt.default)

    def _answer(self, answer: str) -> None:
        prompt = self.pending
        if prompt is None or self._runner is None:
            return
        self._runner.answer(prompt.id, answer)
        shown = "*" * len(answer) if prompt.secret else answer
        self.lines.append(Text(f"> {prompt.question}  {shown}"))
        self.pending = None

    def focus_row(self, index: int) -> None:
        if self.mode == "list" and 0 <= index < len(self.entries):
            self.index = index

    def shutdown(self) -> None:
        """Stop a command that is still running when the app closes."""
        if self.mode == "running" and self._runner is not None:
            self._runner.cancel()
