"""Detect ROM links that ES-DE would not list because of its system extension filters."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from bifrost.config import AppConfig


def system_extensions(custom_systems_dir: Path) -> dict[str, frozenset[str]]:
    """Accepted extensions (lowercase, with dot) per system name in es_systems.xml.

    Empty when the file is missing or unparsable.
    """
    try:
        root = ET.parse(custom_systems_dir / "es_systems.xml").getroot()
    except (OSError, ET.ParseError):
        return {}
    systems: dict[str, frozenset[str]] = {}
    for system in root.iter("system"):
        name = (system.findtext("name") or "").strip()
        extensions = (system.findtext("extension") or "").lower().split()
        if name and extensions:
            systems[name] = frozenset(extensions)
    return systems


def hidden_links(config: AppConfig) -> dict[str, list[str]]:
    """Symlink names per system that ES-DE hides because the extension isn't accepted.

    Only systems defined in the custom es_systems.xml are checked: ES-DE's bundled
    definitions are not available here. A link without an extension (a folder) is
    always hidden.
    """
    roms_root = Path(config.esde.roms_path).expanduser()
    hidden: dict[str, list[str]] = {}
    for system, accepted in system_extensions(
        Path(config.esde.custom_systems_path).expanduser()
    ).items():
        try:
            with os.scandir(roms_root / system) as entries:
                names = sorted(e.name for e in entries if e.is_symlink())
        except OSError:
            continue
        bad = [n for n in names if Path(n).suffix.lower() not in accepted]
        if bad:
            hidden[system] = bad
    return hidden
