from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from automl_api.app import create_app
from automl_api.auth import (
    AuthConfigurationError,
    AuthSettings,
    Principal,
    authenticate_bearer_token,
    enforce_operation_scope,
    require_operation_scope,
    require_principal,
    scope_for_operation,
)
from automl_api.errors import APIProblem, install_problem_handlers
from automl_api.security import HS256JWTVerifier, TokenVerificationError
from automl_api.store import InMemoryStore


JWT_SECRET = "jwt-signing-secret-with-at-least-32-bytes"
CURSOR_SECRET = "cursor-signing-secret-with-at-least-32-bytes"
TICKET_SECRET = "ticket-signing-secret-with-at-least-32-bytes"
ISSUER = "https://identity.example.test"
AUDIENCE = "automl-api"
NOW = 1_800_000_000


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt(
    *,
    updates: dict[str, Any] | None = None,
    remove: tuple[str, ...] = (),
    secret: str = JWT_SECRET,
    key_id: str | None = "primary",
) -> str:
    header: dict[str, Any] = {"alg": "HS256", "typ": "JWT"}
    if key_id is not None:
        header["kid"] = key_id
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "agent-platform",
        "tenant_id": "tenant_signed",
        "scope": "automl:operation:getRun datasets:read",
        "roles": ["service_identity"],
        "actor_type": "agent",
        "exp": NOW + 300,
    }
    payload.update(updates or {})
    for claim in remove:
        payload.pop(claim, None)
    header_segment = _base64url(
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    payload_segment = _base64url(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, sha256).digest()
    return f"{header_segment}.{payload_segment}.{_base64url(signature)}"


def _verifier(*, leeway_seconds: int = 0) -> HS256JWTVerifier:
    return HS256JWTVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys={"primary": JWT_SECRET},
        leeway_seconds=leeway_seconds,
    )


def _production_environment() -> dict[str, str]:
    return {
        "AUTOML_AUTH_MODE": "production",
        "AUTOML_JWT_ISSUER": ISSUER,
        "AUTOML_JWT_AUDIENCE": AUDIENCE,
        "AUTOML_JWT_SECRET": JWT_SECRET,
        "AUTOML_JWT_KID": "primary",
        "AUTOML_CURSOR_SECRET": CURSOR_SECRET,
        "AUTOML_TICKET_SECRET": TICKET_SECRET,
        "AUTOML_JWT_LEEWAY_SECONDS": "0",
    }


def _set_auth_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    names = {
        "AUTOML_AUTH_MODE",
        "AUTOML_ENV",
        "AUTOML_JWT_ISSUER",
        "AUTOML_JWT_AUDIENCE",
        "AUTOML_JWT_SECRET",
        "AUTOML_JWT_KEYS",
        "AUTOML_JWT_KID",
        "AUTOML_CURSOR_SECRET",
        "AUTOML_TICKET_SECRET",
        "AUTOML_JWT_LEEWAY_SECONDS",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _principal_app() -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)

    @app.post("/probe")
    async def probe(
        payload: dict[str, Any],
        principal: Annotated[Principal, Depends(require_principal)],
        tenant_id: str | None = None,
        x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
    ) -> dict[str, Any]:
        return {
            "principal_tenant_id": principal.tenant_id,
            "query_tenant_id": tenant_id,
            "header_tenant_id": x_tenant_id,
            "body_tenant_id": payload.get("tenant_id"),
        }

    return app


def test_development_mode_is_explicit_and_derives_a_stable_local_tenant() -> None:
    settings = AuthSettings.from_env({"AUTOML_AUTH_MODE": "development"})
    first = authenticate_bearer_token("local-agent-token", settings=settings)
    repeated = authenticate_bearer_token("local-agent-token", settings=settings)
    other = authenticate_bearer_token("another-agent-token", settings=settings)

    assert settings.mode == "development"
    assert first.authentication_mode == "development"
    assert first.scopes == frozenset({"*"})
    assert first.tenant_id == repeated.tenant_id
    assert first.tenant_id != other.tenant_id


@pytest.mark.parametrize(
    "missing",
    [
        "AUTOML_JWT_ISSUER",
        "AUTOML_JWT_AUDIENCE",
        "AUTOML_JWT_SECRET",
        "AUTOML_CURSOR_SECRET",
    ],
)
def test_production_configuration_fails_closed_when_a_required_value_is_missing(
    missing: str,
) -> None:
    environment = _production_environment()
    environment.pop(missing)

    with pytest.raises(AuthConfigurationError):
        AuthSettings.from_env(environment)


@pytest.mark.parametrize(
    "cursor_secret",
    [
        "too-short",
        "milestone-1-only-change-this-secret-before-deployment",
        "replace-with-an-independent-random-secret",
    ],
)
def test_production_rejects_weak_or_public_cursor_secrets(cursor_secret: str) -> None:
    environment = _production_environment()
    environment["AUTOML_CURSOR_SECRET"] = cursor_secret

    with pytest.raises(AuthConfigurationError):
        AuthSettings.from_env(environment)


