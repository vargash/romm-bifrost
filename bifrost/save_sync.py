"""Save sync: negotiate with RomM, upload/download save files."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bifrost.api.client import RommApiClient
from bifrost.api.models import (
    ClientSaveState,
    CompleteOutcome,
    DeviceSyncSchema,
    RomSummary,
    SaveSummary,
    SyncCompletePayload,
    SyncNegotiatePayload,
    SyncOperationSchema,
)
from bifrost.config import AppConfig
from bifrost.errors import ApiError, ConfigError
from bifrost.play_sessions import consume_pending_sessions
from bifrost.saves.layout import EmudeckEsdeLayout, ScannedFile

_log = logging.getLogger("bifrost.save_sync")

_MULTISPACE_RE = re.compile(r"\s+")
_TRAILING_TAG_RE = re.compile(r"\s*\[[^\]]+\]$")
_STATE_FILE_RE = re.compile(r"\.state\d*(\.png)?$", re.IGNORECASE)
_SLOT_SUFFIX_RE = re.compile(r"_\d+$")
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
    # Maps file_name (basename) → destination directory for profile-aware downloads.
    # Falls back to save_root when a file_name is not present.
    profile_destinations: dict[str, Path] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.profile_destinations is None:
            object.__setattr__(self, "profile_destinations", {})


@dataclass(frozen=True)
class SaveSyncExecutionResult:
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


def _scanned_to_local(sf: ScannedFile) -> LocalSaveFile:
    """Convert a layout-provider ScannedFile into a LocalSaveFile."""
    stat_result = sf.path.stat()
    return LocalSaveFile(
        path=sf.path,
        file_name=sf.path.name,
        file_size_bytes=stat_result.st_size,
        updated_at=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat(),
        content_hash=_hash_file(sf.path),
    )


def scan_local_save_files(root: Path) -> list[LocalSaveFile]:
    """Return all non-hidden files under root, regardless of extension.

    Callers are responsible for filtering by _is_syncable_save_file.
    Returning all files lets the preview report how many were found vs skipped.
    """
    if not root.exists():
        return []

    save_files: list[LocalSaveFile] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
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
    emulator: str | None = None,
    strip_slot_suffix: bool = False,
) -> tuple[ClientSaveState | None, str | None]:
    file_name_no_ext = local_save.path.stem
    if strip_slot_suffix:
        file_name_no_ext = _SLOT_SUFFIX_RE.sub("", file_name_no_ext)
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
            emulator=emulator,
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


def _find_device_sync(save: SaveSummary, device_id: str) -> DeviceSyncSchema | None:
    """Return the DeviceSyncSchema entry for device_id on this save, or None."""
    return next((ds for ds in save.device_syncs if ds.device_id == device_id), None)


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


def _is_redundant_download(operation: SyncOperationSchema, destination: Path) -> bool:
    """Return True if destination already matches the server's content hash."""
    server_hash = operation.server_content_hash
    if not server_hash:
        return False
    if not destination.exists():
        return False
    return _hash_file(destination).lower() == server_hash.lower()


def _backup_local_file(path: Path) -> Path | None:
    """Create <filename>.bak before overwriting. Returns backup path or None."""
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".bak")
    tmp = backup.with_name(backup.name + ".tmp")
    try:
        tmp.write_bytes(path.read_bytes())
        tmp.replace(backup)
    except OSError:
        tmp.unlink(missing_ok=True)
        return None
    _log.debug("backed up %s -> %s", path.name, backup.name)
    return backup


def _resolve_conflict_action(
    conflict_strategy: str,
    is_interactive: bool,
    file_name: str,
    rom_id: int,
) -> str:
    """Map a 'conflict' operation to upload/download/skip."""
    if conflict_strategy == "server_wins":
        return "download"
    if conflict_strategy == "local_wins":
        return "upload"
    # "ask" strategy
    if not is_interactive:
        _log.warning(
            "conflict auto-resolved as local_wins (headless, no TTY): %s (rom_id=%d)",
            file_name,
            rom_id,
        )
        return "upload"
    # Interactive mode: the CLI layer should provide conflict_overrides before calling execute.
    # If it hasn't (shouldn't happen), fall back to local_wins and log.
    _log.warning(
        "conflict for %s (rom_id=%d) reached execute without resolution — defaulting to local_wins",
        file_name,
        rom_id,
    )
    return "upload"


