from __future__ import annotations

import json
from pathlib import Path

from automl_api.operations import CANONICAL_OPERATION_IDS


ROOT = Path(__file__).resolve().parents[1]


def _realm() -> dict[str, object]:
    return json.loads((ROOT / "deploy" / "identity" / "automl-realm.json").read_text("utf-8"))


def test_keycloak_realm_bootstraps_one_machine_client_with_strict_claims() -> None:
    realm = _realm()
    clients = realm["clients"]
    assert isinstance(clients, list) and len(clients) == 1
    client = clients[0]
    assert isinstance(client, dict)
    assert client["clientId"] == "${AUTOML_OIDC_AGENT_CLIENT_ID}"
    assert client["secret"] == "${AUTOML_OIDC_AGENT_CLIENT_SECRET}"
    assert client["serviceAccountsEnabled"] is True
    assert client["standardFlowEnabled"] is False
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["publicClient"] is False
    assert client["fullScopeAllowed"] is False
    assert realm["accessTokenLifespan"] == 300

    mappers = {item["name"]: item for item in client["protocolMappers"]}
    assert mappers["managed-automl-audience"]["config"]["included.client.audience"] == (
        "managed-automl-api"
    )
    assert mappers["managed-automl-tenant"]["config"]["claim.value"] == (
        "${AUTOML_OIDC_AGENT_TENANT_ID}"
    )
    assert mappers["managed-automl-actor-type"]["config"]["claim.value"] == "agent"
    operation_scopes = set(
        mappers["managed-automl-operation-scopes"]["config"]["claim.value"].split()
    )
    assert operation_scopes
    assert operation_scopes <= {
        f"automl:operation:{operation_id}" for operation_id in CANONICAL_OPERATION_IDS
    }
    assert "automl:operation:decideApproval" not in operation_scopes


def test_oidc_compose_uses_internal_jwks_and_does_not_publish_databases() -> None:
    compose = (ROOT / "compose.oidc.yaml").read_text(encoding="utf-8")
    assert "http://automl-identity:8080/realms/" in compose
    assert 'AUTOML_JWT_SECRET: ""' in compose
    assert 'AUTOML_JWT_ALGORITHMS: "RS256"' in compose
    assert "AUTOML_OIDC_AGENT_CLIENT_SECRET:?" in compose
    assert "AUTOML_OIDC_DB_PASSWORD:?" in compose
    assert "automl-identity-db:" in compose
    database_service = compose.split("automl-identity-db:", 1)[1].split("automl-identity:", 1)[0]
    assert "ports:" not in database_service
    assert 'user: "70:70"' in database_service
    assert "cap_drop: [ALL]" in database_service
    assert "internal: true" in compose
    assert 'command: ["start", "--optimized", "--import-realm"]' in compose
    assert "/tmp:rw,noexec,nosuid,size=256m,mode=0700,uid=1000,gid=0" in compose
    identity_dockerfile = (ROOT / "deploy" / "identity" / "Dockerfile").read_text(encoding="utf-8")
    assert "RUN /opt/keycloak/bin/kc.sh build" in identity_dockerfile
    assert "COPY --from=builder /opt/keycloak/ /opt/keycloak/" in identity_dockerfile
    assert "quay.nju.edu.cn/keycloak/keycloak:26.3.3@sha256:" in identity_dockerfile
    assert "pg_dump" in compose


def test_identity_gateway_exposes_only_discovery_keys_and_token_endpoint() -> None:
    caddyfile = (ROOT / "deploy" / "identity" / "Caddyfile").read_text(encoding="utf-8")
    assert "/.well-known/openid-configuration" in caddyfile
    assert "/protocol/openid-connect/certs" in caddyfile
    assert "/protocol/openid-connect/token" in caddyfile
    assert "/admin" not in caddyfile
    assert "handle {\n\t\trespond 404\n\t}" in caddyfile
    assert "request>uri delete" in caddyfile
    assert "max_size 65536" in caddyfile


def test_production_initializer_generates_oidc_secrets_without_fixed_values() -> None:
    initializer = (ROOT / "scripts" / "init_single_node_production.sh").read_text(encoding="utf-8")
    template = (ROOT / ".env.production-single.example").read_text(encoding="utf-8")
    for name in (
        "AUTOML_OIDC_AGENT_CLIENT_SECRET",
        "AUTOML_OIDC_ADMIN_PASSWORD",
        "AUTOML_OIDC_DB_PASSWORD",
    ):
        assert f"{name}=\n" in template
        assert f"s|^{name}=.*|{name}=$" in initializer
    assert '"$data_root/identity-db"' in initializer
