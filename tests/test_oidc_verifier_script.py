from __future__ import annotations

import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
import pytest

from scripts.verify_oidc_deployment import (
    REQUIRED_DATA_LIFECYCLE_OPERATION_SCOPES,
    REQUIRED_WEBHOOK_OPERATION_SCOPES,
    Settings,
    VerificationError,
    verify,
)


ISSUER = "https://identity.example.test/realms/automl"
API_URL = "https://api.example.test"
AUDIENCE = "managed-automl-api"


def _identity_transport(
    *,
    include_approval_scope: bool = False,
    omitted_scope: str | None = None,
) -> httpx.MockTransport:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "alg": "RS256", "use": "sig"})
    scopes = [
        "automl:operation:getAgentInterfaceManifest",
        *sorted(REQUIRED_WEBHOOK_OPERATION_SCOPES),
        *sorted(REQUIRED_DATA_LIFECYCLE_OPERATION_SCOPES),
    ]
    if include_approval_scope:
        scopes.append("automl:operation:decideApproval")
    if omitted_scope is not None:
        scopes.remove(omitted_scope)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "service-account-agent-platform",
            "tenant_id": "tenant_partner",
            "actor_type": "agent",
            "scopes": " ".join(scopes),
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key"},
    )
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": ISSUER + "/protocol/openid-connect/token",
                    "jwks_uri": ISSUER + "/protocol/openid-connect/certs",
                },
            )
        if request.url.path.endswith("/protocol/openid-connect/token"):
            token_requests += 1
            if token_requests == 1:
                return httpx.Response(401, json={"error": "unauthorized_client"})
            return httpx.Response(
                200,
                json={"access_token": token, "token_type": "Bearer", "expires_in": 300},
            )
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/v1/agent/manifest":
            if request.headers.get("authorization") != f"Bearer {token}":
                return httpx.Response(401, json={"detail": "unauthenticated"})
            return httpx.Response(200, json={"service_version": "0.8.0"})
        if request.url.path == "/admin/":
            return httpx.Response(404)
        return httpx.Response(500)

    return httpx.MockTransport(handler)


def _settings() -> Settings:
    return Settings(
        api_url=API_URL,
        issuer=ISSUER,
        client_id="agent-platform",
        client_secret="kept-out-of-api-requests",
        audience=AUDIENCE,
        expected_tenant_id="tenant_partner",
        ca_file=None,
        timeout_seconds=5,
    )


def test_oidc_verification_script_checks_credentials_jwks_and_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_type = httpx.Client
    transport = _identity_transport()
    monkeypatch.setattr(
        "scripts.verify_oidc_deployment.httpx.Client",
        lambda **_kwargs: client_type(transport=transport),
    )

    result = verify(_settings())

    assert result == {
        "status": "pass",
        "issuer": ISSUER,
        "token_type": "Bearer",
        "expires_in": 300,
        "tenant_id": "tenant_partner",
        "actor_type": "agent",
        "operation_scope_count": 12,
        "webhook_operation_scope_count": 9,
        "data_lifecycle_operation_scope_count": 2,
        "decide_approval_granted": False,
        "invalid_secret_status": 401,
        "invalid_secret_error": "unauthorized_client",
        "jwks_signing_key_verified": True,
        "api_manifest_status": 200,
        "service_version": "0.8.0",
        "keycloak_admin_status": 404,
    }


def test_oidc_verification_script_rejects_machine_approval_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_type = httpx.Client
    transport = _identity_transport(include_approval_scope=True)
    monkeypatch.setattr(
        "scripts.verify_oidc_deployment.httpx.Client",
        lambda **_kwargs: client_type(transport=transport),
    )

    with pytest.raises(VerificationError, match="must not receive decideApproval"):
        verify(_settings())


def test_oidc_verification_script_rejects_missing_webhook_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_type = httpx.Client
    missing = "automl:operation:createWebhookEndpoint"
    transport = _identity_transport(omitted_scope=missing)
    monkeypatch.setattr(
        "scripts.verify_oidc_deployment.httpx.Client",
        lambda **_kwargs: client_type(transport=transport),
    )

    with pytest.raises(VerificationError, match=missing):
        verify(_settings())


def test_oidc_verification_script_rejects_missing_data_lifecycle_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_type = httpx.Client
    missing = "automl:operation:deleteDataset"
    transport = _identity_transport(omitted_scope=missing)
    monkeypatch.setattr(
        "scripts.verify_oidc_deployment.httpx.Client",
        lambda **_kwargs: client_type(transport=transport),
    )

    with pytest.raises(VerificationError, match=missing):
        verify(_settings())