@pytest.mark.parametrize(
    "ticket_secret",
    [
        "too-short",
        "replace-with-an-independent-random-secret",
        "change-this-ticket-secret-before-deployment",
    ],
)
def test_create_app_rejects_weak_or_public_ticket_secrets(
    monkeypatch: pytest.MonkeyPatch,
    ticket_secret: str,
) -> None:
    environment = _production_environment()
    environment["AUTOML_TICKET_SECRET"] = ticket_secret
    _set_auth_environment(monkeypatch, environment)

    with pytest.raises(AuthConfigurationError, match="AUTOML_TICKET_SECRET"):
        create_app(InMemoryStore())


def test_create_app_requires_ticket_secret_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _production_environment()
    environment.pop("AUTOML_TICKET_SECRET")
    _set_auth_environment(monkeypatch, environment)

    with pytest.raises(AuthConfigurationError, match="AUTOML_TICKET_SECRET"):
        create_app(InMemoryStore())


def test_hs256_verifier_rejects_weak_signing_keys() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HS256JWTVerifier(issuer=ISSUER, audience=AUDIENCE, keys={"primary": "too-short"})


def test_valid_jwt_maps_only_verified_identity_and_scope_claims() -> None:
    verified = _verifier().verify(
        _jwt(updates={"aud": ["another-service", AUDIENCE], "scopes": ["artifacts:read"]}),
        now=NOW,
    )

    assert verified.subject == "agent-platform"
    assert verified.tenant_id == "tenant_signed"
    assert verified.scopes == frozenset(
        {"automl:operation:getRun", "datasets:read", "artifacts:read"}
    )
    assert verified.roles == frozenset({"service_identity"})
    assert verified.actor_type == "agent"
    assert verified.issuer == ISSUER
    assert verified.key_id == "primary"


def test_jwt_actor_type_defaults_to_service() -> None:
    verified = _verifier().verify(_jwt(remove=("actor_type",)), now=NOW)

    assert verified.actor_type == "service"


@pytest.mark.parametrize(
    ("updates", "remove"),
    [
        ({"iss": "https://attacker.invalid"}, ()),
        ({"aud": "another-api"}, ()),
        ({"exp": NOW}, ()),
        ({}, ("exp",)),
    ],
)
def test_jwt_rejects_wrong_issuer_audience_and_expiration(
    updates: dict[str, Any], remove: tuple[str, ...]
) -> None:
    with pytest.raises(TokenVerificationError):
        _verifier().verify(_jwt(updates=updates, remove=remove), now=NOW)


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant_id": "../tenant_victim"},
        {"tenant_id": "tenant/victim"},
        {"scope": 'automl:operation:getRun bad"scope'},
        {"scopes": ["valid:scope", "bad\\scope"]},
        {"actor_type": "development"},
        {"actor_type": "administrator"},
    ],
)
def test_jwt_rejects_unsafe_tenant_and_scope_claims(updates: dict[str, Any]) -> None:
    with pytest.raises(TokenVerificationError):
        _verifier().verify(_jwt(updates=updates), now=NOW)


