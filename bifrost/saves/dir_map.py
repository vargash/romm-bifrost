"""Persistent game-directory -> rom_id mapping for per_rom_dir save profiles.

Directory saves (e.g. PCSX2 folder memory cards) are named after the game's
serial, not the ROM, so name matching cannot link them to a RomM ROM. The link
is learned from two sources and stored here: a download (the server save is
already bound to a rom_id) and a play session (the directory that changed while
a known ROM was running).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger("bifrost.saves.dir_map")

_MAP_FILE = Path("~/.local/state/bifrost/dir_saves.json").expanduser()


def _load_all() -> dict[str, dict[str, int]]:
    try:
        raw = json.loads(_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        emulator: {name: rom_id for name, rom_id in entries.items() if isinstance(rom_id, int)}
        for emulator, entries in raw.items()
        if isinstance(entries, dict)
    }


def load_dir_map(emulator: str) -> dict[str, int]:
    return _load_all().get(emulator, {})


def remember_dir(emulator: str, dir_name: str, rom_id: int) -> None:
    data = _load_all()
    entries = data.setdefault(emulator, {})
    if entries.get(dir_name) == rom_id:
        return
    entries[dir_name] = rom_id
    _MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _MAP_FILE.with_name(_MAP_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_MAP_FILE)
    _log.debug("mapped %s dir %r -> rom_id %d", emulator, dir_name, rom_id)
