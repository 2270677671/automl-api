from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from automl_sdk import AutoMLClient, OAuth2ClientCredentialsTokenProvider, OAuthTokenError


def test_client_credentials_provider_caches_and_refreshes_before_expiration() -> None:
    calls: list[httpx.Request] = []
    now = [100.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        token_number = len(calls)
        return httpx.Response(
            200,
            json={
                "access_token": f"access-token-{token_number}",
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    provider = OAuth2ClientCredentialsTokenProvider(
        "https://identity.example.test/realms/automl/protocol/openid-connect/token",
        client_id="agent-platform",
        client_secret="client-secret-value",
        scopes=["automl:operation:getRun", "automl:operation:createRun"],
        transport=httpx.MockTransport(handler),
        refresh_skew_seconds=10,
        clock=lambda: now[0],
    )
    try:
        assert provider() == "access-token-1"
        now[0] = 149
        assert provider() == "access-token-1"
        now[0] = 151
        assert provider() == "access-token-2"
        provider.invalidate()
        assert provider() == "access-token-3"
    finally:
        provider.close()

    assert len(calls) == 3
    expected_basic = base64.b64encode(b"agent-platform:client-secret-value").decode("ascii")
    for request in calls:
        assert request.headers["authorization"] == f"Basic {expected_basic}"
        assert request.headers["accept"] == "application/json"
        assert parse_qs(request.content.decode("ascii")) == {
            "grant_type": ["client_credentials"],
            "scope": ["automl:operation:getRun automl:operation:createRun"],
        }


def test_client_credentials_provider_resolves_rotated_secret_only_when_refreshing() -> None:
    secrets = ["first-secret", "second-secret"]
    calls: list[str] = []
    now = [0.0]

    def secret_provider() -> str:
        return secrets[-1]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        return httpx.Response(
            200,
            json={"access_token": f"token-{len(calls)}", "token_type": "bearer", "expires_in": 60},
        )

    provider = OAuth2ClientCredentialsTokenProvider(
        "https://identity.example.test/token",
        client_id="client",
        client_secret=secret_provider,
        transport=httpx.MockTransport(handler),
        refresh_skew_seconds=0,
        clock=lambda: now[0],
    )
    try:
        assert provider() == "token-1"
        secrets.append("rotated-secret")
        now[0] = 61
        assert provider() == "token-2"
    finally:
        provider.close()

    assert calls[0] != calls[1]


def test_client_credentials_provider_fails_closed_without_leaking_secret() -> None:
    secret = "do-not-leak-this-client-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    provider = OAuth2ClientCredentialsTokenProvider(
        "https://identity.example.test/token",
        client_id="client",
        client_secret=secret,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OAuthTokenError) as raised:
            provider()
    finally:
        provider.close()

    assert raised.value.status_code == 401
    assert raised.value.error == "invalid_client"
    assert secret not in str(raised.value)


def test_automl_client_uses_provider_token_without_forwarding_client_secret() -> None:
    token_requests: list[httpx.Request] = []
    api_requests: list[httpx.Request] = []

    def identity_handler(request: httpx.Request) -> httpx.Response:
        token_requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "short-lived-jwt", "token_type": "Bearer", "expires_in": 300},
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(200, json={"service_version": "0.8.0"})

    provider = OAuth2ClientCredentialsTokenProvider(
        "https://identity.example.test/token",
        client_id="agent-platform",
        client_secret="client-secret",
        transport=httpx.MockTransport(identity_handler),
    )
    api = AutoMLClient(
        "https://automl.example.test",
        token=provider,
        transport=httpx.MockTransport(api_handler),
    )
    try:
        assert api.get_agent_manifest()["service_version"] == "0.8.0"
    finally:
        api.close()
        provider.close()

    assert len(token_requests) == 1
    assert len(api_requests) == 1
    assert api_requests[0].headers["authorization"] == "Bearer short-lived-jwt"
    assert "client-secret" not in str(api_requests[0].headers)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"token_url": "http://identity.example.test/token"}, "must use HTTPS"),
        ({"token_url": "not-a-url"}, "absolute HTTP"),
        ({"client_id": ""}, "client_id"),
        ({"client_secret": ""}, "client_secret"),
        ({"scopes": ["bad scope"]}, "OAuth scope"),
    ],
)
def test_client_credentials_provider_rejects_unsafe_configuration(
    kwargs: dict[str, object], message: str
) -> None:
    options: dict[str, object] = {
        "token_url": "https://identity.example.test/token",
        "client_id": "client",
        "client_secret": "secret",
    }
    options.update(kwargs)
    token_url = str(options.pop("token_url"))
    with pytest.raises(ValueError, match=message):
        OAuth2ClientCredentialsTokenProvider(token_url, **options)  # type: ignore[arg-type]
