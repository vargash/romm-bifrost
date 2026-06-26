"""Persistent disk cache for RomM API responses."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bifrost.config import CacheConfig

CACHE_KEYS: frozenset[str] = frozenset({"firmware", "platforms", "roms"})
_FORMAT_VERSION = 1

_META_LAST_APPLIED = "last_applied"
_META_LAST_FULL_SYNC = "last_full_sync"


@dataclass(frozen=True)
class CacheKeyStatus:
    key: str
    fetched_at: datetime | None
    item_count: int
    age_seconds: float | None
    is_expired: bool
    full_fetch: bool


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bifrost"


def merge_by_id(
    old: list[dict[str, Any]], delta: list[Any]
) -> list[dict[str, Any]]:
    """Merge a delta (updated_after response) into an existing list, keyed by integer `id`.

    New items are appended; existing items are replaced in-place. Deletions are
    not detected — the caller is responsible for TTL-based full refreshes.
    """
    merged: dict[int, dict[str, Any]] = {}
    for item in old:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            merged[item["id"]] = item
    for item in delta:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            merged[item["id"]] = item
    return list(merged.values())


class BifrostCache:
    """File-backed cache for RomM collection responses.

    Only platforms, roms, and firmware are cache-eligible. Saves and states are
    transactional and must never be cached.
    """

    def __init__(self, config: CacheConfig) -> None:
        self._config = config
        raw_dir = config.cache_dir.strip()
        self._dir: Path = Path(raw_dir).expanduser() if raw_dir else default_cache_dir()

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._dir.chmod(0o700)

    def _meta_path(self) -> Path:
        return self._dir / "cache_meta.json"

    def _data_path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _load_meta(self) -> dict[str, Any]:
        path = self._meta_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (ValueError, OSError):
            return {}

    def _save_meta(self, meta: dict[str, Any]) -> None:
        self._ensure_dir()
        path = self._meta_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(path)

    def _ttl_hours(self, key: str) -> int:
        return {
            "platforms": self._config.ttl_platforms_hours,
            "roms": self._config.ttl_roms_hours,
            "firmware": self._config.ttl_firmware_hours,
        }.get(key, 24)

    def _is_expired(self, key: str, meta: dict[str, Any]) -> bool:
        key_meta = meta.get(key)
        if not isinstance(key_meta, dict):
            return True
        fetched_at_str = key_meta.get("fetched_at")
        if not fetched_at_str:
            return True
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
        except ValueError:
            return True
        age_hours = (datetime.now(UTC) - fetched_at).total_seconds() / 3600
        return age_hours >= self._ttl_hours(key)

    def _read_data(self, key: str) -> list[dict[str, Any]] | None:
        path = self._data_path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else None
        except (ValueError, OSError):
            return None

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return cached data if within TTL, else None (miss or expired)."""
        meta = self._load_meta()
        if meta.get("cache_format_version") != _FORMAT_VERSION:
            return None
        if self._is_expired(key, meta):
            return None
        return self._read_data(key)

    def get_stale(self, key: str) -> list[dict[str, Any]] | None:
        """Return cached data regardless of TTL. Returns None only if no data on disk."""
        return self._read_data(key)

    def last_fetched_at(self, key: str) -> datetime | None:
        """Return the timestamp of the last write for `key`, or None if never cached."""
        meta = self._load_meta()
        key_meta = meta.get(key)
        if not isinstance(key_meta, dict):
            return None
        fetched_at_str = key_meta.get("fetched_at")
        if not fetched_at_str:
            return None
        try:
            return datetime.fromisoformat(fetched_at_str)
        except ValueError:
            return None

    def set(self, key: str, data: list[dict[str, Any]], *, full_fetch: bool = True) -> None:
        """Persist data to disk atomically and update cache_meta.json."""
        self._ensure_dir()
        data_path = self._data_path(key)
        tmp = data_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(data_path)

        now = datetime.now(UTC)
        meta = self._load_meta()
        meta["cache_format_version"] = _FORMAT_VERSION
        key_meta: dict[str, Any] = {
            "fetched_at": now.isoformat(),
            "item_count": len(data),
            "full_fetch": full_fetch,
        }
        if key == "roms" and full_fetch:
            key_meta["id_set"] = [
                item["id"] for item in data if isinstance(item.get("id"), int)
            ]
            meta[_META_LAST_FULL_SYNC] = now.isoformat()
        meta[key] = key_meta
        meta[_META_LAST_APPLIED] = now.isoformat()
        self._save_meta(meta)

    # ------------------------------------------------------------------
    # Sync state helpers
    # ------------------------------------------------------------------

    def get_last_applied(self) -> datetime | None:
        """Return timestamp of last successful sync (full or incremental)."""
        meta = self._load_meta()
        ts_str = meta.get(_META_LAST_APPLIED)
        if not isinstance(ts_str, str):
            return None
        try:
            return datetime.fromisoformat(ts_str)
        except ValueError:
            return None

    def set_last_applied(self, ts: datetime | None = None) -> None:
        """Record timestamp of a completed sync run."""
        meta = self._load_meta()
        meta[_META_LAST_APPLIED] = (ts or datetime.now(UTC)).isoformat()
        self._save_meta(meta)

    def get_last_full_sync(self) -> datetime | None:
        """Return timestamp of last full ROM fetch (full_fetch=True)."""
        meta = self._load_meta()
        ts_str = meta.get(_META_LAST_FULL_SYNC)
        if not isinstance(ts_str, str):
            return None
        try:
            return datetime.fromisoformat(ts_str)
        except ValueError:
            return None

    def get_rom_id_set(self) -> set[int] | None:
        """Return the ROM ID set stored during last full sync, or None if unavailable."""
        meta = self._load_meta()
        roms_meta = meta.get("roms")
        if not isinstance(roms_meta, dict):
            return None
        id_list = roms_meta.get("id_set")
        if not isinstance(id_list, list):
            return None
        return {item for item in id_list if isinstance(item, int)}

    def update_rom_id_set(self, server_ids: list[int]) -> None:
        """Overwrite the stored ROM ID set (called after --check-stale)."""
        meta = self._load_meta()
        roms_meta = meta.get("roms")
        if isinstance(roms_meta, dict):
            roms_meta["id_set"] = server_ids
            meta["roms"] = roms_meta
        self._save_meta(meta)

    def invalidate(self, key: str | None = None) -> None:
        """Remove one cache key, or all keys when key is None."""
        meta = self._load_meta()
        keys_to_remove = sorted(CACHE_KEYS) if key is None else [key]
        for k in keys_to_remove:
            data_path = self._data_path(k)
            if data_path.exists():
                try:
                    data_path.unlink()
                except OSError:
                    pass
            meta.pop(k, None)
        self._save_meta(meta)

    def status(self) -> dict[str, CacheKeyStatus]:
        """Return status for each known cache key."""
        meta = self._load_meta()
        now = datetime.now(UTC)
        result: dict[str, CacheKeyStatus] = {}
        for key in sorted(CACHE_KEYS):
            key_meta = meta.get(key) or {}
            fetched_at: datetime | None = None
            age_seconds: float | None = None
            fetched_at_str = key_meta.get("fetched_at") if isinstance(key_meta, dict) else None
            if fetched_at_str:
                try:
                    fetched_at = datetime.fromisoformat(fetched_at_str)
                    age_seconds = (now - fetched_at).total_seconds()
                except ValueError:
                    pass
            result[key] = CacheKeyStatus(
                key=key,
                fetched_at=fetched_at,
                item_count=key_meta.get("item_count", 0) if isinstance(key_meta, dict) else 0,
                age_seconds=age_seconds,
                is_expired=self._is_expired(key, meta),
                full_fetch=key_meta.get("full_fetch", True) if isinstance(key_meta, dict) else True,
            )
        return result
