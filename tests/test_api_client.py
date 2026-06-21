from __future__ import annotations

import httpx
import pytest

from bifrost.api.client import RetryConfig, RommApiClient
from bifrost.config import AppConfig, CacheConfig, RommConfig
from bifrost.errors import ApiError, AuthenticationError


def make_config() -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_token"),
        cache=CacheConfig(enabled=False),
    )


def test_heartbeat_success_with_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer rmm_token"
        if request.url.path == "/api/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={})

    client = RommApiClient(make_config(), transport=httpx.MockTransport(handler))
    hb = client.heartbeat()
    assert hb.status == "ok"
    client.close()


def test_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    client = RommApiClient(make_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(AuthenticationError):
        client.heartbeat()
    client.close()


def test_retry_then_success() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"detail": "try later"})
        return httpx.Response(200, json={"status": "ok"})

    client = RommApiClient(
        make_config(),
        retry=RetryConfig(attempts=2, backoff_seconds=0),
        transport=httpx.MockTransport(handler),
    )
    hb = client.heartbeat()
    assert hb.status == "ok"
    assert calls["count"] == 2
    client.close()


def test_invalid_json_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = RommApiClient(make_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        client.heartbeat()
    client.close()


def test_list_roms_accepts_paged_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/roms":
            return httpx.Response(200, json={"data": [{"id": 1, "fs_name": "game.chd"}]})
        return httpx.Response(404, json={})

    client = RommApiClient(make_config(), transport=httpx.MockTransport(handler))
    roms = client.list_roms()
    assert len(roms) == 1
    assert roms[0].fs_name == "game.chd"
    client.close()
