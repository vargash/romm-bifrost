"""State sync helpers for RomM state files (.state, .state1, ...)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bifrost.api.client import RommApiClient
from bifrost.api.models import RomSummary, StateSummary
from bifrost.config import AppConfig
from bifrost.errors import ApiError

_STATE_FILE_RE = re.compile(r"\.state\d*$", re.IGNORECASE)
_MULTISPACE_RE = re.compile(r"\s+")
_TRAILING_TAG_RE = re.compile(r"\s*\[[^\]]+\]$")


@dataclass(frozen=True)
class LocalStateFile:
    path: Path
    file_name: str
    file_size_bytes: int
    updated_at: str
    content_hash: str


@dataclass(frozen=True)
class StateSyncOperation:
    action: str
    rom_id: int
    file_name: str
    reason: str
    state_id: int | None = None


@dataclass(frozen=True)
class StateSyncPreview:
    scanned_files: int
    mapped_files: int
    skipped_files: int
    operations: list[StateSyncOperation]
    skipped_paths: list[Path]


@dataclass(frozen=True)
class StateSyncExecutionResult:
    executed: int
    failed: int
    skipped: int
    details: list[tuple[str, str, str]]


def _expand_path(path_value: str) -> Path:
    return Path(path_value).expanduser()


def _hash_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_trailing_tags(value: str) -> str:
    normalized = value
    while True:
        updated = _TRAILING_TAG_RE.sub("", normalized)
        if updated == normalized:
            break
        normalized = updated
    return normalized.strip()


def _normalize_name(value: str) -> str:
    lowered = value.strip().lower().replace("_", " ").replace(".", " ")
    lowered = _strip_trailing_tags(lowered)
    lowered = _MULTISPACE_RE.sub(" ", lowered)
    return lowered.strip()


def _matches_filter(value: str, file_filters: list[str] | None) -> bool:
    if not file_filters:
        return True
    lowered = value.lower()
    for item in file_filters:
        query = item.strip().lower()
        if query and query in lowered:
            return True
    return False


def _is_state_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".png"):
        return False
    return _STATE_FILE_RE.search(name) is not None


def scan_local_state_files(root: Path) -> list[LocalStateFile]:
    if not root.exists():
        return []

    state_files: list[LocalStateFile] = []
    for dirpath, _, filenames in os.walk(root, followlinks=True):
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if not _is_state_file(path):
                continue
            try:
                stat_result = path.stat()
                updated_at = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat()
                state_files.append(
                    LocalStateFile(
                        path=path,
                        file_name=path.name,
                        file_size_bytes=stat_result.st_size,
                        updated_at=updated_at,
                        content_hash=_hash_file(path),
                    )
                )
            except (OSError, PermissionError):
                pass

    return sorted(state_files, key=lambda item: item.path)


def _build_rom_index(remote_roms: list[RomSummary]) -> dict[str, RomSummary]:
    index: dict[str, RomSummary] = {}
    for rom in remote_roms:
        candidates: list[str] = []
        if rom.name:
            candidates.append(rom.name)
        if rom.fs_name:
            candidates.append(Path(rom.fs_name).stem)
            candidates.append(rom.fs_name)
        if rom.fs_path:
            candidates.append(Path(rom.fs_path).stem)
            candidates.append(Path(rom.fs_path).name)
        if rom.full_path:
            candidates.append(Path(rom.full_path).stem)
            candidates.append(Path(rom.full_path).name)

        for candidate in candidates:
            normalized = _normalize_name(candidate)
            if normalized:
                index.setdefault(normalized, rom)
    return index


def _build_state_lookup_key(rom_id: int, file_name: str) -> tuple[int, str]:
    path = Path(file_name)
    stem = _normalize_name(_strip_trailing_tags(path.stem))
    return (rom_id, f"{stem}{path.suffix.lower()}")


def _map_local_state_to_rom(
    local_state: LocalStateFile,
    rom_index: dict[str, RomSummary],
) -> RomSummary | None:
    stem = local_state.path.stem
    base_name = re.sub(r"\.state\d*$", "", stem, flags=re.IGNORECASE)
    candidates = [local_state.file_name, stem, base_name]

    for candidate in candidates:
        normalized = _normalize_name(candidate)
        if not normalized:
            continue
        rom = rom_index.get(normalized)
        if rom is not None:
            return rom
    return None


def build_state_sync_preview(
    config: AppConfig,
    client: RommApiClient,
    file_filters: list[str] | None = None,
) -> StateSyncPreview:
    root = _expand_path(config.emudeck.saves_path)
    all_local_states = scan_local_state_files(root)
    local_states = [
        item
        for item in all_local_states
        if _matches_filter(item.file_name, file_filters) or _matches_filter(str(item.path), file_filters)
    ]

    rom_index = _build_rom_index(client.list_roms())
    remote_states = client.list_states()
    remote_index: dict[tuple[int, str], StateSummary] = {
        _build_state_lookup_key(state.rom_id, state.file_name): state for state in remote_states
    }

    operations: list[StateSyncOperation] = []
    skipped_paths: list[Path] = []
    mapped = 0

    for local_state in local_states:
        rom = _map_local_state_to_rom(local_state, rom_index)
        if rom is None:
            skipped_paths.append(local_state.path)
            continue

        mapped += 1
        key = _build_state_lookup_key(rom.id, local_state.file_name)
        remote = remote_index.get(key)
        if remote is None:
            operations.append(
                StateSyncOperation(
                    action="upload",
                    rom_id=rom.id,
                    file_name=local_state.file_name,
                    reason="State exists on client but not on server",
                    state_id=None,
                )
            )
            continue

        # Already in sync: same content hash
        if remote.content_hash and local_state.content_hash:
            if remote.content_hash.lower() == local_state.content_hash.lower():
                continue
        elif remote.file_size_bytes is not None and int(remote.file_size_bytes) == local_state.file_size_bytes:
            continue

        operations.append(
            StateSyncOperation(
                action="upload",
                rom_id=rom.id,
                file_name=local_state.file_name,
                reason="State differs from server copy",
                state_id=remote.id,
            )
        )

    return StateSyncPreview(
        scanned_files=len(local_states),
        mapped_files=mapped,
        skipped_files=len(skipped_paths),
        operations=operations,
        skipped_paths=skipped_paths,
    )


def execute_state_sync_preview(
    config: AppConfig,
    client: RommApiClient,
    preview: StateSyncPreview,
    file_filters: list[str] | None = None,
) -> StateSyncExecutionResult:
    root = _expand_path(config.emudeck.saves_path)
    local_states = scan_local_state_files(root)
    local_index: dict[str, list[LocalStateFile]] = {}
    for item in local_states:
        local_index.setdefault(item.file_name.lower(), []).append(item)

    selected_ops = [op for op in preview.operations if _matches_filter(op.file_name, file_filters)]

    executed = 0
    failed = 0
    skipped = 0
    details: list[tuple[str, str, str]] = []

    remote_index_cache: dict[tuple[int, str], StateSummary] | None = None

    for operation in selected_ops:
        matches = local_index.get(operation.file_name.lower())
        local_state = matches[0] if matches else None
        if local_state is None:
            skipped += 1
            details.append(("upload", operation.file_name, "skipped (local state not found)"))
            continue

        try:
            try:
                client.upload_state_file(
                    rom_id=operation.rom_id,
                    state_path=local_state.path,
                    state_id=operation.state_id,
                )
            except ApiError as exc:
                if operation.state_id is None:
                    if remote_index_cache is None:
                        remote_index_cache = {
                            _build_state_lookup_key(state.rom_id, state.file_name): state
                            for state in client.list_states(rom_id=operation.rom_id)
                        }
                    existing = remote_index_cache.get(
                        _build_state_lookup_key(operation.rom_id, operation.file_name)
                    )
                    if existing is None:
                        raise exc
                    client.upload_state_file(
                        rom_id=operation.rom_id,
                        state_path=local_state.path,
                        state_id=existing.id,
                    )
                else:
                    raise exc

            executed += 1
            details.append(("upload", operation.file_name, "ok"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            details.append(("upload", operation.file_name, f"failed ({exc})"))

    return StateSyncExecutionResult(
        executed=executed,
        failed=failed,
        skipped=skipped,
        details=details,
    )
