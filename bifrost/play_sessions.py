"""Play session recording for ES-DE game-start / game-end event hooks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STATE_DIR = Path("~/.local/state/bifrost/play").expanduser()
_PENDING_FILE = _STATE_DIR / "pending.jsonl"


def _start_marker(rom_path: str) -> Path:
    key = hashlib.sha256(rom_path.encode()).hexdigest()[:16]
    return _STATE_DIR / f"{key}.start"


def record_game_start(rom_path: str) -> None:
    """Write a start-time marker for rom_path."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _start_marker(rom_path).write_text(
        json.dumps({"rom_path": rom_path, "start_time": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )


def record_game_end(rom_path: str) -> None:
    """Complete the session for rom_path and append to the pending queue."""
    marker = _start_marker(rom_path)
    if not marker.exists():
        return
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        start_iso: str = data.get("start_time", "")
        start_dt = datetime.fromisoformat(start_iso) if start_iso else None
    except (json.JSONDecodeError, ValueError):
        marker.unlink(missing_ok=True)
        return

    end_dt = datetime.now(UTC)
    duration_ms = int((end_dt - start_dt).total_seconds() * 1000) if start_dt else 0

    entry = {
        "rom_path": rom_path,
        "start_time": start_iso,
        "end_time": end_dt.isoformat(),
        "duration_ms": duration_ms,
    }
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _PENDING_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    marker.unlink(missing_ok=True)


def consume_pending_sessions() -> list[dict[str, Any]]:
    """Read and clear the pending play sessions queue.

    Returns entries shaped as SyncPlaySessionEntry (start_time, end_time, duration_ms).
    rom_id is omitted; RomM accepts it as optional.
    """
    if not _PENDING_FILE.exists():
        return []

    sessions: list[dict[str, Any]] = []
    try:
        lines = _PENDING_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            sessions.append(
                {
                    "start_time": raw.get("start_time", ""),
                    "end_time": raw.get("end_time", ""),
                    "duration_ms": raw.get("duration_ms", 0),
                }
            )
        except json.JSONDecodeError:
            continue

    try:
        _PENDING_FILE.unlink()
    except OSError:
        pass

    return sessions
