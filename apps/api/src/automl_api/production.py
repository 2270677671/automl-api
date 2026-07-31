from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


_TRUTHY = {"1", "true", "yes", "on", "required", "enabled"}
_SINGLE_NODE_PROFILES = {"production", "prod", "single-node-production"}
_FORMAL_PROFILES = {"formal", "cluster-production"}
_PRODUCTION_PROFILES = _SINGLE_NODE_PROFILES | _FORMAL_PROFILES
_IMAGE_DEPENDENCIES = {
    "psycopg": "PostgreSQL client/RLS readiness",
    "boto3": "S3-compatible object storage and KMS client",
    "jwt": "OIDC/JWKS JWT verification",
    "cryptography": "RS256/ES256 token verification primitives",
    "httpx": "outbound integration and test HTTP client",
}


@dataclass(frozen=True, slots=True)
class ProductionCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        status = "pass" if self.ok else ("fail" if self.required else "warn")
        return {
            "name": self.name,
            "status": status,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    profile: str
    checks: tuple[ProductionCheck, ...]

    @property
    def strict(self) -> bool:
        return self.profile.lower() in _PRODUCTION_PROFILES

    @property
    def single_node(self) -> bool:
        return self.profile.lower() in _SINGLE_NODE_PROFILES

    @property
    def formal(self) -> bool:
        return self.profile.lower() in _FORMAL_PROFILES

    @property
    def ready(self) -> bool:
        return all(check.ok or not check.required for check in self.checks)

    def manifest(self, extra_checks: tuple[ProductionCheck, ...] = ()) -> dict[str, Any]:
        checks = (*self.checks, *extra_checks)
        return {
            "profile": self.profile,
            "strict": self.strict,
            "ready": all(check.ok or not check.required for check in checks),
            "checks": [check.as_dict() for check in checks],
        }

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProductionSettings:
        source = os.environ if environ is None else environ
        profile = source.get("AUTOML_DEPLOYMENT_PROFILE", "local-durable").strip()
        strict = profile.lower() in _PRODUCTION_PROFILES
        single_node = profile.lower() in _SINGLE_NODE_PROFILES
        formal = profile.lower() in _FORMAL_PROFILES
        checks = [
            *_dependency_checks(required=strict),
            _runtime_adapter_check(source, profile=profile, required=strict),
            _oidc_check(source, required=strict, formal=formal),
            _tls_gateway_check(source, required=strict),
            _postgres_check(source, required=formal),
            _object_store_check(source, required=formal),
            _dlp_check(source, required=strict),
            _webhook_check(source, required=strict),
            _deletion_check(source, required=strict),
            _model_registry_check(source, required=strict),
            _worker_isolation_check(source, required=strict, single_node=single_node),
            _request_hardening_check(source, required=strict),
            _backup_check(source, required=strict),
        ]
        return cls(profile=profile or "local-durable", checks=tuple(checks))


def _runtime_adapter_check(
    source: Mapping[str, str], *, profile: str, required: bool
) -> ProductionCheck:
    normalized = profile.lower()
    if normalized in _SINGLE_NODE_PROFILES:
        metadata_store = source.get("AUTOML_METADATA_STORE", "").strip().lower()
        object_store = source.get("AUTOML_OBJECT_STORE", "").strip().lower()
        acknowledged = _enabled(source.get("AUTOML_SINGLE_NODE_ACKNOWLEDGED"))
        ok = metadata_store == "sqlite" and object_store == "local" and acknowledged
        return ProductionCheck(
            "runtime_adapters",
            ok,
            (
                "The operator acknowledged the single-node boundary; SQLite WAL and the local "
                "immutable object store are selected. Runtime health is checked separately."
                if ok
                else "Set AUTOML_METADATA_STORE=sqlite, AUTOML_OBJECT_STORE=local, and "
                "AUTOML_SINGLE_NODE_ACKNOWLEDGED=true for the controlled single-node profile."
            ),
            required=required,
        )
    if normalized in _FORMAL_PROFILES:
        return ProductionCheck(
            "runtime_adapters",
            False,
            (
                "Cluster production remains fail-closed: PostgreSQL/RLS, S3/KMS, dispatcher, "
                "and isolated-worker adapters are not connected to the request path."
            ),
            required=required,
        )
    return ProductionCheck(
        "runtime_adapters",
        False,
        (
            "This distribution still wires SQLite, local object storage, and an in-process worker; "
            "it cannot truthfully report formal production readiness until PostgreSQL/RLS, S3/KMS, "
            "DLP, dispatcher, and isolated-worker adapters are connected."
        ),
        required=required,
    )


def _dependency_checks(*, required: bool) -> list[ProductionCheck]:
    checks: list[ProductionCheck] = []
    for module_name, purpose in _IMAGE_DEPENDENCIES.items():
        installed = importlib.util.find_spec(module_name) is not None
        checks.append(
            ProductionCheck(
                name=f"python_dependency:{module_name}",
                ok=installed,
                detail=(
                    f"{module_name} is installed for {purpose}."
                    if installed
                    else f"{module_name} is missing; {purpose} cannot run."
                ),
                required=required,
            )
        )
    return checks


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _has_value(source: Mapping[str, str], name: str) -> bool:
    return bool(source.get(name, "").strip())


def _oidc_check(source: Mapping[str, str], *, required: bool, formal: bool) -> ProductionCheck:
    production_auth = source.get("AUTOML_AUTH_MODE", "").strip().lower() == "production"
    jwks_configured = _has_value(source, "AUTOML_JWKS_URL") or _has_value(
        source, "AUTOML_JWKS_JSON"
    )
    hmac_configured = _has_value(source, "AUTOML_JWT_SECRET") or _has_value(
        source, "AUTOML_JWT_KEYS"
    )
    configured = production_auth and (
        jwks_configured if formal else jwks_configured or hmac_configured
    )
    return ProductionCheck(
        "oidc_jwks",
        configured,
        (
            "Production JWT verification is configured."
            if configured
            else (
                "Formal production requires AUTOML_AUTH_MODE=production and OIDC/JWKS."
                if formal
                else "Set AUTOML_AUTH_MODE=production and configure JWKS or an independent HS256 key."
            )
        ),
        required=required,
    )


def _tls_gateway_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    public_base_url = source.get("AUTOML_PUBLIC_BASE_URL", "").strip()
    public_base_urls = [public_base_url]
    public_base_urls.extend(
        item.strip()
        for item in source.get("AUTOML_PUBLIC_BASE_URLS", "").split(",")
        if item.strip()
    )
    allowed_hosts = {
        item.strip() for item in source.get("AUTOML_ALLOWED_HOSTS", "").split(",") if item.strip()
    }
    parsed_origins = [urlsplit(value) for value in public_base_urls]
    valid_origins = all(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        for parsed in parsed_origins
    )
    public_hosts_allowed = all(
        bool(parsed.hostname)
        and any(
            host == parsed.hostname
            or (host.startswith("*.") and parsed.hostname.endswith(host.removeprefix("*")))
            for host in allowed_hosts
        )
        for parsed in parsed_origins
    )
    ok = (
        _enabled(source.get("AUTOML_TLS_TERMINATED"))
        and valid_origins
        and bool(allowed_hosts)
        and "*" not in allowed_hosts
        and public_hosts_allowed
    )
    return ProductionCheck(
        "tls_gateway",
        ok,
        (
            "HTTPS termination, the public base URL, and an explicit Host allowlist are configured."
            if ok
            else "Set TLS termination, a valid HTTPS origin, and an allowlist containing its host."
        ),
        required=required,
    )


def _postgres_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    database_url = source.get("AUTOML_DATABASE_URL", "").strip()
    rls_required = _enabled(source.get("AUTOML_POSTGRES_RLS_REQUIRED"))
    ok = database_url.startswith(("postgresql://", "postgres://")) and rls_required
    return ProductionCheck(
        "postgresql_rls",
        ok,
        (
            "PostgreSQL metadata URL and RLS requirement are configured."
            if ok
            else "Set AUTOML_DATABASE_URL and AUTOML_POSTGRES_RLS_REQUIRED=true."
        ),
        required=required,
    )


def _object_store_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    object_store = source.get("AUTOML_OBJECT_STORE", "").strip().lower()
    ok = (
        object_store in {"s3", "s3-compatible"}
        and _has_value(source, "AUTOML_S3_BUCKET")
        and _has_value(source, "AUTOML_KMS_KEY_ID")
    )
    return ProductionCheck(
        "object_store_kms",
        ok,
        (
            "S3-compatible object store and KMS key are configured."
            if ok
            else "Set AUTOML_OBJECT_STORE=s3, AUTOML_S3_BUCKET, and AUTOML_KMS_KEY_ID."
        ),
        required=required,
    )


def _dlp_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    dlp_mode = source.get("AUTOML_DLP_MODE", "").strip().lower()
    allowlist = _has_value(source, "AUTOML_AGENT_CONTEXT_FIELD_ALLOWLIST")
    ok = dlp_mode == "strict" and allowlist
    return ProductionCheck(
        "dlp",
        ok,
        (
            "Strict DLP mode and Agent context field allowlist are configured."
            if ok
            else "Set AUTOML_DLP_MODE=strict and AUTOML_AGENT_CONTEXT_FIELD_ALLOWLIST."
        ),
        required=required,
    )


def _webhook_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    dispatch_mode = source.get("AUTOML_WEBHOOK_DISPATCH_MODE", "").strip().lower()
    ok = dispatch_mode in {"builtin", "http"} and _enabled(
        source.get("AUTOML_WEBHOOK_SIGNING_REQUIRED")
    )
    return ProductionCheck(
        "webhook_outbox",
        ok,
        (
            "The durable webhook outbox, HMAC signing, and in-process HTTP dispatcher are configured."
            if ok
            else "Set AUTOML_WEBHOOK_DISPATCH_MODE=builtin and AUTOML_WEBHOOK_SIGNING_REQUIRED=true."
        ),
        required=required,
    )


def _deletion_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    ok = _enabled(source.get("AUTOML_DELETION_SAGA_ENABLED"))
    return ProductionCheck(
        "deletion_saga",
        ok,
        (
            "Deletion saga is enabled."
            if ok
            else "Set AUTOML_DELETION_SAGA_ENABLED=true for production deletion tracking."
        ),
        required=required,
    )


def _model_registry_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    ok = source.get("AUTOML_MODEL_REGISTRY_MODE", "").strip().lower() == "enabled"
    return ProductionCheck(
        "model_registry",
        ok,
        (
            "Model registry mode is enabled."
            if ok
            else "Set AUTOML_MODEL_REGISTRY_MODE=enabled before production candidates are exposed."
        ),
        required=required,
    )


def _worker_isolation_check(
    source: Mapping[str, str], *, required: bool, single_node: bool
) -> ProductionCheck:
    isolation = source.get("AUTOML_WORKER_ISOLATION", "").strip().lower()
    allowed = {"container-bounded"} if single_node else {"process", "container"}
    ok = isolation in allowed and _enabled(source.get("AUTOML_REQUIRE_WORKER_ISOLATION"))
    return ProductionCheck(
        "worker_isolation",
        ok,
        (
            (
                "Training is serialized and bounded by the single API container's CPU, memory, "
                "PID, and GPU limits."
                if single_node
                else "Worker isolation is explicitly required and configured."
            )
            if ok
            else (
                "Set AUTOML_WORKER_ISOLATION=container-bounded and require isolation for single-node production."
                if single_node
                else "Set AUTOML_WORKER_ISOLATION=container/process and require isolation."
            )
        ),
        required=required,
    )


def _positive_int(source: Mapping[str, str], name: str) -> bool:
    try:
        return int(source.get(name, "0")) > 0
    except ValueError:
        return False


def _request_hardening_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    ok = (
        _enabled(source.get("AUTOML_AUDIT_LOG_ENABLED"))
        and _enabled(source.get("AUTOML_METRICS_ENABLED"))
        and _positive_int(source, "AUTOML_RATE_LIMIT_REQUESTS_PER_MINUTE")
        and _positive_int(source, "AUTOML_RATE_LIMIT_MAX_BUCKETS")
        and _positive_int(source, "AUTOML_MAX_CONCURRENT_REQUESTS")
        and _positive_int(source, "AUTOML_MAX_SSE_CONNECTIONS")
    )
    return ProductionCheck(
        "request_hardening",
        ok,
        (
            "Bounded request/SSE concurrency, client-and-token rate limiting, audit logs, and metrics are enabled."
            if ok
            else "Enable audit logs and metrics and configure positive request-rate, bucket, request-concurrency, and SSE limits."
        ),
        required=required,
    )


def _backup_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    ok = (
        _enabled(source.get("AUTOML_BACKUP_ENABLED"))
        and _has_value(source, "AUTOML_BACKUP_DIR")
        and _positive_int(source, "AUTOML_BACKUP_RETENTION_COUNT")
    )
    return ProductionCheck(
        "backup_restore",
        ok,
        (
            "State backups and a positive retention count are configured."
            if ok
            else "Set AUTOML_BACKUP_ENABLED=true, AUTOML_BACKUP_DIR, and a positive retention count."
        ),
        required=required,
    )


def image_dependency_report() -> dict[str, Any]:
    checks = _dependency_checks(required=True)
    return {
        "ready": all(check.ok for check in checks),
        "checks": [check.as_dict() for check in checks],
    }


def main() -> int:
    report = image_dependency_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
