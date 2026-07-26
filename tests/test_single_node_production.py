from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI
import httpx
import pytest
from fastapi.testclient import TestClient

from automl_api import __main__ as cli
from automl_api.app import create_app
from automl_api.backup import BackupError, create_backup, restore_backup, verify_backup
from automl_api.credentials import issue_token
from automl_api.hardening import RuntimeHardeningSettings, ServiceMetrics, install_runtime_hardening
from automl_api.persistence import SqliteStore
from automl_api.security import HS256JWTVerifier
from automl_api.storage import LocalBlobStore

from .helpers import mutation_headers


_JWT_SECRET = "single-node-jwt-signing-secret-000000000000000000000000"
_ROOT = Path(__file__).resolve().parents[1]


def _single_node_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "AUTOML_AGENT_CONTEXT_FIELD_ALLOWLIST": "run,objective,open_decision_packets",
        "AUTOML_ALLOWED_HOSTS": "testserver,automl-api,127.0.0.1",
        "AUTOML_AUDIT_LOG_ENABLED": "true",
        "AUTOML_AUTH_MODE": "production",
        "AUTOML_BACKUP_DIR": str(tmp_path / "backups"),
        "AUTOML_BACKUP_ENABLED": "true",
        "AUTOML_BACKUP_RETENTION_COUNT": "3",
        "AUTOML_CURSOR_SECRET": "cursor-secret-000000000000000000000000000000000000",
        "AUTOML_DELETION_SAGA_ENABLED": "true",
        "AUTOML_DEPLOYMENT_PROFILE": "single-node-production",
        "AUTOML_DLP_MODE": "strict",
        "AUTOML_JWT_AUDIENCE": "managed-automl-api",
        "AUTOML_JWT_ISSUER": "https://testserver/identity",
        "AUTOML_JWT_KID": "single-node-1",
        "AUTOML_JWT_SECRET": _JWT_SECRET,
        "AUTOML_MAX_CONCURRENT_REQUESTS": "8",
        "AUTOML_MAX_SSE_CONNECTIONS": "4",
        "AUTOML_METADATA_STORE": "sqlite",
        "AUTOML_METRICS_ENABLED": "true",
        "AUTOML_MODEL_REGISTRY_MODE": "enabled",
        "AUTOML_OBJECT_STORE": "local",
        "AUTOML_PUBLIC_BASE_URL": "https://testserver",
        "AUTOML_RATE_LIMIT_REQUESTS_PER_MINUTE": "100",
        "AUTOML_RATE_LIMIT_MAX_BUCKETS": "256",
        "AUTOML_REQUIRE_WORKER_ISOLATION": "true",
        "AUTOML_SINGLE_NODE_ACKNOWLEDGED": "true",
        "AUTOML_STATE_DIR": str(tmp_path / "state"),
        "AUTOML_TICKET_SECRET": "ticket-secret-000000000000000000000000000000000000",
        "AUTOML_TLS_TERMINATED": "true",
        "AUTOML_WEBHOOK_DISPATCH_MODE": "outbox",
        "AUTOML_WEBHOOK_SIGNING_REQUIRED": "true",
        "AUTOML_WORKER_ISOLATION": "container-bounded",
    }


def _set_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name in list(os.environ):
        if name.startswith("AUTOML_") or name == "TABPFN_TOKEN":
            monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_single_node_production_readiness_includes_live_runtime_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_environment(monkeypatch, _single_node_environment(tmp_path))
    application = create_app()
    with TestClient(application) as client:
        response = client.get("/readyz")
    assert response.status_code == 200, response.text
    production = response.json()["production"]
    assert production["ready"] is True
    checks = {item["name"]: item for item in production["checks"]}
    for name in (
        "sqlite_quick_check",
        "local_blob_store",
        "worker_running",
        "backup_directory",
    ):
        assert checks[name]["status"] == "pass"


def test_cluster_production_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _single_node_environment(tmp_path)
    environment["AUTOML_DEPLOYMENT_PROFILE"] = "cluster-production"
    _set_environment(monkeypatch, environment)
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code == 503, response.text
    checks = {item["name"]: item for item in response.json()["production"]["checks"]}
    assert checks["runtime_adapters"]["status"] == "fail"


def test_hardening_rejects_unknown_hosts_and_emits_headers_metrics_and_redacted_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = _single_node_environment(tmp_path)
    environment["AUTOML_RATE_LIMIT_REQUESTS_PER_MINUTE"] = "2"
    _set_environment(monkeypatch, environment)
    sensitive_token = "do-not-log-this-bearer-token"
    caplog.set_level(logging.INFO, logger="automl_api.audit")
    with TestClient(create_app()) as client:
        rejected = client.get("/healthz", headers={"Host": "attacker.invalid"})
        assert rejected.status_code == 400

        headers = {"Authorization": f"Bearer {sensitive_token}"}
        first = client.get("/openapi.yaml", headers=headers)
        second = client.get("/openapi.yaml", headers=headers)
        limited = client.get("/openapi.yaml", headers=headers)
        metrics = client.get("/metrics")

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]
    for response in (rejected, first, limited):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")
    assert metrics.status_code == 200
    assert "automl_http_requests_total" in metrics.text
    assert "automl_http_rate_limited_total 1" in metrics.text
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert sensitive_token not in rendered_logs
    assert "Authorization" not in rendered_logs


