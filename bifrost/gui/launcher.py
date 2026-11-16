"""The ES-DE entry that opens the GUI: a small shell script in a launcher system's folder."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from bifrost.config import AppConfig
from bifrost.errors import BifrostError

LAUNCHER_NAME = "Bifrost.sh"
MARKER = "# Managed by bifrost gui install-launcher"
# EmuDeck's ES-DE lists the scripts in this ROMs subfolder (.sh files) as launchable entries.
DEFAULT_SYSTEM_DIR = "emulators"


class LauncherError(BifrostError):
    """The launcher cannot be written or removed safely."""


def default_directory(config: AppConfig) -> Path:
    return Path(config.esde.roms_path).expanduser() / DEFAULT_SYSTEM_DIR


def bifrost_command(bifrost_bin: str | None = None) -> list[str]:
    """Command that starts bifrost: the given binary, the one on PATH, or this interpreter."""
    found = bifrost_bin or shutil.which("bifrost")
    return [found] if found else [sys.executable, "-m", "bifrost"]


def launcher_content(command: list[str]) -> str:
    return (
        "#!/bin/sh\n"
        f"{MARKER} - do not edit manually.\n"
        f"exec {shlex.join([*command, 'gui', '--fullscreen'])}\n"
    )


def is_managed(path: Path) -> bool:
    try:
        return MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def install(directory: Path, command: list[str]) -> Path:
    """Write the launcher; refuses to replace a file of the same name it did not create."""
    path = directory / LAUNCHER_NAME
    if path.exists() and not is_managed(path):
        raise LauncherError(f"{path} exists and was not created by Bifrost; not overwriting it.")
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(launcher_content(command), encoding="utf-8")
    path.chmod(0o755)
    return path


def remove(directory: Path) -> Path | None:
    """Delete the launcher if Bifrost wrote it; returns its path, or None when absent."""
    path = directory / LAUNCHER_NAME
    if not path.exists():
        return None
    if not is_managed(path):
        raise LauncherError(f"{path} was not created by Bifrost; not removing it.")
    path.unlink()
    return path
