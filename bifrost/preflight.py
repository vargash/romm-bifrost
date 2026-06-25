"""Pre-flight checks for --apply commands.

Validates local paths and RomM reachability before any filesystem writes.
All checks produce explicit, human-readable failure messages rather than
letting downstream code fail with generic OS errors.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bifrost.config import AppConfig
from bifrost.errors import ApiError, AuthenticationError, NetworkError

if TYPE_CHECKING:
    from bifrost.api.client import RommApiClient

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


def run_nas_check(config: AppConfig) -> PreflightResult:
    """Verify NAS library and resources paths are accessible.

    Always runs, even in dry-run mode.  If the NAS is down there is no
    point planning or applying a sync: all ROM targets would be broken and
    stale-removal would be triggered incorrectly.
    """
    result = PreflightResult()
    nas_lib = Path(config.nas.library_path).expanduser()
    nas_res = Path(config.nas.resources_path).expanduser()
    _check_path_accessible("NAS library", nas_lib, result)
    _check_path_accessible("NAS resources", nas_res, result)
    return result


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


def run_save_api_preflight(config: AppConfig, client: "RommApiClient") -> PreflightResult:
    """API-level pre-flight for save-sync --apply (requires an open RommApiClient).

    Checks:
    (b) device_id configured + device exists in RomM
    (c) POST /api/sync/negotiate capability ping:
        200  → modern negotiate available
        404/405 → legacy fallback mode (warn)
        401/403 → token missing sync scopes (error)
        other  → warn only (fail-open; RomM may be temporarily degraded)
    """
    from bifrost.api.models import SyncNegotiatePayload

    result = PreflightResult()

    device_id = config.romm.device_id.strip()
    if not device_id:
        result.errors.append(
            "No device_id configured. Run 'bifrost device-enroll' first."
        )
        return result

    # (b) Device exists
    try:
        client.get_device(device_id)
    except ApiError as exc:
        if exc.http_status == 404:
            result.errors.append(
                f"Device '{device_id}' not found in RomM. "
                "Run 'bifrost device-enroll --replace' to re-register."
            )
        elif exc.http_status in {401, 403}:
            result.errors.append(
                f"Token lacks permission to read device info (HTTP {exc.http_status}). "
                "Check token scopes in RomM."
            )
        else:
            result.warnings.append(
                f"Could not verify device '{device_id}' (HTTP {exc.http_status}): {exc}"
            )
    except (NetworkError, AuthenticationError) as exc:
        result.warnings.append(f"Could not verify device (network/auth error): {exc}")

    if not result.ok:
        return result

    # (c) Negotiate capability ping
    try:
        client.negotiate_sync(SyncNegotiatePayload(device_id=device_id, saves=[]))
    except ApiError as exc:
        if exc.http_status in {404, 405}:
            result.warnings.append(
                f"RomM /sync/negotiate not available (HTTP {exc.http_status}) — "
                "legacy upload fallback will be used. "
                "Set romm.legacy_upload_fallback = true in config.toml to suppress this warning."
            )
        elif exc.http_status in {401, 403}:
            result.errors.append(
                f"Token missing sync scopes (HTTP {exc.http_status}). "
                "Ensure the token has 'saves.write' and 'sync' permissions in RomM."
            )
        else:
            result.warnings.append(
                f"Negotiate capability check returned HTTP {exc.http_status} — "
                "sync may use legacy fallback."
            )
    except (NetworkError, AuthenticationError) as exc:
        result.warnings.append(f"Negotiate capability check failed (will proceed): {exc}")

    return result
