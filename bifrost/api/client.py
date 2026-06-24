"""HTTP client for RomM API (F0 foundation)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bifrost.api.models import (
    CompleteOutcome,
    DeviceCreatePayload,
    DeviceCreateResponse,
    HeartbeatResponse,
    LibrarySetupResponse,
    PlatformSummary,
    RomSummary,
    SaveSummary,
    StateSummary,
    StatsReturn,
    SyncCompletePayload,
    SyncNegotiatePayload,
    SyncNegotiateResponse,
)
from bifrost.cache import BifrostCache, merge_by_id
from bifrost.config import AppConfig
from bifrost.errors import ApiError, AuthenticationError, NetworkError

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_COLLECTION_PAGE_SIZE = 1000
_log = logging.getLogger("bifrost.api.client")


def _find_rmm_token(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.startswith("rmm_"):
        return payload

    if isinstance(payload, dict):
        for key in (
            "token",
            "client_token",
            "clientToken",
            "access_token",
            "accessToken",
        ):
            value = payload.get(key)
            token = _find_rmm_token(value)
            if token is not None:
                return token

        for value in payload.values():
            token = _find_rmm_token(value)
            if token is not None:
                return token

    if isinstance(payload, list):
        for item in payload:
            token = _find_rmm_token(item)
            if token is not None:
                return token

    return None


def _extract_list_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("data", "items", "results", "roms", "platforms"):
            value = payload.get(key)
            if isinstance(value, list):
                return value

        for value in payload.values():
            extracted = _extract_list_payload(value)
            if extracted is not None:
                return extracted

    return None


def _extract_collection_metadata(payload: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}

    if isinstance(payload, dict):
        for key in (
            "page",
            "current_page",
            "currentPage",
            "pages",
            "total_pages",
            "totalPages",
            "per_page",
            "page_size",
            "size",
            "limit",
            "total",
            "count",
            "next_page",
            "nextPage",
            "has_next",
            "hasNext",
        ):
            if key in payload:
                metadata[key] = payload[key]

    return metadata


def _item_signature(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("id", "fs_path", "fs_name", "name", "slug", "fs_slug"):
            value = item.get(key)
            if value is not None:
                return f"{key}:{value}"
    return repr(item)


def exchange_pairing_code(
    base_url: str,
    code: str,
    timeout_seconds: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Exchange an 8-digit pairing code for a RomM client token."""

    normalized_url = base_url.rstrip("/")

    try:
        with httpx.Client(
            base_url=normalized_url,
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
            transport=transport,
        ) as client:
            response = client.post("/api/client-tokens/exchange", json={"code": code})
    except httpx.RequestError as exc:
        raise NetworkError(f"Unable to reach RomM API at {normalized_url}") from exc

    if response.is_error:
        raise ApiError(
            f"Pairing token exchange failed: {response.status_code} {response.text.strip()}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError("Invalid JSON response from /api/client-tokens/exchange") from exc

    if not isinstance(data, dict):
        raise ApiError("Unexpected response type from /api/client-tokens/exchange")

    token = _find_rmm_token(data)
    if token is not None:
        return token

    raise ApiError("Pairing exchange response did not include a valid client token.")


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 3
    backoff_seconds: float = 0.35


class RommApiClient:
    """Thin synchronous wrapper around RomM REST API."""

    def __init__(
        self,
        config: AppConfig,
        timeout_seconds: float = 10.0,
        retry: RetryConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        no_cache: bool = False,
    ) -> None:
        self._config = config
        self._retry = retry or RetryConfig()
        self._platforms_cache: list[PlatformSummary] | None = None
        self._roms_cache: list[RomSummary] | None = None
        self._roms_raw_cache: list[dict[str, Any]] | None = None
        self._firmware_cache: list[dict[str, Any]] | None = None
        self._collection_info: dict[str, str] = {}
        self._cache: BifrostCache | None = (
            BifrostCache(config.cache) if config.cache.enabled and not no_cache else None
        )
        self._client = httpx.Client(
            base_url=config.romm.url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {config.romm.client_token}",
                "Accept": "application/json",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RommApiClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, self._retry.attempts + 1):
            try:
                response = self._client.request(method, endpoint, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self._retry.attempts:
                    raise NetworkError(
                        f"Unable to reach RomM API at {self._config.romm.url}"
                    ) from exc
                time.sleep(self._retry.backoff_seconds * attempt)
                continue

            if response.status_code == 401:
                raise AuthenticationError(
                    "RomM authentication failed (401). Re-run 'bifrost setup' with a valid token."
                )

            if response.status_code in RETRY_STATUS_CODES and attempt < self._retry.attempts:
                time.sleep(self._retry.backoff_seconds * attempt)
                continue

            if response.is_error:
                raise ApiError(
                    f"RomM API request failed: {response.status_code} {response.text.strip()}",
                    http_status=response.status_code,
                )

            return response

        raise ApiError("Unexpected API failure") from last_error

    def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        response = self._request(method, endpoint, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"Invalid JSON response from RomM API endpoint: {endpoint}") from exc

    def _request_bytes(self, method: str, endpoint: str, **kwargs: Any) -> bytes:
        response = self._request(method, endpoint, **kwargs)
        return response.content

    def _fetch_platforms_delta(self, since: datetime) -> list[Any]:
        """Fetch platforms updated after `since` (no pagination needed for platforms)."""
        data = self._request_json(
            "GET", "/api/platforms", params={"updated_after": since.isoformat()}
        )
        items = _extract_list_payload(data)
        return items if items is not None else []

    def _fetch_roms_delta(self, since: datetime) -> list[dict[str, Any]]:
        """Fetch ROMs updated after `since` using offset pagination."""
        since_str = since.isoformat()
        limit = 1000
        offset = 0
        raw_items: list[Any] = []

        while True:
            payload = self._request_json(
                "GET",
                "/api/roms",
                params={
                    "updated_after": since_str,
                    "limit": limit,
                    "offset": offset,
                    "with_char_index": False,
                    "with_filter_values": False,
                    "with_files": False,
                },
            )
            items_payload: list[Any] | None = None
            total: int | None = None

            if isinstance(payload, dict):
                candidate = payload.get("items")
                total_candidate = payload.get("total")
                if isinstance(candidate, list) and isinstance(total_candidate, int):
                    items_payload = candidate
                    total = total_candidate
                else:
                    items_payload = _extract_list_payload(payload) or []
                    total = len(raw_items) + len(items_payload)
            elif isinstance(payload, list):
                items_payload = payload
                total = len(raw_items) + len(payload)

            if not items_payload:
                break

            raw_items.extend(items_payload)

            if total is not None and len(raw_items) >= total:
                break
            offset += limit

        return [item for item in raw_items if isinstance(item, dict)]

    def upload_save_file(
        self,
        rom_id: int,
        save_path: Path,
        device_id: str | None = None,
        session_id: int | None = None,
        save_id: int | None = None,
        overwrite: bool = True,
        autocleanup: bool = False,
        autocleanup_limit: int = 3,
    ) -> dict[str, Any]:
        with save_path.open("rb") as handle:
            files = {"saveFile": (save_path.name, handle, "application/octet-stream")}
            if save_id is not None:
                data = self._request_json(
                    "PUT",
                    f"/api/saves/{save_id}",
                    params={"device_id": device_id} if device_id else None,
                    files=files,
                )
            else:
                params: dict[str, Any] = {"rom_id": rom_id, "overwrite": overwrite}
                if device_id:
                    params["device_id"] = device_id
                if session_id is not None:
                    params["session_id"] = session_id
                if autocleanup:
                    params["autocleanup"] = True
                    params["autocleanup_limit"] = autocleanup_limit
                data = self._request_json("POST", "/api/saves", params=params, files=files)

        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for save upload")
        return data

    def download_save_file_content(
        self,
        save_id: int,
        device_id: str | None = None,
        session_id: int | None = None,
        optimistic: bool = False,
    ) -> bytes:
        params: dict[str, Any] = {}
        if device_id:
            params["device_id"] = device_id
        if session_id is not None:
            params["session_id"] = session_id
        if optimistic:
            params["optimistic"] = True
        return self._request_bytes("GET", f"/api/saves/{save_id}/content", params=params)

    def confirm_save_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        data = self._request_json(
            "POST",
            f"/api/saves/{save_id}/downloaded",
            json={"device_id": device_id},
        )
        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for /api/saves/{id}/downloaded")
        return data

    def track_save(self, save_id: int, device_id: str) -> dict[str, Any]:
        data = self._request_json(
            "POST",
            f"/api/saves/{save_id}/track",
            json={"device_id": device_id},
        )
        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for /api/saves/{id}/track")
        return data

    def list_states(self, rom_id: int | None = None) -> list[StateSummary]:
        params: dict[str, Any] = {}
        if rom_id is not None:
            params["rom_id"] = rom_id
        data = self._request_json("GET", "/api/states", params=params)
        if not isinstance(data, list):
            raise ApiError("Unexpected response type for /api/states")
        return [StateSummary.model_validate(item) for item in data if isinstance(item, dict)]

    def upload_state_file(
        self,
        rom_id: int,
        state_path: Path,
        emulator: str | None = None,
        state_id: int | None = None,
    ) -> dict[str, Any]:
        with state_path.open("rb") as handle:
            files = {"stateFile": (state_path.name, handle, "application/octet-stream")}
            if state_id is not None:
                data = self._request_json("PUT", f"/api/states/{state_id}", files=files)
            else:
                params: dict[str, Any] = {"rom_id": rom_id}
                if emulator:
                    params["emulator"] = emulator
                data = self._request_json("POST", "/api/states", params=params, files=files)

        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for state upload")
        return data

    def _load_collection(self, endpoint: str) -> tuple[list[Any], str]:
        first_payload = self._request_json(
            "GET",
            endpoint,
            params={"page": 1, "size": DEFAULT_COLLECTION_PAGE_SIZE},
        )
        items_payload = _extract_list_payload(first_payload)
        if items_payload is None:
            raise ApiError(f"Unexpected response type for {endpoint}")

        metadata = _extract_collection_metadata(first_payload)
        items = list(items_payload)
        source = "flat"

        should_attempt_pagination = isinstance(first_payload, dict) or bool(metadata)
        if not should_attempt_pagination:
            return items, source

        seen_signatures = {_item_signature(item) for item in items}
        page_size = int(
            metadata.get("size")
            or metadata.get("per_page")
            or metadata.get("page_size")
            or metadata.get("limit")
            or len(items)
            or DEFAULT_COLLECTION_PAGE_SIZE
        )
        total = metadata.get("total") or metadata.get("count")
        total_pages = metadata.get("pages") or metadata.get("total_pages")
        max_pages = int(total_pages) if isinstance(total_pages, int) and total_pages > 0 else None

        pages_fetched = 1
        page = 2
        while True:
            if max_pages is not None and page > max_pages:
                break
            if isinstance(total, int) and total >= 0 and len(items) >= total:
                break

            next_payload = self._request_json(
                "GET",
                endpoint,
                params={"page": page, "size": page_size},
            )
            next_items_payload = _extract_list_payload(next_payload)
            if next_items_payload is None or not next_items_payload:
                break

            next_items = list(next_items_payload)
            next_signatures = {_item_signature(item) for item in next_items}
            if next_signatures.issubset(seen_signatures):
                break

            new_items = [
                item for item in next_items if _item_signature(item) not in seen_signatures
            ]
            if not new_items:
                break

            items.extend(new_items)
            seen_signatures.update(_item_signature(item) for item in new_items)
            pages_fetched += 1
            page += 1

        if pages_fetched > 1:
            source = f"paginated ({pages_fetched} pages)"
        elif bool(metadata):
            source = "paginated (single page)"

        return items, source

    def heartbeat(self) -> HeartbeatResponse:
        data = self._request_json("GET", "/api/heartbeat")
        if isinstance(data, dict):
            return HeartbeatResponse.model_validate(data)
        # Some RomM builds return simple strings for heartbeat.
        return HeartbeatResponse(status=str(data), message=str(data))

    def get_library_setup(self) -> LibrarySetupResponse:
        data = self._request_json("GET", "/api/setup/library")
        return LibrarySetupResponse.model_validate(data)

    def stats(self, include_platform_stats: bool = False) -> StatsReturn:
        data = self._request_json(
            "GET",
            "/api/stats",
            params={"include_platform_stats": include_platform_stats},
        )
        return StatsReturn.model_validate(data)

    def roms_count(self, **filters: Any) -> int:
        params: dict[str, Any] = {
            "limit": 1,
            "offset": 0,
            "with_char_index": False,
            "with_filter_values": False,
            "with_files": False,
            **filters,
        }
        data = self._request_json("GET", "/api/roms", params=params)
        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for /api/roms")
        total = data.get("total")
        if isinstance(total, int):
            return total

        nested = data.get("data") or data.get("result") or data.get("page")
        if isinstance(nested, dict):
            nested_total = nested.get("total")
            if isinstance(nested_total, int):
                return nested_total

        raise ApiError("Unexpected response type for /api/roms")

    def list_platforms(self, use_cache: bool = True) -> list[PlatformSummary]:
        # L1: in-memory
        if use_cache and self._platforms_cache is not None:
            return self._platforms_cache

        # L2: disk cache
        if use_cache and self._cache is not None:
            cached = self._cache.get("platforms")
            if cached is not None:
                items = [PlatformSummary.model_validate(i) for i in cached]
                self._platforms_cache = items
                self._collection_info["platforms"] = "disk_cache"
                return items

            last_fetched = self._cache.last_fetched_at("platforms")
            if last_fetched is not None:
                stale = self._cache.get_stale("platforms")
                if stale is not None:
                    try:
                        delta = self._fetch_platforms_delta(last_fetched)
                        merged_dicts = merge_by_id(stale, delta)
                        try:
                            self._cache.set("platforms", merged_dicts, full_fetch=False)
                        except OSError:
                            pass
                        items = [PlatformSummary.model_validate(i) for i in merged_dicts]
                        self._platforms_cache = items
                        self._collection_info["platforms"] = "disk_cache_incremental"
                        return items
                    except (ApiError, NetworkError):
                        pass

        # L3: full HTTP fetch
        data = self._request_json("GET", "/api/platforms")
        items_payload = _extract_list_payload(data)
        if items_payload is None:
            raise ApiError("Unexpected response type for /api/platforms")
        items = [PlatformSummary.model_validate(item) for item in items_payload]

        if use_cache and self._cache is not None:
            try:
                self._cache.set(
                    "platforms",
                    [i.model_dump(mode="python") for i in items],
                    full_fetch=True,
                )
            except OSError:
                pass

        self._platforms_cache = items
        self._collection_info["platforms"] = "flat"
        return items

    def list_roms(self, use_cache: bool = True) -> list[RomSummary]:
        if use_cache and self._roms_cache is not None:
            return self._roms_cache

        raw_items = self.list_roms_raw(use_cache=use_cache)
        items = [RomSummary.model_validate(item) for item in raw_items]
        self._roms_cache = items
        return items

    def list_roms_raw(self, use_cache: bool = True) -> list[dict[str, Any]]:
        # L1: in-memory
        if use_cache and self._roms_raw_cache is not None:
            return self._roms_raw_cache

        # L2: disk cache
        if use_cache and self._cache is not None:
            cached = self._cache.get("roms")
            if cached is not None:
                self._roms_raw_cache = cached
                self._roms_cache = [RomSummary.model_validate(i) for i in cached]
                self._collection_info["roms"] = "disk_cache"
                return cached

            last_fetched = self._cache.last_fetched_at("roms")
            if last_fetched is not None:
                stale = self._cache.get_stale("roms")
                if stale is not None:
                    try:
                        delta = self._fetch_roms_delta(last_fetched)
                        merged = merge_by_id(stale, delta)
                        try:
                            self._cache.set("roms", merged, full_fetch=False)
                        except OSError:
                            pass
                        self._roms_raw_cache = merged
                        self._roms_cache = [RomSummary.model_validate(i) for i in merged]
                        self._collection_info["roms"] = "disk_cache_incremental"
                        return merged
                    except (ApiError, NetworkError):
                        pass

        # L3: full HTTP fetch
        first_payload = self._request_json(
            "GET",
            "/api/roms",
            params={
                "limit": 1000,
                "offset": 0,
                "with_char_index": False,
                "with_filter_values": False,
                "with_files": False,
            },
        )

        # Backward compatibility: some builds return list payloads in "data" without total.
        if isinstance(first_payload, dict) and not isinstance(first_payload.get("total"), int):
            fallback_items = _extract_list_payload(first_payload)
            if fallback_items is not None:
                items = [item for item in fallback_items if isinstance(item, dict)]
                if use_cache and self._cache is not None:
                    try:
                        self._cache.set("roms", items, full_fetch=True)
                    except OSError:
                        pass
                self._roms_raw_cache = items
                self._roms_cache = [RomSummary.model_validate(item) for item in items]
                self._collection_info["roms"] = "flat"
                return items

        limit = 1000
        offset = 0
        pages = 0
        raw_items: list[Any] = []
        total: int | None = None

        while True:
            payload = first_payload if pages == 0 else self._request_json(
                "GET",
                "/api/roms",
                params={
                    "limit": limit,
                    "offset": offset,
                    "with_char_index": False,
                    "with_filter_values": False,
                    "with_files": False,
                },
            )

            if not isinstance(payload, dict):
                raise ApiError("Unexpected response type for /api/roms")

            items_payload = payload.get("items")
            total_payload = payload.get("total")
            if not isinstance(items_payload, list) or not isinstance(total_payload, int):
                raise ApiError("Unexpected response type for /api/roms")

            raw_items.extend(items_payload)
            total = total_payload
            pages += 1

            if len(raw_items) >= total:
                break
            if not items_payload:
                break
            offset += limit

        items = [item for item in raw_items if isinstance(item, dict)]
        if use_cache and self._cache is not None:
            try:
                self._cache.set("roms", items, full_fetch=True)
            except OSError:
                pass
        self._roms_raw_cache = items
        self._roms_cache = [RomSummary.model_validate(item) for item in items]
        self._collection_info["roms"] = f"paginated ({pages} pages)"
        return items

    def list_firmware(
        self, platform_id: int | None = None, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        # Cache only applies to the full list (no platform_id filter).
        can_cache = platform_id is None

        # L1: in-memory
        if use_cache and can_cache and self._firmware_cache is not None:
            return self._firmware_cache

        # L2: disk cache (TTL only — /api/firmware does not support updated_after)
        if use_cache and can_cache and self._cache is not None:
            cached = self._cache.get("firmware")
            if cached is not None:
                self._firmware_cache = cached
                self._collection_info["firmware"] = "disk_cache"
                return cached

        # L3: full HTTP fetch
        params: dict[str, Any] = {}
        if platform_id is not None:
            params["platform_id"] = platform_id
        data = self._request_json("GET", "/api/firmware", params=params)
        if not isinstance(data, list):
            raise ApiError("Unexpected response type for /api/firmware")
        items = [item for item in data if isinstance(item, dict)]

        if use_cache and can_cache and self._cache is not None:
            try:
                self._cache.set("firmware", items, full_fetch=True)
            except OSError:
                pass
        if can_cache:
            self._firmware_cache = items
        return items

    def get_rom(self, rom_id: int) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/roms/{rom_id}")
        if not isinstance(data, dict):
            raise ApiError(f"Unexpected response type for /api/roms/{rom_id}")
        return data

    def get_device(self, device_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/devices/{device_id}")
        if not isinstance(data, dict):
            raise ApiError(f"Unexpected response type for /api/devices/{device_id}")
        return data

    def register_device(self, payload: DeviceCreatePayload) -> DeviceCreateResponse:
        data = self._request_json(
            "POST",
            "/api/devices",
            json=payload.model_dump(mode="python", exclude_none=True),
        )
        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for /api/devices")
        return DeviceCreateResponse.model_validate(data)

    def list_saves(self, device_id: str | None = None) -> list[SaveSummary]:
        params: dict[str, Any] = {}
        if device_id:
            params["device_id"] = device_id
        data = self._request_json("GET", "/api/saves", params=params)
        if not isinstance(data, list):
            raise ApiError("Unexpected response type for /api/saves")
        return [SaveSummary.model_validate(item) for item in data if isinstance(item, dict)]

    def negotiate_sync(self, payload: SyncNegotiatePayload) -> SyncNegotiateResponse:
        data = self._request_json(
            "POST",
            "/api/sync/negotiate",
            json=payload.model_dump(mode="python"),
        )
        if not isinstance(data, dict):
            raise ApiError("Unexpected response type for /api/sync/negotiate")
        return SyncNegotiateResponse.model_validate(data)

    def complete_sync_session(
        self,
        session_id: int,
        payload: SyncCompletePayload,
    ) -> CompleteOutcome:
        try:
            data = self._request_json(
                "POST",
                f"/api/sync/sessions/{session_id}/complete",
                json=payload.model_dump(mode="python"),
            )
        except ApiError as exc:
            status = exc.http_status
            if status in {404, 409, 410}:
                _log.info(
                    "sync session %d already finalized (HTTP %s)", session_id, status
                )
                return CompleteOutcome.ALREADY_FINALIZED
            if status is not None and 400 <= status < 500:
                _log.warning(
                    "sync session %d complete returned %s; treating as finalized",
                    session_id,
                    status,
                )
                return CompleteOutcome.CLIENT_ERROR
            _log.warning(
                "sync session %d complete failed (%s); will not retry", session_id, exc
            )
            return CompleteOutcome.RETRY_LATER
        except Exception as exc:  # noqa: BLE001
            _log.warning("sync session %d complete network error: %s", session_id, exc)
            return CompleteOutcome.RETRY_LATER
        if not isinstance(data, dict):
            _log.warning("sync session %d complete: unexpected response type", session_id)
            return CompleteOutcome.RETRY_LATER
        return CompleteOutcome.ACCEPTED

    def clear_cache(self) -> None:
        self._platforms_cache = None
        self._roms_cache = None
        self._roms_raw_cache = None
        self._firmware_cache = None
        self._collection_info = {}

    def collection_info(self, collection: str) -> str | None:
        return self._collection_info.get(collection)
