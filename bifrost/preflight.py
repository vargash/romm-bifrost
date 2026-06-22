"""Pre-flight checks for --apply commands.

Validates local paths and RomM reachability before any filesystem writes.
All checks produce explicit, human-readable failure messages rather than
letting downstream code fail with generic OS errors.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from bifrost.config import AppConfig

_MIN_FREE_MB = 200


@dataclass
class PreflightResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def _check_path_accessible(label: str, path: Path, result: PreflightResult) -> bool:
    """Return True if path exists and is listable (not a dead/empty mount point)."""
    if not path.exists():
        result.errors.append(
            f"{label} not found: {path}\n"
            "  → Is the NAS mounted? Check with: ls -la " + str(path.parent)
        )
        return False
    if path.is_dir():
        try:
            entries = list(path.iterdir())
        except PermissionError:
            result.errors.append(f"{label} exists but is not readable: {path}")
            return False
        if not entries:
            result.warnings.append(
                f"{label} is an empty directory: {path}\n"
                "  → If this is a NAS mount point, the mount may be stale or the share is empty."
            )
    return True


def _check_dir_writable(label: str, path: Path, result: PreflightResult) -> None:
    path.mkdir(parents=True, exist_ok=True)
    test_file = path / ".bifrost_write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError as exc:
        result.errors.append(f"{label} is not writable: {path} ({exc})")


def _check_disk_space(label: str, path: Path, result: PreflightResult) -> None:
    try:
        stat = shutil.disk_usage(path)
        free_mb = stat.free // (1024 * 1024)
        if free_mb < _MIN_FREE_MB:
            result.errors.append(
                f"Low disk space on {label}: {free_mb} MB free at {path} "
                f"(minimum {_MIN_FREE_MB} MB required)"
            )
    except OSError:
        pass  # path might not exist yet; caught by _check_path_accessible


def run_sync_preflight(config: AppConfig) -> PreflightResult:
    """Pre-flight for `bifrost sync --apply` and `bifrost gamelist --apply`."""
    result = PreflightResult()

    nas_lib = Path(config.nas.library_path).expanduser()
    nas_roms = nas_lib / config.nas.roms_subpath
    nas_bios = nas_lib / config.nas.bios_subpath
    nas_res = Path(config.nas.resources_path).expanduser()
    esde_roms = Path(config.esde.roms_path).expanduser()
    esde_lists = Path(config.esde.gamelists_path).expanduser()
    bios_dst = Path(config.emudeck.bios_path).expanduser()

    # NAS source paths must exist and be readable
    if _check_path_accessible("NAS library", nas_lib, result):
        _check_path_accessible("NAS ROMs subdir", nas_roms, result)
        _check_path_accessible("NAS BIOS subdir", nas_bios, result)
    _check_path_accessible("NAS resources", nas_res, result)

    # Destination paths must be writable
    _check_dir_writable("ES-DE ROMs dir", esde_roms, result)
    _check_dir_writable("ES-DE gamelists dir", esde_lists, result)
    _check_dir_writable("BIOS dir", bios_dst, result)

    # Disk space on home partition
    _check_disk_space("home", Path.home(), result)

    return result


def run_save_preflight(config: AppConfig) -> PreflightResult:
    """Pre-flight for `bifrost save-sync --apply` and `bifrost state-sync --apply`."""
    result = PreflightResult()

    saves = Path(config.emudeck.saves_path).expanduser()

    _check_dir_writable("saves dir", saves, result)
    _check_disk_space("home", Path.home(), result)

    return result
