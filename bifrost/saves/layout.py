"""Layout providers: translate a saves_root directory into scanned save files.

A LayoutProvider knows how to walk a particular directory structure to discover
save files for each supported SaveProfile.  Today only EmudeckEsdeLayout exists;
the abstraction makes it easy to support different directory conventions later.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from bifrost.saves.profiles import PROFILES, SaveProfile

_log = logging.getLogger("bifrost.saves.layout")


@dataclass(frozen=True)
class ScannedFile:
    """A save file found on disk together with the profile it belongs to."""

    path: Path
    profile: SaveProfile


@runtime_checkable
class LayoutProvider(Protocol):
    def scan_saves(
        self,
        saves_root: Path,
        enabled_emulators: list[str] | None = None,
    ) -> list[ScannedFile]:
        """Return all syncable save files under saves_root.

        enabled_emulators: if non-empty, restrict to profiles whose emulator id
        appears in the list.  An empty list or None means all supported profiles.
        """
        ...


class EmudeckEsdeLayout:
    """Scans saves following the EmuDeck/ES-DE convention.

    Expected layout: <saves_root>/<emulator>/saves/<files>
    Each profile's save_subpath is resolved relative to saves_root.
    Scanning is flat (no recursion into subdirectories within a profile dir);
    RetroArch users with "Save folder per core" ON should add per-core profiles
    with the core name in the save_subpath (e.g. "retroarch/saves/SwanStation").
    """

    def scan_saves(
        self,
        saves_root: Path,
        enabled_emulators: list[str] | None = None,
    ) -> list[ScannedFile]:
        results: list[ScannedFile] = []
        for profile in PROFILES:
            if not profile.supported:
                profile_dir = saves_root / profile.save_subpath
                if profile_dir.exists():
                    _log.warning(
                        "save profile %r (%s) found at %s but is not yet supported "
                        "(mapping=%s); skipping",
                        profile.emulator,
                        profile.platform,
                        profile_dir,
                        profile.mapping,
                    )
                continue

            if enabled_emulators and profile.emulator not in enabled_emulators:
                continue

            profile_dir = saves_root / profile.save_subpath
            if not profile_dir.exists():
                continue

            results.extend(self._scan_profile_dir(profile_dir, profile))

        return sorted(results, key=lambda f: f.path)

    def _scan_profile_dir(self, profile_dir: Path, profile: SaveProfile) -> list[ScannedFile]:
        found: list[ScannedFile] = []
        try:
            entries = list(profile_dir.iterdir())
        except OSError:
            return found
        for path in entries:
            if path.is_dir() or path.name.startswith("."):
                continue
            try:
                if self._matches(path.name, profile.include_globs) and not self._matches(
                    path.name, profile.exclude_globs
                ):
                    found.append(ScannedFile(path=path, profile=profile))
            except OSError:
                pass
        return found

    @staticmethod
    def _matches(name: str, globs: tuple[str, ...]) -> bool:
        if not globs:
            return False
        name_lower = name.lower()
        return any(fnmatch.fnmatch(name_lower, g.lower()) for g in globs)