def test_hardening_limits_rotating_invalid_bearer_tokens_by_client() -> None:
    application = FastAPI()
    install_runtime_hardening(
        application,
        RuntimeHardeningSettings(requests_per_minute=2, max_rate_limit_buckets=4),
    )

    @application.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(application) as client:
        first = client.get("/probe", headers={"Authorization": "Bearer invalid-1"})
        second = client.get("/probe", headers={"Authorization": "Bearer invalid-2"})
        limited = client.get("/probe", headers={"Authorization": "Bearer invalid-3"})

    assert first.status_code == second.status_code == 200
    assert limited.status_code == 429


def test_hardening_audits_verified_identity_operation_and_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = _single_node_environment(tmp_path)
    _set_environment(monkeypatch, environment)
    token = str(
        issue_token(
            subject="partner-agent",
            tenant_id="partner_a",
            actor_type="agent",
            operations=["getAgentInterfaceManifest"],
            expires_in=600,
        )["token"]
    )
    caplog.set_level(logging.INFO, logger="automl_api.audit")
    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/agent/manifest",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if '"route":"/v1/agent/manifest"' in record.getMessage()
    ]
    assert records[-1]["tenant_id"] == "partner_a"
    assert records[-1]["subject"] == "partner-agent"
    assert records[-1]["actor_type"] == "agent"
    assert records[-1]["operation_id"] == "getAgentInterfaceManifest"


