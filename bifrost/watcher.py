"""Save-file watcher: triggers sync when local save files change.

Uses watchdog (inotify on Linux) when available; falls back to polling.
Debounces rapid writes — a 15-second quiet window after the last event
before triggering, so multi-file emulator saves don't spawn N syncs.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("bifrost.watcher")

_DEBOUNCE_SECONDS = 15
_POLL_INTERVAL_SECONDS = 30


def _run_sync(bifrost_bin: str) -> None:
    """Run save-sync --apply, log outcome."""
    for cmd_args in (
        [bifrost_bin, "save-sync", "--apply"],
        # DISABILITATO (Fase 0 — state sync escluso): il watcher non invoca più state-sync.
        # [bifrost_bin, "state-sync", "--apply"],
    ):
        label = " ".join(cmd_args[1:])
        log.info("watcher: triggering %s", label)
        try:
            result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                log.info("watcher: %s completed (exit 0)", label)
            else:
                log.warning(
                    "watcher: %s exited %d\nstdout: %s\nstderr: %s",
                    label,
                    result.returncode,
                    result.stdout.strip(),
                    result.stderr.strip(),
                )
        except subprocess.TimeoutExpired:
            log.error("watcher: %s timed out after 300s", label)
        except FileNotFoundError:
            log.error("watcher: bifrost binary not found at %s", bifrost_bin)
            return


class _DebounceTimer:
    """Resets on each call; fires callback after debounce_seconds of quiet."""

    def __init__(self, debounce_seconds: float, callback: Callable[[], None]) -> None:
        self._debounce = debounce_seconds
        self._callback = callback
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._callback)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _watch_with_watchdog(watch_path: Path, bifrost_bin: str) -> None:
    from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
    from watchdog.observers import Observer  # type: ignore[import-untyped]

    debouncer = _DebounceTimer(_DEBOUNCE_SECONDS, lambda: _run_sync(bifrost_bin))

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event: object) -> None:  # type: ignore[override]
            # Skip directory events and hidden files
            src = getattr(event, "src_path", "")
            if src and Path(src).name.startswith("."):
                return
            is_dir = getattr(event, "is_directory", False)
            if is_dir:
                return
            log.debug("watcher: fs event %s", src)
            debouncer.trigger()

    observer = Observer()
    observer.schedule(_Handler(), str(watch_path), recursive=True)
    observer.start()
    log.info("watcher: watching %s (watchdog/inotify, debounce=%ds)", watch_path, _DEBOUNCE_SECONDS)
    try:
        while observer.is_alive():
            observer.join(timeout=5)
    except KeyboardInterrupt:
        pass
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join()
    log.info("watcher: stopped")


def _watch_with_polling(watch_path: Path, bifrost_bin: str) -> None:
    """Fallback: poll directory mtimes every POLL_INTERVAL_SECONDS seconds."""
    log.info(
        "watcher: watching %s (polling every %ds, watchdog not installed)",
        watch_path,
        _POLL_INTERVAL_SECONDS,
    )
    debouncer = _DebounceTimer(_DEBOUNCE_SECONDS, lambda: _run_sync(bifrost_bin))

    def _snapshot(root: Path) -> dict[str, float]:
        result: dict[str, float] = {}
        try:
            for p in root.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    try:
                        result[str(p)] = p.stat().st_mtime
                    except OSError:
                        pass
        except OSError:
            pass
        return result

    prev = _snapshot(watch_path)
    try:
        while True:
            time.sleep(_POLL_INTERVAL_SECONDS)
            curr = _snapshot(watch_path)
            if curr != prev:
                log.debug("watcher: poll detected changes")
                debouncer.trigger()
                prev = curr
    except KeyboardInterrupt:
        pass
    finally:
        debouncer.cancel()
    log.info("watcher: stopped")


def run_save_watcher(watch_path: Path, bifrost_bin: str = "bifrost") -> None:
    """Start the save watcher (blocking). Prefers watchdog, falls back to polling."""
    if not watch_path.exists():
        log.warning("watcher: saves path does not exist: %s — waiting for it to appear", watch_path)
        while not watch_path.exists():
            time.sleep(10)

    try:
        import watchdog  # noqa: F401  # type: ignore[import-untyped]
        _watch_with_watchdog(watch_path, bifrost_bin)
    except ImportError:
        log.warning("watcher: watchdog not installed, falling back to polling")
        _watch_with_polling(watch_path, bifrost_bin)
