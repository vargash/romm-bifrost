"""Symlink planning and application for F2."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bifrost.api.client import RommApiClient
from bifrost.config import AppConfig


@dataclass(frozen=True)
class SymlinkOperation:
    category: str
    destination: Path
    target: Path
    is_dir: bool


@dataclass(frozen=True)
class RemoveSymlinkOperation:
    """Remove a stale symlink that is no longer in the sync plan."""

    category: str
    destination: Path

    @property
    def target(self) -> Path:
        try:
            raw = os.readlink(self.destination)
            t = Path(raw)
            return t if t.is_absolute() else self.destination.parent / t
        except OSError:
            return self.destination

    @property
    def is_dir(self) -> bool:
        return False


@dataclass(frozen=True)
class OperationResult:
    operation: SymlinkOperation | RemoveSymlinkOperation
    action: str
    detail: str = ""


def _normalize_path(path_value: str) -> Path:
    return Path(path_value).expanduser()


def _rom_target_path(roms_root: Path, path_value: str, fs_name: str | None) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0] == "roms":
        candidate = Path(*candidate.parts[1:])
    if fs_name and candidate.name != fs_name:
        candidate = candidate / fs_name
    return roms_root / candidate


def _bios_target_path(bios_root: Path, file_path: str, file_name: str) -> Path:
    candidate = Path(file_path)
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0] == "bios":
        candidate = Path(*candidate.parts[1:])
    if not str(candidate) or candidate.name != file_name:
        candidate = candidate / file_name
    return bios_root / candidate


def plan_symlink_operations(config: AppConfig, client: RommApiClient) -> list[SymlinkOperation]:
    platforms = client.list_platforms()
    roms = client.list_roms()
    firmware_items = client.list_firmware()

    platform_slug_by_id = {p.id: p.fs_slug for p in platforms if p.fs_slug}

    roms_root = _normalize_path(config.nas.library_path) / config.nas.roms_subpath
    bios_root = _normalize_path(config.nas.library_path) / config.nas.bios_subpath
    resources_root = _normalize_path(config.nas.resources_path) / "roms"

    esde_roms_root = _normalize_path(config.esde.roms_path)
    emudeck_bios_root = _normalize_path(config.emudeck.bios_path)
    media_root = _normalize_path(config.emudeck.media_path)

    ops: list[SymlinkOperation] = []

    for rom in roms:
        path_hint = rom.full_path or rom.fs_path
        if rom.platform_id is None or not rom.fs_name or not path_hint:
            continue
        slug = platform_slug_by_id.get(rom.platform_id)
        if not slug:
            continue

        destination = esde_roms_root / slug / rom.fs_name
        target = _rom_target_path(roms_root, path_hint, rom.fs_name)
        ops.append(
            SymlinkOperation(
                category="rom",
                destination=destination,
                target=target,
                is_dir=False,
            )
        )

    for fw in firmware_items:
        file_name = fw.get("file_name")
        file_path = fw.get("file_path")
        if not isinstance(file_name, str) or not file_name:
            continue

        if isinstance(file_path, str) and file_path:
            target = _bios_target_path(bios_root, file_path, file_name)
        else:
            target = bios_root / file_name

        destination = emudeck_bios_root / file_name
        ops.append(
            SymlinkOperation(
                category="bios",
                destination=destination,
                target=target,
                is_dir=False,
            )
        )

    for platform in platforms:
        if not platform.fs_slug:
            continue
        for romm_folder, esde_folder in config.assets.folder_map.items():
            destination = media_root / platform.fs_slug / esde_folder
            target = resources_root / str(platform.id) / romm_folder
            ops.append(
                SymlinkOperation(
                    category="asset-dir",
                    destination=destination,
                    target=target,
                    is_dir=True,
                )
            )

    return ops


def _is_bifrost_symlink(path: Path, nas_root: Path) -> bool:
    """True if path is a symlink whose target falls under nas_root (bifrost-managed)."""
    if not path.is_symlink():
        return False
    try:
        raw = os.readlink(str(path))
    except OSError:
        return False
    t = Path(raw)
    if not t.is_absolute():
        t = path.parent / t
    return str(t.resolve(strict=False)).startswith(str(nas_root.resolve()))


def plan_stale_removals(
    config: AppConfig,
    ops: list[SymlinkOperation],
) -> list[RemoveSymlinkOperation]:
    """Find symlinks in bifrost-managed directories that should be removed.

    Scans ROM platform directories and the BIOS directory.  A symlink is
    flagged for removal when:
    - it is not in the current plan AND (its target is missing OR it points under the NAS root), OR
    - it IS in the plan but its target is missing on the NAS and the NAS is reachable
      (handles cache-stale plans where a ROM was deleted from RomM but the cache still lists it).

    The NAS-reachable guard prevents mass removal when the NAS is temporarily offline.
    Only symlinks are touched; regular files and directories are left alone.
    """
    planned_destinations: set[Path] = {op.destination for op in ops}
    nas_root = _normalize_path(config.nas.library_path)
    esde_roms_root = _normalize_path(config.esde.roms_path)
    emudeck_bios_root = _normalize_path(config.emudeck.bios_path)
    nas_accessible = nas_root.is_dir()

    remove_ops: list[RemoveSymlinkOperation] = []

    # ROM symlinks: only scan platform dirs that have at least one planned ROM op.
    managed_slugs: set[str] = {
        op.destination.parent.name for op in ops if op.category == "rom"
    }
    for slug in managed_slugs:
        platform_dir = esde_roms_root / slug
        if not platform_dir.is_dir():
            continue
        for item in platform_dir.iterdir():
            if not item.is_symlink():
                continue
            if item in planned_destinations:
                if nas_accessible and not item.exists():
                    remove_ops.append(RemoveSymlinkOperation(category="rom", destination=item))
                continue
            if not item.exists() or _is_bifrost_symlink(item, nas_root):
                remove_ops.append(RemoveSymlinkOperation(category="rom", destination=item))

    # BIOS symlinks.
    if emudeck_bios_root.is_dir():
        planned_bios: set[Path] = {op.destination for op in ops if op.category == "bios"}
        for item in emudeck_bios_root.iterdir():
            if not item.is_symlink():
                continue
            if item in planned_bios:
                if nas_accessible and not item.exists():
                    remove_ops.append(RemoveSymlinkOperation(category="bios", destination=item))
                continue
            if not item.exists() or _is_bifrost_symlink(item, nas_root):
                remove_ops.append(RemoveSymlinkOperation(category="bios", destination=item))

    return remove_ops


def evaluate_operation(op: SymlinkOperation) -> OperationResult:
    dest = op.destination
    target = op.target

    if dest.exists() or dest.is_symlink():
        if dest.is_symlink():
            resolved_dest = dest.resolve(strict=False)
            resolved_target = target.resolve(strict=False)
            if resolved_dest == resolved_target:
                if dest.exists():
                    return OperationResult(op, "ok")
                return OperationResult(op, "broken", "Symlink target missing on NAS")
            return OperationResult(op, "replace")
        return OperationResult(op, "conflict", "Destination exists and is not a symlink")

    return OperationResult(op, "create")


def evaluate_remove_operation(op: RemoveSymlinkOperation) -> OperationResult:
    if not op.destination.is_symlink():
        return OperationResult(op, "skip", "Not a symlink")
    return OperationResult(op, "remove")


def apply_operations(ops: list[SymlinkOperation]) -> list[OperationResult]:
    """Apply operations and continue on per-item filesystem failures."""

    results: list[OperationResult] = []
    for op in ops:
        results.append(apply_operation(op))

    return results


def apply_operation(op: SymlinkOperation) -> OperationResult:
    """Apply one operation and return a structured status without raising."""

    eval_result = evaluate_operation(op)
    action = eval_result.action

    if action in {"ok", "conflict", "broken"}:
        return eval_result

    # Don't (re-)create a symlink if the NAS parent directory is reachable but the target file is gone.
    if not op.target.exists() and op.target.parent.is_dir():
        return OperationResult(op, "missing-target", "NAS target does not exist")

    try:
        op.destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return OperationResult(op, "error", f"Failed to create parent directory: {exc}")

    if action == "replace" and op.destination.is_symlink():
        try:
            op.destination.unlink()
        except OSError as exc:
            return OperationResult(op, "error", f"Failed to replace existing symlink: {exc}")

    try:
        op.destination.symlink_to(op.target, target_is_directory=op.is_dir)
    except OSError as exc:
        return OperationResult(op, "error", f"Failed to create symlink: {exc}")

    return OperationResult(op, action)


def apply_remove_operation(op: RemoveSymlinkOperation) -> OperationResult:
    """Remove a stale symlink atomically."""
    eval_result = evaluate_remove_operation(op)
    if eval_result.action != "remove":
        return eval_result
    try:
        op.destination.unlink()
        return OperationResult(op, "remove")
    except OSError as exc:
        return OperationResult(op, "error", str(exc))
