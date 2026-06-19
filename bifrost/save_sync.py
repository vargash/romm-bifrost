"""Save sync preview helpers for the first RomM tranche."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bifrost.api.client import RommApiClient
from bifrost.api.models import (
    ClientSaveState,
    RomSummary,
    SaveSummary,
    SyncCompletePayload,
    SyncNegotiatePayload,
    SyncOperationSchema,
)
from bifrost.config import AppConfig
from bifrost.errors import ApiError
from bifrost.errors import ConfigError


@dataclass(frozen=True)
class LocalSaveFile:
    path: Path
    file_name: str
    file_size_bytes: int
    updated_at: str
    content_hash: str


@dataclass(frozen=True)
class SaveSyncPreview:
    device_id: str
    scanned_files: int
    mapped_files: int
    skipped_files: int
    local_saves: list[ClientSaveState]
    operations: list[SyncOperationSchema]
    skipped_paths: list[Path]
    session_id: int | None = None


@dataclass(frozen=True)
class SaveSyncExecutionResult:
    executed: int
    failed: int
    skipped: int
    details: list[tuple[str, str, str]]


def _expand_path(path_value: str) -> Path:
    return Path(path_value).expanduser()


def _hash_file(path: Path) -> str:
    # RomM save content_hash is MD5 in current API responses.
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_local_save_files(root: Path) -> list[LocalSaveFile]:
    if not root.exists():
        return []

    save_files: list[LocalSaveFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            try:
                stat_result = path.stat()
                updated_at = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat()
                save_files.append(
                    LocalSaveFile(
                        path=path,
                        file_name=path.name,
                        file_size_bytes=stat_result.st_size,
                        updated_at=updated_at,
                        content_hash=_hash_file(path),
                    )
                )
            except (OSError, PermissionError):
                pass

    return sorted(save_files, key=lambda f: f.path)


_MULTISPACE_RE = re.compile(r"\s+")
_TRAILING_TAG_RE = re.compile(r"\s*\[[^\]]+\]$")
_STATE_FILE_RE = re.compile(r"\.state\d*(\.png)?$", re.IGNORECASE)
_SUPPORTED_SAVE_EXTENSIONS = {
    ".srm",
    ".sav",
    ".mcr",
    ".mcd",
    ".dsv",
    ".fla",
    ".eep",
    ".sra",
    ".gme",
    ".mem",
    ".vmp",
    ".vm1",
    ".vm2",
    ".nv",
    ".hi",
}


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


def _is_syncable_save_file(path: Path) -> bool:
    name = path.name.lower()
    if _STATE_FILE_RE.search(name):
        return False
    if name.endswith(".png"):
        return False
    return path.suffix.lower() in _SUPPORTED_SAVE_EXTENSIONS


def _matches_file_filter(value: str, file_filters: list[str] | None) -> bool:
    if not file_filters:
        return True
    lowered = value.lower()
    for item in file_filters:
        selector = item.strip().lower()
        if not selector:
            continue
        if lowered == selector or selector in lowered:
            return True
    return False


def _build_remote_rom_index(remote_roms: list[RomSummary]) -> dict[str, RomSummary]:
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


def _build_local_save_state(
    local_save: LocalSaveFile,
    remote_index: dict[str, RomSummary],
) -> tuple[ClientSaveState | None, str | None]:
    file_name_no_ext = local_save.path.stem
    stripped_name = _strip_trailing_tags(file_name_no_ext)
    candidates = [local_save.file_name, file_name_no_ext, stripped_name]

    remote: RomSummary | None = None
    for candidate in candidates:
        normalized = _normalize_name(candidate)
        if not normalized:
            continue
        remote = remote_index.get(normalized)
        if remote is not None:
            break

    if remote is None:
        return None, None

    return (
        ClientSaveState(
            rom_id=remote.id,
            file_name=local_save.file_name,
            slot=None,
            emulator=None,
            content_hash=local_save.content_hash,
            updated_at=local_save.updated_at,
            file_size_bytes=local_save.file_size_bytes,
        ),
        remote.name,
    )


def _save_lookup_key(rom_id: int, file_name: str) -> tuple[int, str]:
    path = Path(file_name)
    stem = _normalize_name(_strip_trailing_tags(path.stem))
    extension = path.suffix.lower()
    return (rom_id, f"{stem}{extension}")


def _build_remote_save_index(remote_saves: list[SaveSummary]) -> dict[tuple[int, str], SaveSummary]:
    index: dict[tuple[int, str], SaveSummary] = {}
    for save in remote_saves:
        index.setdefault(_save_lookup_key(save.rom_id, save.file_name), save)
    return index


def _is_redundant_upload_operation(
    operation: SyncOperationSchema,
    local_state: ClientSaveState | None,
    remote_save: SaveSummary | None,
) -> bool:
    if operation.action != "upload" or local_state is None or remote_save is None:
        return False

    if remote_save.content_hash and local_state.content_hash:
        return remote_save.content_hash.lower() == local_state.content_hash.lower()

    if remote_save.file_size_bytes is not None:
        return int(remote_save.file_size_bytes) == int(local_state.file_size_bytes)

    return False


def _lookup_local_file_for_operation(
    operation: SyncOperationSchema,
    local_index: dict[str, list[LocalSaveFile]],
) -> LocalSaveFile | None:
    candidates = local_index.get(operation.file_name.lower())
    if not candidates:
        return None
    return candidates[0]


def build_save_sync_preview(
    config: AppConfig,
    client: RommApiClient,
    device_id: str | None = None,
    file_filters: list[str] | None = None,
) -> SaveSyncPreview:
    resolved_device_id = (device_id or config.romm.device_id).strip()
    if not resolved_device_id:
        raise ConfigError(
            "Save sync requires a RomM device_id. Run 'bifrost device-enroll' first, "
            "or pass --device-id."
        )

    save_root = _expand_path(config.emudeck.saves_path)
    all_local_files = scan_local_save_files(save_root)
    local_files = [
        item
        for item in all_local_files
        if _matches_file_filter(item.file_name, file_filters)
        or _matches_file_filter(str(item.path), file_filters)
    ]
    remote_roms = client.list_roms()
    remote_index = _build_remote_rom_index(remote_roms)
    remote_saves = client.list_saves(device_id=resolved_device_id)
    remote_save_index = _build_remote_save_index(remote_saves)

    local_states: list[ClientSaveState] = []
    local_state_index: dict[tuple[int, str], ClientSaveState] = {}
    skipped_paths: list[Path] = []
    for local_save in local_files:
        if not _is_syncable_save_file(local_save.path):
            skipped_paths.append(local_save.path)
            continue
        state, _ = _build_local_save_state(local_save, remote_index)
        if state is None:
            skipped_paths.append(local_save.path)
            continue
        local_states.append(state)
        local_state_index[_save_lookup_key(state.rom_id, state.file_name)] = state

    negotiate_response = client.negotiate_sync(
        SyncNegotiatePayload(device_id=resolved_device_id, saves=local_states)
    )

    filtered_operations = [
        operation
        for operation in negotiate_response.operations
        if not _is_redundant_upload_operation(
            operation,
            local_state_index.get(_save_lookup_key(operation.rom_id, operation.file_name)),
            remote_save_index.get(_save_lookup_key(operation.rom_id, operation.file_name)),
        )
    ]

    return SaveSyncPreview(
        device_id=resolved_device_id,
        scanned_files=len(local_files),
        mapped_files=len(local_states),
        skipped_files=len(skipped_paths),
        local_saves=local_states,
        operations=filtered_operations,
        skipped_paths=skipped_paths,
        session_id=negotiate_response.session_id,
    )


def execute_save_sync_preview(
    config: AppConfig,
    client: RommApiClient,
    preview: SaveSyncPreview,
    file_filters: list[str] | None = None,
) -> SaveSyncExecutionResult:
    """Execute selected sync operations from a previously negotiated preview."""

    save_root = _expand_path(config.emudeck.saves_path)
    local_files = scan_local_save_files(save_root)
    local_index: dict[str, list[LocalSaveFile]] = {}
    for local_save in local_files:
        local_index.setdefault(local_save.file_name.lower(), []).append(local_save)

    selected_ops = [
        op
        for op in preview.operations
        if _matches_file_filter(op.file_name, file_filters)
    ]

    remote_save_index: dict[tuple[int, str], SaveSummary] | None = None

    completed = 0
    failed = 0
    skipped = 0
    details: list[tuple[str, str, str]] = []

    for operation in selected_ops:
        if operation.action not in {"upload", "download"}:
            skipped += 1
            details.append((operation.action, operation.file_name, "skipped (non-transfer operation)"))
            continue

        try:
            if operation.action == "upload":
                local_file = _lookup_local_file_for_operation(operation, local_index)
                if local_file is None:
                    skipped += 1
                    details.append(("upload", operation.file_name, "skipped (local file not found)"))
                    continue
                try:
                    upload_response = client.upload_save_file(
                        rom_id=operation.rom_id,
                        save_path=local_file.path,
                        device_id=preview.device_id,
                        session_id=preview.session_id,
                        save_id=operation.save_id,
                        overwrite=True,
                    )
                except ApiError as exc:
                    # Some RomM builds may return 500 on create when the save already exists.
                    if operation.save_id is None:
                        if remote_save_index is None:
                            device_scoped = client.list_saves(device_id=preview.device_id)
                            global_scoped = client.list_saves()
                            remote_save_index = {}
                            for save in [*device_scoped, *global_scoped]:
                                remote_save_index.setdefault(
                                    _save_lookup_key(save.rom_id, save.file_name),
                                    save,
                                )
                        existing = remote_save_index.get(
                            _save_lookup_key(operation.rom_id, operation.file_name)
                        )
                        if existing is not None:
                            upload_response = client.upload_save_file(
                                rom_id=operation.rom_id,
                                save_path=local_file.path,
                                device_id=preview.device_id,
                                session_id=preview.session_id,
                                save_id=existing.id,
                                overwrite=True,
                            )
                        else:
                            raise exc
                    else:
                        raise exc
                uploaded_id = upload_response.get("id") if isinstance(upload_response, dict) else None
                if isinstance(uploaded_id, int):
                    client.track_save(uploaded_id, preview.device_id)
                completed += 1
                details.append(("upload", operation.file_name, "ok"))
                continue

            if operation.save_id is None:
                failed += 1
                details.append(("download", operation.file_name, "failed (missing save_id)"))
                continue

            content = client.download_save_file_content(
                save_id=operation.save_id,
                device_id=preview.device_id,
                session_id=preview.session_id,
            )
            destination = save_root / operation.file_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            client.confirm_save_download(save_id=operation.save_id, device_id=preview.device_id)
            completed += 1
            details.append(("download", operation.file_name, f"ok -> {destination}"))
        except Exception as exc:
            failed += 1
            details.append((operation.action, operation.file_name, f"failed ({exc})"))

    if preview.session_id is not None:
        client.complete_sync_session(
            preview.session_id,
            SyncCompletePayload(
                operations_completed=completed,
                operations_failed=failed,
            ),
        )

    return SaveSyncExecutionResult(
        executed=completed,
        failed=failed,
        skipped=skipped,
        details=details,
    )