"""Subset of RomM API models required for Bifrost."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HeartbeatResponse(BaseModel):
    """Response payload for GET /api/heartbeat."""

    status: str | None = None
    message: str | None = None


class LibrarySetupResponse(BaseModel):
    """Response payload for GET /api/setup/library."""

    structure: str | None = None


class PlatformSummary(BaseModel):
    """Subset of platform fields needed by early modules."""

    id: int
    fs_slug: str | None = None
    name: str | None = None


class RomSummary(BaseModel):
    """Subset of ROM fields needed by early modules."""

    id: int
    fs_name: str | None = None
    fs_path: str | None = None
    full_path: str | None = None
    platform_id: int | None = None
    name: str | None = None


class StatsReturn(BaseModel):
    """RomM aggregated stats used by status checks."""

    PLATFORMS: int
    ROMS: int
    SAVES: int | None = None
    STATES: int | None = None
    SCREENSHOTS: int | None = None
    TOTAL_FILESIZE_BYTES: int | None = None
    METADATA_COVERAGE: dict[str, object] | None = None
    REGION_BREAKDOWN: dict[str, object] | None = None


class SaveSummary(BaseModel):
    """Subset of save fields needed for sync preview."""

    id: int
    rom_id: int
    user_id: int | None = None
    file_name: str
    file_name_no_tags: str | None = None
    file_name_no_ext: str | None = None
    file_extension: str | None = None
    file_path: str | None = None
    file_size_bytes: int | None = None
    full_path: str | None = None
    download_path: str | None = None
    missing_from_fs: bool | None = None
    created_at: str | None = None
    updated_at: str
    emulator: str | None = None
    slot: str | None = None
    content_hash: str | None = None


class StateSummary(BaseModel):
    """Subset of state fields needed for state sync preview/apply."""

    id: int
    rom_id: int
    user_id: int | None = None
    file_name: str
    file_name_no_tags: str | None = None
    file_name_no_ext: str | None = None
    file_extension: str | None = None
    file_path: str | None = None
    file_size_bytes: int | None = None
    full_path: str | None = None
    download_path: str | None = None
    missing_from_fs: bool | None = None
    created_at: str | None = None
    updated_at: str
    emulator: str | None = None


class ClientSaveState(BaseModel):
    """Client-reported save state used during sync negotiation."""

    rom_id: int
    file_name: str
    slot: str | None = None
    emulator: str | None = None
    content_hash: str | None = None
    updated_at: str
    file_size_bytes: int


class SyncOperationSchema(BaseModel):
    """Single sync operation returned by RomM."""

    action: str
    rom_id: int
    save_id: int | None = None
    file_name: str
    slot: str | None = None
    emulator: str | None = None
    reason: str
    server_updated_at: str | None = None
    server_content_hash: str | None = None


class SyncNegotiatePayload(BaseModel):
    """Request body for POST /api/sync/negotiate."""

    device_id: str
    saves: list[ClientSaveState] = Field(default_factory=list)


class SyncNegotiateResponse(BaseModel):
    """Response body for POST /api/sync/negotiate."""

    session_id: int
    operations: list[SyncOperationSchema]
    total_upload: int
    total_download: int
    total_conflict: int
    total_no_op: int


class SyncSessionSchema(BaseModel):
    """Sync session summary returned by RomM."""

    id: int
    device_id: str
    user_id: int
    status: str
    initiated_at: str
    completed_at: str | None = None
    operations_planned: int
    operations_completed: int
    operations_failed: int
    error_message: str | None = None
    created_at: str
    updated_at: str


class SyncCompletePayload(BaseModel):
    """Request body for POST /api/sync/sessions/{session_id}/complete."""

    operations_completed: int = 0
    operations_failed: int = 0
    play_sessions: list[dict[str, object]] | None = None


class SyncCompleteResponse(BaseModel):
    """Response body for POST /api/sync/sessions/{session_id}/complete."""

    session: SyncSessionSchema
    play_session_ingest: dict[str, object] | None = None


class DeviceCreatePayload(BaseModel):
    """Request body for POST /api/devices."""

    name: str | None = None
    platform: str | None = None
    client: str | None = None
    client_version: str | None = None
    ip_address: str | None = None
    mac_address: str | None = None
    hostname: str | None = None
    sync_mode: str | None = None
    sync_config: dict[str, object] | None = None
    allow_existing: bool = True
    allow_duplicate: bool = False
    reset_syncs: bool = False


class DeviceCreateResponse(BaseModel):
    """Response body for POST /api/devices."""

    device_id: str
    name: str | None = None
    created_at: str