def _lookup_local_file_for_operation(
    operation: SyncOperationSchema,
    local_index: dict[str, list[LocalSaveFile]],
) -> LocalSaveFile | None:
    candidates = local_index.get(operation.file_name.lower())
    if not candidates:
        return None
    return candidates[0]


def _legacy_negotiate(
    local_states: list[ClientSaveState],
    remote_save_index: dict[tuple[int, str], SaveSummary],
    sync_mode: str,
    device_id: str = "",
) -> tuple[list[SyncOperationSchema], None]:
    """Fallback when /api/sync/negotiate is unavailable (older RomM server).

    Builds upload/download operations by direct local↔server comparison.
    Downloads are only added in push_pull mode.
    Saves marked is_current for our device are skipped (already in sync).
    """
    _log.warning(
        "using legacy negotiate fallback (no session tracking); %d local saves, %d remote saves",
        len(local_states),
        len(remote_save_index),
    )
    ops: list[SyncOperationSchema] = []
    local_keys = {_save_lookup_key(s.rom_id, s.file_name) for s in local_states}

    for state in local_states:
        key = _save_lookup_key(state.rom_id, state.file_name)
        remote = remote_save_index.get(key)
        if remote is None:
            ops.append(
                SyncOperationSchema(
                    action="upload",
                    rom_id=state.rom_id,
                    file_name=state.file_name,
                    reason="Save exists on client but not on server",
                )
            )
        else:
            ds = _find_device_sync(remote, device_id) if device_id else None
            if ds is not None and ds.is_current:
                _log.debug("legacy: skipping upload, already current: %s", state.file_name)
                continue
            if (
                state.content_hash
                and remote.content_hash
                and state.content_hash.lower() != remote.content_hash.lower()
            ):
                ops.append(
                    SyncOperationSchema(
                        action="upload",
                        rom_id=state.rom_id,
                        file_name=state.file_name,
                        save_id=remote.id,
                        reason="Client version differs from server",
                    )
                )

    if sync_mode == "push_pull":
        for key, remote in remote_save_index.items():
            if key not in local_keys:
                ops.append(
                    SyncOperationSchema(
                        action="download",
                        rom_id=remote.rom_id,
                        save_id=remote.id,
                        file_name=remote.file_name,
                        reason="Save exists on server but not on client",
                    )
                )

    return ops, None


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
    enabled_emulators = config.sync.profiles.enabled or None
    layout = EmudeckEsdeLayout()
    all_scanned = layout.scan_saves(save_root, enabled_emulators=enabled_emulators)

    # Apply optional file filters (--only-file / --rom-path scoping)
    scanned_files = [
        sf
        for sf in all_scanned
        if _matches_file_filter(sf.path.name, file_filters)
        or _matches_file_filter(str(sf.path), file_filters)
    ]

    # Build profile destination map for download path resolution in execute
    profile_destinations: dict[str, Path] = {
        sf.path.name: sf.path.parent for sf in all_scanned
    }

    remote_roms = client.list_roms()
    remote_rom_index = _build_remote_rom_index(remote_roms)
    remote_saves = client.list_saves(device_id=resolved_device_id)

    # Filter out saves this device has explicitly untracked — never sync them.
    untracked_save_ids: set[int] = set()
    for save in remote_saves:
        ds = _find_device_sync(save, resolved_device_id)
        if ds is not None and ds.is_untracked:
            untracked_save_ids.add(save.id)
            _log.debug("skipping untracked save: %s (id=%d)", save.file_name, save.id)

    tracked_remote_saves = [s for s in remote_saves if s.id not in untracked_save_ids]
    remote_save_index = _build_remote_save_index(tracked_remote_saves)

    local_states: list[ClientSaveState] = []
    local_state_index: dict[tuple[int, str], ClientSaveState] = {}
    skipped_paths: list[Path] = []

    for sf in scanned_files:
        local_save = _scanned_to_local(sf)
        state, _rom_name = _build_local_save_state(
            local_save,
            remote_rom_index,
            emulator=sf.profile.romm_emulator,
            strip_slot_suffix=sf.profile.strip_slot_suffix,
        )
        if state is None:
            skipped_paths.append(sf.path)
            continue
        local_states.append(state)
        local_state_index[_save_lookup_key(state.rom_id, state.file_name)] = state

    # Negotiate with RomM, falling back to local comparison on 404/405
    session_id: int | None
    raw_operations: list[SyncOperationSchema]
    try:
        negotiate_response = client.negotiate_sync(
            SyncNegotiatePayload(device_id=resolved_device_id, saves=local_states)
        )
        session_id = negotiate_response.session_id
        raw_operations = negotiate_response.operations
        _log.info(
            "negotiate complete: session=%d upload=%d download=%d conflict=%d no_op=%d",
            session_id,
            negotiate_response.total_upload,
            negotiate_response.total_download,
            negotiate_response.total_conflict,
            negotiate_response.total_no_op,
        )
    except ApiError as exc:
        if exc.http_status in {404, 405}:
            raw_operations, session_id = _legacy_negotiate(
                local_states,
                remote_save_index,
                config.sync.direction,
                device_id=resolved_device_id,
            )
        else:
            raise

    filtered_operations = [
        operation
        for operation in raw_operations
        if operation.save_id not in untracked_save_ids
    ]

    return SaveSyncPreview(
        device_id=resolved_device_id,
        scanned_files=len(scanned_files),
        mapped_files=len(local_states),
        skipped_files=len(skipped_paths),
        local_saves=local_states,
        operations=filtered_operations,
        skipped_paths=skipped_paths,
        session_id=session_id,
        profile_destinations=profile_destinations,
    )


