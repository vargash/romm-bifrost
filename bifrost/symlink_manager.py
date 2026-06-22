"""Symlink planning and application."""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from bifrost.api.client import RommApiClient
from bifrost.config import AppConfig

# For most RomM asset types the preferred file is <type>.png.
# Exceptions are listed here explicitly.  Use _MANUAL_ASSET_FILE for types
# whose filename depends on the ROM's numeric id (e.g. "1046.pdf").
_PREFERRED_ASSET_FILE: dict[str, str] = {
    "cover": "big.png",                          # RomM generates big/small variants for covers
    "video_normalized": "video_normalized.mp4",  # video, not image
    "physical": "physical.png",
    "screenshots": "0.png",                      # first screenshot, 0-indexed
}
_ROM_ID_NAMED: frozenset[str] = frozenset({"manual"})  # filename is "<rom_id>.pdf"


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

    for rom in roms:
        if rom.platform_id is None or not rom.fs_name:
            continue
        slug = platform_slug_by_id.get(rom.platform_id)
        if not slug:
            continue
        rom_stem = Path(rom.fs_name).stem
        for romm_asset_type, esde_folder in config.assets.folder_map.items():
            if romm_asset_type in _ROM_ID_NAMED:
                pref_file = f"{rom.id}.pdf"
            else:
                pref_file = _PREFERRED_ASSET_FILE.get(romm_asset_type, f"{romm_asset_type}.png")
            ext = Path(pref_file).suffix
            destination = media_root / slug / esde_folder / f"{rom_stem}{ext}"
            target = (
                resources_root
                / str(rom.platform_id)
                / str(rom.id)
                / romm_asset_type
                / pref_file
            )
            ops.append(
                SymlinkOperation(
                    category="asset",
                    destination=destination,
                    target=target,
                    is_dir=False,
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

    # Old asset-dir directory symlinks (legacy flat NAS structure).
    # These are directory-level symlinks like downloaded_media/psx/covers → NAS/resources/roms/<id>/covers.
    # They point under resources_root, not nas_root, so we check against resources_root.
    # The new per-game approach creates file symlinks inside real directories, so these must go.
    media_root = _normalize_path(config.emudeck.media_path)
    resources_root = _normalize_path(config.nas.resources_path)
    planned_asset_parents: set[Path] = {
        op.destination.parent for op in ops if op.category == "asset"
    }
    for slug_dir in (media_root.iterdir() if media_root.is_dir() else []):
        if not slug_dir.is_dir():
            continue
        for item in slug_dir.iterdir():
            if item.is_symlink() and item not in planned_asset_parents:
                if not item.exists() or _is_bifrost_symlink(item, resources_root):
                    remove_ops.append(RemoveSymlinkOperation(category="asset-dir", destination=item))

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

    # Don't create a symlink when the NAS is reachable but the target is absent.
    # Check both parent and grandparent: an asset type dir (parent) may not exist even
    # when the NAS is up — the grandparent (ROM asset root) is enough to confirm reachability.
    if not op.target.exists() and (
        op.target.parent.is_dir() or op.target.parent.parent.is_dir()
    ):
        return OperationResult(op, "missing-target", "NAS target does not exist")

    parent = op.destination.parent
    if parent.is_symlink():
        # Replace any legacy directory-level symlink (broken or valid) with a real directory
        # so per-ROM asset files land locally instead of inside the NAS tree.
        try:
            parent.unlink()
        except OSError as exc:
            return OperationResult(op, "error", f"Failed to remove stale parent symlink: {exc}")
    try:
        parent.mkdir(parents=True, exist_ok=True)
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
