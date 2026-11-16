"""The commands offered by each action screen."""

from __future__ import annotations

from collections.abc import Callable

from bifrost.gui.actions_screen import ActionScreen, Command, Link, Runner

Start = Callable[[Command], Runner]


def build_screens(start: Start) -> dict[str, ActionScreen]:
    """Action screens by home-tile key."""
    return {
        "setup": ActionScreen(
            "Setup",
            (
                Link("Edit RomM connection", "Server URL, token and device id", "settings:romm"),
                Command("Check connection", "Contact RomM with the saved settings", ("status",)),
                Command(
                    "Enroll this device",
                    "Register this machine in RomM (needed for save sync)",
                    ("device-enroll",),
                    confirm=True,
                ),
            ),
            start,
        ),
        "sync": ActionScreen(
            "Sync",
            (
                Command("Preview library sync", "Show planned ROM, BIOS, asset links", ("sync",)),
                Command(
                    "Apply library sync",
                    "Create and update links for ES-DE",
                    ("sync", "--apply"),
                    confirm=True,
                ),
                Command(
                    "Apply incremental sync",
                    "Only ROMs changed since the last sync",
                    ("sync", "--apply", "--incremental"),
                    confirm=True,
                ),
                Command("Preview gamelists", "Show gamelist.xml changes", ("gamelist",)),
                Command(
                    "Write gamelists",
                    "Write gamelist.xml files for ES-DE",
                    ("gamelist", "--apply"),
                    confirm=True,
                ),
                Command("Preview save sync", "Compare local saves with RomM", ("save", "sync")),
                Command(
                    "Apply save sync",
                    "Upload and download saves",
                    ("save", "sync", "--apply"),
                    confirm=True,
                ),
                Command(
                    "Force save resync",
                    "Full reconciliation, e.g. after deleting a save locally",
                    ("save", "sync", "--apply", "--resync"),
                    confirm=True,
                ),
            ),
            start,
        ),
        "services": ActionScreen(
            "Services",
            (
                Command(
                    "Timer and watcher status",
                    "Systemd user units",
                    ("systemd", "status"),
                    takes_config=False,
                ),
                Command(
                    "Install timers",
                    "Periodic sync and save watcher",
                    ("systemd", "install"),
                    confirm=True,
                ),
                Command(
                    "Remove timers",
                    "Disable and delete the systemd units",
                    ("systemd", "uninstall", "--yes"),
                    confirm=True,
                    takes_config=False,
                ),
                Command(
                    "ES-DE hooks status",
                    "Event scripts in ES-DE/scripts",
                    ("esde-hooks", "status"),
                    takes_config=False,
                ),
                Command(
                    "Install ES-DE hooks",
                    "Sync saves when games start and end",
                    ("esde-hooks", "install"),
                    confirm=True,
                    takes_config=False,
                ),
                Command(
                    "Remove ES-DE hooks",
                    "Delete the Bifrost event scripts",
                    ("esde-hooks", "uninstall", "--yes"),
                    confirm=True,
                    takes_config=False,
                ),
            ),
            start,
        ),
        "maintenance": ActionScreen(
            "Maintenance",
            (
                Command("Run diagnostics", "Check paths, connectivity and services", ("doctor",)),
                Command("Check RomM connection", "API heartbeat and endpoints", ("status",)),
                Command("Cache status", "Age and size of cached API data", ("cache", "status")),
                Command(
                    "Clear cache",
                    "Forget cached API data; the next sync refetches it",
                    ("cache", "invalidate"),
                    confirm=True,
                ),
                Command("Scan library", "Missing, duplicate and unmatched ROMs", ("scan",)),
            ),
            start,
        ),
    }