def execute_save_sync_preview(
    config: AppConfig,
    client: RommApiClient,
    preview: SaveSyncPreview,
    file_filters: list[str] | None = None,
    is_interactive: bool = False,
    conflict_overrides: dict[str, str] | None = None,
) -> SaveSyncExecutionResult:
    """Execute selected sync operations from a previously negotiated preview.

    conflict_overrides maps file_name → "upload"|"download"|"skip" for conflicts
    resolved interactively by the CLI layer. When absent, conflict_strategy from
    config is applied automatically (headless-safe).
    """
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

    _log.info(
        "save-sync execute: device=%s session=%s ops=%d apply=True",
        preview.device_id,
        preview.session_id,
        len(selected_ops),
    )

    for raw_op in selected_ops:
        # Resolve conflicts to a concrete upload/download/skip action
        if raw_op.action == "conflict":
            if conflict_overrides and raw_op.file_name in conflict_overrides:
                resolved_action = conflict_overrides[raw_op.file_name]
            else:
                resolved_action = _resolve_conflict_action(
                    config.sync.conflict_strategy,
                    is_interactive,
                    raw_op.file_name,
                    raw_op.rom_id,
                )
            if resolved_action == "skip":
                skipped += 1
                details.append(("conflict", raw_op.file_name, "skipped (conflict unresolved)"))
                continue
            operation = SyncOperationSchema(
                action=resolved_action,
                rom_id=raw_op.rom_id,
                save_id=raw_op.save_id,
                file_name=raw_op.file_name,
                slot=raw_op.slot,
                emulator=raw_op.emulator,
                reason=f"conflict resolved as {resolved_action}",
                server_updated_at=raw_op.server_updated_at,
                server_content_hash=raw_op.server_content_hash,
            )
        else:
            operation = raw_op

        if operation.action not in {"upload", "download"}:
            skipped += 1
            details.append(
                (operation.action, operation.file_name, "skipped (non-transfer operation)")
            )
            continue

        try:
            if operation.action == "upload":
                local_file = _lookup_local_file_for_operation(operation, local_index)
                if local_file is None:
                    skipped += 1
                    details.append(
                        ("upload", operation.file_name, "skipped (local file not found)")
                    )
                    continue
                _do_autocleanup = config.sync.autocleanup
                _autocleanup_limit = config.sync.autocleanup_limit
                try:
                    upload_result = client.upload_save_file(
                        rom_id=operation.rom_id,
                        save_path=local_file.path,
                        device_id=preview.device_id,
                        session_id=preview.session_id,
                        save_id=operation.save_id,
                        overwrite=True,
                        autocleanup=_do_autocleanup,
                        autocleanup_limit=_autocleanup_limit,
                    )
                except ApiError as exc:
                    # Legacy workaround: some RomM builds return 500 on POST when a save
                    # already exists. Re-list saves to find the existing id and retry with PUT.
                    # Only active when romm.legacy_upload_fallback = true in config.
                    if operation.save_id is None and config.romm.legacy_upload_fallback:
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
                            upload_result = client.upload_save_file(
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
                # Establish device-save sync link so negotiate sees this device as current.
                # POST /api/saves does not create DeviceSyncSchema automatically.
                uploaded_save_id = upload_result.get("id") if isinstance(upload_result, dict) else None
                if uploaded_save_id is not None:
                    client.track_save_for_device(int(uploaded_save_id), preview.device_id)
                completed += 1
                details.append(("upload", operation.file_name, "ok"))
                continue

            # download
            if operation.save_id is None:
                failed += 1
                details.append(("download", operation.file_name, "failed (missing save_id)"))
                continue

            # Resolve destination directory: prefer profile-aware path, fall back to save_root
            dest_dir = preview.profile_destinations.get(operation.file_name, save_root)
            destination = dest_dir / operation.file_name

            # Skip download if local already matches server
            if _is_redundant_download(operation, destination):
                skipped += 1
                details.append(("download", operation.file_name, "skipped (already in sync)"))
                continue

            use_optimistic = config.sync.optimistic_downloads
            content = client.download_save_file_content(
                save_id=operation.save_id,
                device_id=preview.device_id,
                session_id=preview.session_id,
                optimistic=use_optimistic,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _backup_local_file(destination)
            part = destination.with_name(destination.name + ".part")
            try:
                part.write_bytes(content)
                os.replace(part, destination)
            except Exception:
                part.unlink(missing_ok=True)
                raise
            if not use_optimistic:
                client.confirm_save_download(
                    save_id=operation.save_id, device_id=preview.device_id
                )
            completed += 1
            details.append(("download", operation.file_name, f"ok -> {destination}"))

        except Exception as exc:  # noqa: BLE001
            failed += 1
            details.append((operation.action, operation.file_name, f"failed ({exc})"))
            _log.error("save-sync op failed: %s %s: %s", operation.action, operation.file_name, exc)

    if preview.session_id is not None:
        pending_play_sessions = consume_pending_sessions()
        if pending_play_sessions:
            _log.info(
                "including %d pending play session(s) in complete", len(pending_play_sessions)
            )
        outcome = client.complete_sync_session(
            preview.session_id,
            SyncCompletePayload(
                operations_completed=completed,
                operations_failed=failed,
                play_sessions=pending_play_sessions if pending_play_sessions else None,
            ),
        )
        if outcome == CompleteOutcome.RETRY_LATER:
            _log.warning(
                "sync session %d not confirmed; may auto-expire on server", preview.session_id
            )

    _log.info(
        "save-sync execute done: completed=%d failed=%d skipped=%d",
        completed,
        failed,
        skipped,
    )
    return SaveSyncExecutionResult(
        executed=completed,
        failed=failed,
        skipped=skipped,
        details=details,
    )
