"""Fail-closed production smoke test for the OIDC client-credentials path."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt


REQUIRED_WEBHOOK_OPERATION_IDS = (
    "createWebhookEndpoint",
    "listWebhookEndpoints",
    "getWebhookEndpoint",
    "deleteWebhookEndpoint",
    "rotateWebhookEndpointSecret",
    "enableWebhookEndpoint",
    "listWebhookDeliveries",
    "getWebhookDelivery",
    "redeliverWebhookDelivery",
)
REQUIRED_WEBHOOK_OPERATION_SCOPES = frozenset(
    f"automl:operation:{operation_id}" for operation_id in REQUIRED_WEBHOOK_OPERATION_IDS
)
REQUIRED_DATA_LIFECYCLE_OPERATION_SCOPES = frozenset(
    {
        "automl:operation:deleteDataset",
        "automl:operation:getDeletionJob",
    }
)


class VerificationError(RuntimeError):
    """The deployed identity or API contract did not pass verification."""


@dataclass(frozen=True)
class Settings:
    api_url: str
    issuer: str
    client_id: str
    client_secret: str
    audience: str
    expected_tenant_id: str | None
    ca_file: str | None
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 120:
            raise VerificationError("timeout must be greater than 0 and at most 120 seconds")

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> Settings:
        return cls(
            api_url=_required(args.api_url or os.getenv("AUTOML_API_URL"), "AUTOML_API_URL"),
            issuer=_required(args.issuer or os.getenv("AUTOML_OIDC_ISSUER"), "AUTOML_OIDC_ISSUER"),
            client_id=_required(
                args.client_id or os.getenv("AUTOML_OIDC_CLIENT_ID"),
                "AUTOML_OIDC_CLIENT_ID",
            ),
            client_secret=_required(
                os.getenv("AUTOML_OIDC_CLIENT_SECRET"), "AUTOML_OIDC_CLIENT_SECRET"
            ),
            audience=_required(
                args.audience or os.getenv("AUTOML_OIDC_AUDIENCE", "managed-automl-api"),
                "AUTOML_OIDC_AUDIENCE",
            ),
            expected_tenant_id=(
                args.expected_tenant or os.getenv("AUTOML_OIDC_EXPECTED_TENANT_ID") or None
            ),
            ca_file=args.ca_file or os.getenv("AUTOML_CA_FILE") or None,
            timeout_seconds=args.timeout,
        )


def _required(value: str | None, name: str) -> str:
    if not value or value != value.strip():
        raise VerificationError(f"{name} must be a non-empty value without surrounding whitespace")
    return value


def _json_object(response: httpx.Response, name: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as error:
        raise VerificationError(f"{name} did not return valid JSON") from error
    if not isinstance(value, dict):
        raise VerificationError(f"{name} must return a JSON object")
    return value


def _expect_status(response: httpx.Response, expected: int, name: str) -> None:
    if response.status_code != expected:
        raise VerificationError(
            f"{name} returned HTTP {response.status_code}; expected HTTP {expected}"
        )


def _same_origin_url(value: object, *, issuer: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"OIDC discovery is missing {name}")
    expected = urlsplit(issuer)
    actual = urlsplit(value)
    if actual.scheme != "https" or actual.netloc != expected.netloc:
        raise VerificationError(f"OIDC discovery {name} is not on the trusted issuer origin")
    return value


def _verify_jwt(
    token: str,
    *,
    jwks: Mapping[str, Any],
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise VerificationError("access token is missing a string kid header")
        key_set = jwt.PyJWKSet.from_dict(dict(jwks))
        signing_key = next((key.key for key in key_set.keys if key.key_id == key_id), None)
        if signing_key is None:
            raise VerificationError("access token kid is absent from JWKS")
        claims = jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except VerificationError:
        raise
    except Exception as error:
        raise VerificationError("access token failed RS256/JWKS verification") from error
    if not isinstance(claims, dict):
        raise VerificationError("verified JWT claims must be a JSON object")
    return claims


def verify(settings: Settings) -> dict[str, Any]:
    issuer = settings.issuer.rstrip("/")
    api_url = settings.api_url.rstrip("/")
    if urlsplit(issuer).scheme != "https" or urlsplit(api_url).scheme != "https":
        raise VerificationError("API and OIDC issuer URLs must use HTTPS")

    verify_tls: str | bool = settings.ca_file or True
    with httpx.Client(verify=verify_tls, timeout=settings.timeout_seconds) as client:
        discovery_response = client.get(issuer + "/.well-known/openid-configuration")
        _expect_status(discovery_response, 200, "OIDC discovery")
        discovery = _json_object(discovery_response, "OIDC discovery")
        if discovery.get("issuer") != issuer:
            raise VerificationError(
                "OIDC discovery issuer does not exactly match the configured issuer"
            )
        token_url = _same_origin_url(
            discovery.get("token_endpoint"), issuer=issuer, name="token_endpoint"
        )
        jwks_url = _same_origin_url(discovery.get("jwks_uri"), issuer=issuer, name="jwks_uri")

        invalid_response = client.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=httpx.BasicAuth(settings.client_id, "automl-verification-intentionally-wrong"),
            headers={"Accept": "application/json"},
        )
        _expect_status(invalid_response, 401, "invalid client-secret request")
        invalid_error = _json_object(invalid_response, "invalid client-secret response").get(
            "error"
        )
        if invalid_error not in {"invalid_client", "unauthorized_client"}:
            raise VerificationError("invalid client secret did not return an OAuth client error")

        token_response = client.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=httpx.BasicAuth(settings.client_id, settings.client_secret),
            headers={"Accept": "application/json"},
        )
        _expect_status(token_response, 200, "client-credentials token request")
        token_payload = _json_object(token_response, "client-credentials token response")
        token = token_payload.get("access_token")
        expires_in = token_payload.get("expires_in")
        token_type = token_payload.get("token_type")
        if not isinstance(token, str) or not token:
            raise VerificationError("token response is missing access_token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise VerificationError("token response token_type is not Bearer")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= 300
        ):
            raise VerificationError("access token lifetime must be between 1 and 300 seconds")

        jwks_response = client.get(jwks_url)
        _expect_status(jwks_response, 200, "OIDC JWKS")
        jwks = _json_object(jwks_response, "OIDC JWKS")
        claims = _verify_jwt(
            token,
            jwks=jwks,
            issuer=issuer,
            audience=settings.audience,
        )
        if claims.get("actor_type") != "agent":
            raise VerificationError("machine client access token actor_type must be agent")
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise VerificationError("machine client access token is missing tenant_id")
        if settings.expected_tenant_id and tenant_id != settings.expected_tenant_id:
            raise VerificationError("machine client access token tenant_id is incorrect")
        scopes = frozenset(str(claims.get("scopes", "")).split())
        if "automl:operation:getAgentInterfaceManifest" not in scopes:
            raise VerificationError("machine client cannot read the Agent interface manifest")
        missing_webhook_scopes = REQUIRED_WEBHOOK_OPERATION_SCOPES - scopes
        if missing_webhook_scopes:
            missing = ", ".join(sorted(missing_webhook_scopes))
            raise VerificationError(
                f"machine client is missing required Webhook operation scopes: {missing}"
            )
        missing_lifecycle_scopes = REQUIRED_DATA_LIFECYCLE_OPERATION_SCOPES - scopes
        if missing_lifecycle_scopes:
            missing = ", ".join(sorted(missing_lifecycle_scopes))
            raise VerificationError(
                f"machine client is missing required data lifecycle operation scopes: {missing}"
            )
        if "automl:operation:decideApproval" in scopes:
            raise VerificationError("machine client must not receive decideApproval")

        unauthenticated = client.get(api_url + "/v1/agent/manifest")
        _expect_status(unauthenticated, 401, "unauthenticated AutoML manifest request")
        authenticated = client.get(
            api_url + "/v1/agent/manifest",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        _expect_status(authenticated, 200, "authenticated AutoML manifest request")
        manifest = _json_object(authenticated, "AutoML Agent manifest")

        issuer_parts = urlsplit(issuer)
        blocked_admin = client.get(f"{issuer_parts.scheme}://{issuer_parts.netloc}/admin/")
        _expect_status(blocked_admin, 404, "public Keycloak admin route")

    return {
        "status": "pass",
        "issuer": issuer,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "tenant_id": tenant_id,
        "actor_type": "agent",
        "operation_scope_count": len(scopes),
        "webhook_operation_scope_count": len(REQUIRED_WEBHOOK_OPERATION_SCOPES),
        "data_lifecycle_operation_scope_count": len(REQUIRED_DATA_LIFECYCLE_OPERATION_SCOPES),
        "decide_approval_granted": False,
        "invalid_secret_status": 401,
        "invalid_secret_error": invalid_error,
        "jwks_signing_key_verified": True,
        "api_manifest_status": 200,
        "service_version": manifest.get("service_version"),
        "keycloak_admin_status": 404,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify OIDC client credentials, JWKS, and the authenticated AutoML API path."
    )
    parser.add_argument("--api-url", help="defaults to AUTOML_API_URL")
    parser.add_argument("--issuer", help="defaults to AUTOML_OIDC_ISSUER")
    parser.add_argument("--client-id", help="defaults to AUTOML_OIDC_CLIENT_ID")
    parser.add_argument("--audience", help="defaults to managed-automl-api")
    parser.add_argument("--expected-tenant", help="expected tenant_id claim")
    parser.add_argument("--ca-file", help="private CA PEM; defaults to AUTOML_CA_FILE")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main() -> int:
    try:
        settings = Settings.from_env(_parser().parse_args())
        print(json.dumps(verify(settings), indent=2, sort_keys=True))
    except (VerificationError, httpx.HTTPError) as error:
        print(f"OIDC verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
