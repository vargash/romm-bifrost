"""Process-level exclusive lock for bifrost save-sync."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from bifrost.errors import BifrostError

_DEFAULT_LOCK_PATH = Path("~/.local/state/bifrost/save-sync.lock").expanduser()


class SaveSyncLockError(BifrostError):
    """Raised when another bifrost save-sync process is already running."""


@contextmanager
def save_sync_lock(lock_path: Path = _DEFAULT_LOCK_PATH) -> Generator[None, None, None]:
    """Acquire an exclusive flock on lock_path.

    Raises SaveSyncLockError immediately (LOCK_NB) if already held.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SaveSyncLockError(
                f"Another bifrost save-sync is already running (lock: {lock_path})"
            ) from exc
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
