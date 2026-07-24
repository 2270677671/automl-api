from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_TRUTHY = {"1", "true", "yes", "on", "required", "enabled"}
_PRODUCTION_PROFILES = {"production", "prod", "formal"}
_IMAGE_DEPENDENCIES = {
    "psycopg": "PostgreSQL client/RLS readiness",
    "boto3": "S3-compatible object storage and KMS client",
    "jwt": "OIDC/JWKS JWT verification",
    "cryptography": "RS256/ES256 token verification primitives",
    "httpx": "Webhook dispatcher HTTP client",
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
    def ready(self) -> bool:
        return all(check.ok or not check.required for check in self.checks)

    def manifest(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "strict": self.strict,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProductionSettings:
        source = os.environ if environ is None else environ
        profile = source.get("AUTOML_DEPLOYMENT_PROFILE", "local-durable").strip()
        strict = profile.lower() in _PRODUCTION_PROFILES
        checks = [
            *_dependency_checks(required=strict),
            _runtime_adapter_check(required=strict),
            _oidc_check(source, required=strict),
            _postgres_check(source, required=strict),
            _object_store_check(source, required=strict),
            _dlp_check(source, required=strict),
            _webhook_check(source, required=strict),
            _deletion_check(source, required=strict),
            _model_registry_check(source, required=strict),
            _worker_isolation_check(source, required=strict),
        ]
        return cls(profile=profile or "local-durable", checks=tuple(checks))


def _runtime_adapter_check(*, required: bool) -> ProductionCheck:
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


def _oidc_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    configured = _has_value(source, "AUTOML_JWKS_URL") or _has_value(source, "AUTOML_JWKS_JSON")
    return ProductionCheck(
        "oidc_jwks",
        configured,
        (
            "OIDC/JWKS is configured through AUTOML_JWKS_URL or AUTOML_JWKS_JSON."
            if configured
            else "Set AUTOML_JWKS_URL or AUTOML_JWKS_JSON for production token verification."
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
    ok = dispatch_mode in {"outbox", "http"} and _enabled(
        source.get("AUTOML_WEBHOOK_SIGNING_REQUIRED")
    )
    return ProductionCheck(
        "webhook_dispatch",
        ok,
        (
            "Webhook outbox/dispatcher and signing requirement are configured."
            if ok
            else "Set AUTOML_WEBHOOK_DISPATCH_MODE=outbox and AUTOML_WEBHOOK_SIGNING_REQUIRED=true."
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


def _worker_isolation_check(source: Mapping[str, str], *, required: bool) -> ProductionCheck:
    isolation = source.get("AUTOML_WORKER_ISOLATION", "").strip().lower()
    ok = isolation in {"process", "container"} and _enabled(
        source.get("AUTOML_REQUIRE_WORKER_ISOLATION")
    )
    return ProductionCheck(
        "worker_isolation",
        ok,
        (
            "Worker isolation is explicitly required and configured."
            if ok
            else "Set AUTOML_WORKER_ISOLATION=container/process and AUTOML_REQUIRE_WORKER_ISOLATION=true."
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
