from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess

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
    postgres_dockerfile = (ROOT / "deploy" / "identity" / "Postgres.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "docker.1panel.live/library/postgres:17-alpine@sha256:" in postgres_dockerfile
    assert "POSTGRES_BASE_IMAGE:" in compose
    assert 'image: "${AUTOML_POSTGRES_IMAGE:-managed-automl-postgres:17-alpine}"' in compose
    assert 'GOMAXPROCS: "${AUTOML_GATEWAY_GOMAXPROCS:-2}"' in compose
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
    assert "http://127.0.0.1:8081" in caddyfile
    assert "respond /healthz 200" in caddyfile
    assert 'test: [CMD, wget, -qO-, "http://127.0.0.1:8081/healthz"]' in (
        ROOT / "compose.oidc.yaml"
    ).read_text(encoding="utf-8")


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


def test_existing_production_environment_can_be_migrated_without_printing_secrets(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.production-single"
    data_root = tmp_path / "production-data"
    env_file.write_text(
        "AUTOML_PUBLIC_HOST=192.0.2.10\n"
        "AUTOML_HTTPS_BIND_ADDRESS=192.0.2.10\n"
        f"AUTOML_STATE_HOST_DIR={data_root}/state\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = subprocess.run(
        [
            "sh",
            str(ROOT / "scripts" / "enable_oidc_single_node.sh"),
            str(env_file),
            str(data_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    settings = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    secrets = {
        settings["AUTOML_OIDC_AGENT_CLIENT_SECRET"],
        settings["AUTOML_OIDC_ADMIN_PASSWORD"],
        settings["AUTOML_OIDC_DB_PASSWORD"],
    }
    assert len(secrets) == 3
    assert all(len(value) >= 64 for value in secrets)
    assert all(value not in result.stdout for value in secrets)
    assert settings["AUTOML_OIDC_PUBLIC_HOST"] == "192.0.2.10"
    assert settings["AUTOML_OIDC_AGENT_TENANT_ID"] == "partner_a"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((data_root / "identity-db").stat().st_mode) == 0o700

    repeated = subprocess.run(
        [
            "sh",
            str(ROOT / "scripts" / "enable_oidc_single_node.sh"),
            str(env_file),
            str(data_root),
        ],
        capture_output=True,
        text=True,
    )
    assert repeated.returncode != 0
    assert "already contains OIDC settings" in repeated.stderr