def test_tenant_cannot_be_overridden_by_headers_query_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _production_environment()
    _set_auth_environment(monkeypatch, environment)
    current_token = _jwt(updates={"exp": int(time.time()) + 300})

    with TestClient(_principal_app()) as client:
        response = client.post(
            "/probe?tenant_id=tenant_victim",
            headers={
                "Authorization": f"Bearer {current_token}",
                "X-Tenant-ID": "tenant_victim",
            },
            json={"tenant_id": "tenant_victim"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "principal_tenant_id": "tenant_signed",
        "query_tenant_id": "tenant_victim",
        "header_tenant_id": "tenant_victim",
        "body_tenant_id": "tenant_victim",
    }


def test_misconfigured_production_dependency_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _production_environment()
    environment.pop("AUTOML_CURSOR_SECRET")
    _set_auth_environment(monkeypatch, environment)

    with TestClient(_principal_app()) as client:
        response = client.post(
            "/probe",
            headers={"Authorization": f"Bearer {_jwt(updates={'exp': int(time.time()) + 300})}"},
            json={},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "authentication_unavailable"


def test_create_app_fails_closed_when_production_auth_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _production_environment()
    environment.pop("AUTOML_JWT_AUDIENCE")
    _set_auth_environment(monkeypatch, environment)

    with pytest.raises(AuthConfigurationError):
        create_app(InMemoryStore())


def test_runtime_routes_require_exact_operation_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_auth_environment(monkeypatch, _production_environment())
    manifest_token = _jwt(
        updates={
            "scope": scope_for_operation("getAgentInterfaceManifest"),
            "exp": int(time.time()) + 300,
        }
    )
    wrong_scope_token = _jwt(
        updates={"scope": scope_for_operation("getRun"), "exp": int(time.time()) + 300}
    )

    with TestClient(create_app(InMemoryStore())) as client:
        denied = client.get(
            "/v1/agent/manifest",
            headers={"Authorization": f"Bearer {wrong_scope_token}"},
        )
        allowed = client.get(
            "/v1/agent/manifest",
            headers={"Authorization": f"Bearer {manifest_token}"},
        )

    assert denied.status_code == 403
    assert denied.json()["missing_scopes"] == [scope_for_operation("getAgentInterfaceManifest")]
    assert allowed.status_code == 200


def test_agent_actor_cannot_answer_a_human_required_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_auth_environment(monkeypatch, _production_environment())
    scopes = " ".join(
        scope_for_operation(operation_id)
        for operation_id in [
            "createDatasetUpload",
            "finalizeDatasetUpload",
            "createRun",
            "listDecisionPackets",
            "answerDecisionPacket",
        ]
    )
    agent_token = _jwt(
        updates={"scope": scopes, "actor_type": "agent", "exp": int(time.time()) + 300}
    )
    human_token = _jwt(
        updates={"scope": scopes, "actor_type": "human", "exp": int(time.time()) + 300}
    )
    agent_headers = {"Authorization": f"Bearer {agent_token}"}
    human_headers = {"Authorization": f"Bearer {human_token}"}

    with TestClient(create_app(InMemoryStore())) as client:
        created = client.post(
            "/v1/datasets",
            headers={**agent_headers, "Idempotency-Key": "prod-dataset-key-0001"},
            json={
                "name": "prod-human-gate",
                "filename": "data.csv",
                "media_type": "text/csv",
                "size_bytes": 16,
            },
        )
        assert created.status_code == 201, created.text
        upload = created.json()
        finalized = client.post(
            f"/v1/dataset-versions/{upload['dataset_version_id']}:finalize",
            headers={**agent_headers, "Idempotency-Key": "prod-finalize-key-0001"},
            json={
                "upload_id": upload["upload_id"],
                "parts": [{"part_number": 1, "etag": "synthetic-part"}],
                "sha256": "b" * 64,
            },
        )
        assert finalized.status_code == 202, finalized.text
        run_request = {
            "dataset_version_id": upload["dataset_version_id"],
            "objective": {},
            "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
            "policy": {
                "allow_pii": False,
                "allow_external_llm": True,
                "risk_tier": "STANDARD",
            },
            "budget": {
                "max_trials": 1,
                "max_compute_credits": 1,
                "max_wall_time_seconds": 60,
                "max_llm_tokens": 0,
            },
        }
        run = client.post(
            "/v1/runs",
            headers={**agent_headers, "Idempotency-Key": "prod-run-key-0001"},
            json=run_request,
        )
        assert run.status_code == 202, run.text
        run_id = run.json()["run_id"]
        packet = client.get(
            f"/v1/runs/{run_id}/decision-packets",
            headers=agent_headers,
            params={"status": "OPEN"},
        ).json()["items"][0]
        denied = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers={
                **agent_headers,
                "Idempotency-Key": "prod-agent-answer-key-0001",
                "If-Match": f'"{packet["wait_set_revision"]}"',
            },
            json={"answers": [{"question_id": "q_target", "value": "label"}]},
        )
        allowed = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers={
                **human_headers,
                "Idempotency-Key": "prod-human-answer-key-0001",
                "If-Match": f'"{packet["wait_set_revision"]}"',
            },
            json={"answers": [{"question_id": "q_target", "value": "label"}]},
        )

    assert packet["resolution_policy"] == "HUMAN_REQUIRED"
    assert denied.status_code == 403
    assert denied.json()["code"] == "human_decision_required"
    assert allowed.status_code == 202


def test_operation_scope_helper_is_exact_and_fail_closed() -> None:
    principal = Principal(
        subject="agent-platform",
        tenant_id="tenant_signed",
        scopes=frozenset({scope_for_operation("getRun")}),
    )

    assert enforce_operation_scope(principal, "getRun") is principal
    with pytest.raises(APIProblem) as error:
        enforce_operation_scope(principal, "createRun")
    assert error.value.status == 403
    assert error.value.extras["missing_scopes"] == ["automl:operation:createRun"]
    with pytest.raises(ValueError):
        scope_for_operation("getRun invalid")


def test_operation_scope_dependency_returns_403_and_advertises_required_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_auth_environment(monkeypatch, _production_environment())
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/run")
    async def get_run(
        principal: Principal = Depends(require_operation_scope("getRun")),
    ) -> dict[str, str]:
        return {"tenant_id": principal.tenant_id}

    wrong_scope_token = _jwt(
        updates={"scope": "automl:operation:createRun", "exp": int(time.time()) + 300}
    )
    right_scope_token = _jwt(updates={"exp": int(time.time()) + 300})
    with TestClient(app) as client:
        denied = client.get("/run", headers={"Authorization": f"Bearer {wrong_scope_token}"})
        allowed = client.get("/run", headers={"Authorization": f"Bearer {right_scope_token}"})

    assert denied.status_code == 403
    assert denied.json()["required_scopes"] == ["automl:operation:getRun"]
    assert denied.headers["www-authenticate"] == (
        'Bearer error="insufficient_scope", scope="automl:operation:getRun"'
    )
    assert allowed.status_code == 200
    assert allowed.json() == {"tenant_id": "tenant_signed"}
