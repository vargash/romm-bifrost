from __future__ import annotations

import httpx
import pytest

from bifrost.api.client import (
    RetryConfig,
    RommApiClient,
    device_auth_init,
    device_auth_poll_token,
)
from bifrost.api.models import DeviceAuthInitPayload
from bifrost.config import AppConfig, CacheConfig, RommConfig
from bifrost.errors import ApiError, AuthenticationError, DeviceAuthDenied


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


def test_device_auth_init_returns_parsed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/device/init"
        return httpx.Response(
            201,
            json={
                "device_code": "devcode123",
                "user_code": "ABCD-EFGH",
                "verification_path": "/pair/device",
                "verification_path_complete": "/pair/device?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            },
        )

    response = device_auth_init(
        "http://romm.local",
        DeviceAuthInitPayload(
            client_device_identifier="aa:bb:cc:dd:ee:ff",
            name="Bifrost on host",
            client="bifrost",
            requested_scopes=["roms.read"],
        ),
        transport=httpx.MockTransport(handler),
    )

    assert response.device_code == "devcode123"
    assert response.user_code == "ABCD-EFGH"
    assert response.interval == 5


def test_device_auth_poll_token_pending_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "authorization_pending"})

    result = device_auth_poll_token(
        "http://romm.local", "devcode123", transport=httpx.MockTransport(handler)
    )
    assert result is None


def test_device_auth_poll_token_denied_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "access_denied"})

    with pytest.raises(DeviceAuthDenied):
        device_auth_poll_token(
            "http://romm.local", "devcode123", transport=httpx.MockTransport(handler)
        )


def test_device_auth_poll_token_success_returns_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "rmm_scoped_token",
                "device_id": "device-42",
                "scopes": ["roms.read"],
                "expires_at": None,
            },
        )

    result = device_auth_poll_token(
        "http://romm.local", "devcode123", transport=httpx.MockTransport(handler)
    )
    assert result is not None
    assert result.access_token == "rmm_scoped_token"
    assert result.device_id == "device-42"