def test_hardening_rejects_requests_above_the_concurrency_limit() -> None:
    application = FastAPI()
    install_runtime_hardening(
        application,
        RuntimeHardeningSettings(requests_per_minute=100, max_concurrent_requests=1),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    @application.get("/slow")
    async def slow() -> dict[str, bool]:
        started.set()
        await release.wait()
        return {"ok": True}

    @application.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(client.get("/slow"))
            await started.wait()
            probe = await client.get("/healthz")
            rejected = await client.get("/slow")
            release.set()
            first = await first_task
            return first, rejected, probe

    first, rejected, probe = asyncio.run(exercise())
    assert first.status_code == 200
    assert probe.status_code == 200
    assert rejected.status_code == 503
    assert rejected.json()["code"] == "server_busy"
    assert rejected.headers["Retry-After"] == "1"


def test_unhandled_errors_are_audited_and_return_hardened_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = FastAPI()
    install_runtime_hardening(
        application,
        RuntimeHardeningSettings(audit_log_enabled=True, tls_terminated=True),
    )

    @application.get("/broken")
    async def broken() -> None:
        raise RuntimeError("sensitive internal detail")

    caplog.set_level(logging.INFO, logger="automl_api.audit")
    with TestClient(application) as client:
        response = client.get("/broken")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "sensitive internal detail" not in response.text
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")
    assert any('"status":500' in record.getMessage() for record in caplog.records)


def test_sse_connection_slots_are_bounded_and_observable() -> None:
    metrics = ServiceMetrics()

    async def exercise() -> str:
        assert await metrics.enter_stream(3, tenant_id="tenant_a", maximum_per_tenant=2) is True
        assert await metrics.enter_stream(3, tenant_id="tenant_a", maximum_per_tenant=2) is True
        assert await metrics.enter_stream(3, tenant_id="tenant_a", maximum_per_tenant=2) is False
        assert await metrics.enter_stream(3, tenant_id="tenant_b", maximum_per_tenant=2) is True
        assert await metrics.enter_stream(3, tenant_id="tenant_c", maximum_per_tenant=2) is False
        rendered = await metrics.render()
        await metrics.leave_stream(tenant_id="tenant_a")
        await metrics.leave_stream(tenant_id="tenant_a")
        await metrics.leave_stream(tenant_id="tenant_b")
        return rendered

    rendered = asyncio.run(exercise())
    assert "automl_sse_active_connections 3" in rendered
    assert "automl_sse_rejected_connections_total 2" in rendered


def test_sse_per_tenant_limit_cannot_exceed_global_limit() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        RuntimeHardeningSettings.from_env(
            {
                "AUTOML_MAX_SSE_CONNECTIONS": "2",
                "AUTOML_MAX_SSE_CONNECTIONS_PER_TENANT": "3",
            }
        )


def test_cli_disables_raw_access_logs_and_configures_the_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setenv("AUTOML_FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    cli.main(["--host", "127.0.0.1", "--port", "8123"])

    assert captured["access_log"] is False
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "*"


def test_gateway_redacts_download_ticket_uris_and_defines_the_proxy_boundary() -> None:
    caddyfile = (_ROOT / "deploy" / "single-node" / "Caddyfile").read_text(encoding="utf-8")
    compose = (_ROOT / "compose.production-single.yaml").read_text(encoding="utf-8")
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "log_skip @download_ticket" in caddyfile
    assert "request>uri delete" in caddyfile
    assert "@compressible not path /v1/artifact-downloads/*" in caddyfile
    assert "encode @compressible zstd gzip" in caddyfile
    assert "default_sni {$AUTOML_PUBLIC_HOST}" in caddyfile
    assert ":8443 {\n\trespond 421\n}" in caddyfile
    assert 'AUTOML_FORWARDED_ALLOW_IPS: "*"' in compose
    assert "cap_add: [DAC_OVERRIDE, NET_BIND_SERVICE]" in compose
    assert "https://$$AUTOML_PUBLIC_HOST:8443/healthz" in compose
    assert "healthcheck:\n      disable: true" in compose
    assert "header_up X-Forwarded-Proto https" in caddyfile
    assert 'project["dependencies"]' in dockerfile
    assert "--constraint requirements.production.lock" in dockerfile
    assert "--requirement requirements.production.lock" not in dockerfile


def test_public_base_url_controls_upload_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_environment(
        monkeypatch,
        {
            "AUTOML_PUBLIC_BASE_URL": "https://automl.partner.test:8443",
            "AUTOML_STATE_DIR": str(tmp_path / "state"),
        },
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/datasets",
            headers=mutation_headers("public-base-url-test-0001"),
            json={
                "name": "public-url",
                "filename": "data.csv",
                "media_type": "text/csv",
                "size_bytes": 8,
            },
        )
    assert response.status_code == 201, response.text
    assert response.json()["parts"][0]["url"].startswith("https://automl.partner.test:8443/")


def test_backup_verify_restore_and_tamper_detection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "automl.db"
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('from-backup')")
    connection.commit()
    connection.close()
    objects = state / "objects" / "artifacts" / "tenant"
    objects.mkdir(parents=True)
    (objects / "report.json").write_text('{"ok":true}', encoding="utf-8")

    backup = create_backup(state, tmp_path / "backups", retention_count=2)
    manifest = verify_backup(backup)
    assert manifest["schema_version"] == 1
    assert {item["path"] for item in manifest["files"]} == {
        "automl.db",
        "objects/artifacts/tenant/report.json",
    }
    assert {
        path.relative_to(backup).as_posix() for path in backup.rglob("*") if path.is_file()
    } == {
        "automl.db",
        "manifest.json",
        "objects/artifacts/tenant/report.json",
    }

    target = tmp_path / "restored"
    target.mkdir()
    cache = target / "tabpfn-cache"
    cache.mkdir()
    (cache / "weights.bin").write_bytes(b"weights")
    old_database = target / "automl.db"
    sqlite3.connect(old_database).close()
    rollback = restore_backup(backup, target, force=True)
    assert rollback is not None and (rollback / "automl.db").is_file()
    assert (target / "tabpfn-cache" / "weights.bin").read_bytes() == b"weights"
    restored = sqlite3.connect(target / "automl.db")
    try:
        assert restored.execute("SELECT value FROM marker").fetchone()[0] == "from-backup"
    finally:
        restored.close()

    (backup / "objects" / "artifacts" / "tenant" / "report.json").write_bytes(b"tampered")
    with pytest.raises(BackupError):
        verify_backup(backup)


def test_issued_credentials_are_operation_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _single_node_environment(tmp_path)
    _set_environment(monkeypatch, environment)
    issued = issue_token(
        subject="partner-agent",
        tenant_id="partner_a",
        actor_type="agent",
        operations=["getAgentInterfaceManifest", "createRun"],
        expires_in=600,
    )
    verifier = HS256JWTVerifier(
        issuer=environment["AUTOML_JWT_ISSUER"],
        audience=environment["AUTOML_JWT_AUDIENCE"],
        keys={environment["AUTOML_JWT_KID"]: _JWT_SECRET},
    )
    principal = verifier.verify(str(issued["token"]))
    assert principal.tenant_id == "partner_a"
    assert principal.actor_type == "agent"
    assert principal.scopes == frozenset(
        {
            "automl:operation:getAgentInterfaceManifest",
            "automl:operation:createRun",
        }
    )


def test_state_database_permissions_are_private(tmp_path: Path) -> None:
    database = tmp_path / "automl.db"
    state = SqliteStore(database)
    try:
        assert database.stat().st_mode & 0o777 == 0o600
        assert asyncio.run(state.quick_check()) == "ok"
    finally:
        asyncio.run(state.close())

    blob_store = LocalBlobStore(tmp_path / "objects", ticket_secret=b"ticket-secret" * 4)
    artifact = asyncio.run(
        blob_store.put_artifact(
            tenant_id="tenant",
            run_id="run_1",
            artifact_id="artifact_1",
            content=b"artifact",
        )
    )
    assert blob_store.root.stat().st_mode & 0o777 == 0o700
    assert blob_store.path_for_key(artifact.key).stat().st_mode & 0o777 == 0o600
