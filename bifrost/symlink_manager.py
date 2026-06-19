"""Symlink planning and application for F2."""

from __future__ import annotations

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
class OperationResult:
    operation: SymlinkOperation
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


def evaluate_operation(op: SymlinkOperation) -> OperationResult:
    dest = op.destination
    target = op.target

    if dest.exists() or dest.is_symlink():
        if dest.is_symlink():
            resolved_dest = dest.resolve(strict=False)
            resolved_target = target.resolve(strict=False)
            if resolved_dest == resolved_target:
                return OperationResult(op, "ok")
            return OperationResult(op, "replace")
        return OperationResult(op, "conflict", "Destination exists and is not a symlink")

    return OperationResult(op, "create")


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

    if action in {"ok", "conflict"}:
        return eval_result

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
