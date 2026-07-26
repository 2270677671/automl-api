from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import os
from time import monotonic, time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware


AUDIT_LOGGER = logging.getLogger("automl_api.audit")
_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeHardeningSettings:
    audit_log_enabled: bool = False
    metrics_enabled: bool = False
    tls_terminated: bool = False
    requests_per_minute: int = 1200
    max_concurrent_requests: int = 64
    max_sse_connections: int = 16
    max_sse_connections_per_tenant: int = 4
    max_rate_limit_buckets: int = 4096
    allowed_hosts: tuple[str, ...] = ("*",)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeHardeningSettings:
        source = os.environ if environ is None else environ
        allowed_hosts = tuple(
            item.strip()
            for item in source.get("AUTOML_ALLOWED_HOSTS", "*").split(",")
            if item.strip()
        )
        if not allowed_hosts:
            raise ValueError("AUTOML_ALLOWED_HOSTS must not be empty")
        max_sse_connections = _positive_int(source, "AUTOML_MAX_SSE_CONNECTIONS", 16)
        max_sse_connections_per_tenant = _positive_int(
            source, "AUTOML_MAX_SSE_CONNECTIONS_PER_TENANT", 4
        )
        if max_sse_connections_per_tenant > max_sse_connections:
            raise ValueError(
                "AUTOML_MAX_SSE_CONNECTIONS_PER_TENANT must not exceed AUTOML_MAX_SSE_CONNECTIONS"
            )
        return cls(
            audit_log_enabled=_enabled(source.get("AUTOML_AUDIT_LOG_ENABLED")),
            metrics_enabled=_enabled(source.get("AUTOML_METRICS_ENABLED")),
            tls_terminated=_enabled(source.get("AUTOML_TLS_TERMINATED")),
            requests_per_minute=_positive_int(
                source, "AUTOML_RATE_LIMIT_REQUESTS_PER_MINUTE", 1200
            ),
            max_concurrent_requests=_positive_int(source, "AUTOML_MAX_CONCURRENT_REQUESTS", 64),
            max_sse_connections=max_sse_connections,
            max_sse_connections_per_tenant=max_sse_connections_per_tenant,
            max_rate_limit_buckets=_positive_int(source, "AUTOML_RATE_LIMIT_MAX_BUCKETS", 4096),
            allowed_hosts=allowed_hosts,
        )


class _RateLimiter:
    _OVERFLOW_KEY = "__overflow__"

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: float = 60.0,
        max_buckets: int = 4096,
    ) -> None:
        if max_buckets < 2:
            raise ValueError("max_buckets must be at least 2")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._buckets: dict[str, deque[float]] = {self._OVERFLOW_KEY: deque()}
        self._lock = asyncio.Lock()
        self._requests = 0

    async def allow(self, key: str) -> tuple[bool, int]:
        now = monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._prune(cutoff)
                if len(self._buckets) < self.max_buckets:
                    bucket = self._buckets.setdefault(key, deque())
                else:
                    bucket = self._buckets[self._OVERFLOW_KEY]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            self._requests += 1
            if self._requests % 512 == 0:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff: float) -> None:
        stale = [
            key
            for key, bucket in self._buckets.items()
            if key != self._OVERFLOW_KEY and (not bucket or bucket[-1] <= cutoff)
        ]
        for key in stale:
            self._buckets.pop(key, None)
        overflow = self._buckets[self._OVERFLOW_KEY]
        while overflow and overflow[0] <= cutoff:
            overflow.popleft()


class ServiceMetrics:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        self._duration_seconds: dict[tuple[str, str], float] = {}
        self._rate_limited = 0
        self._rejected_concurrency = 0
        self._active = 0
        self._active_streams = 0
        self._active_streams_by_tenant: dict[str, int] = {}
        self._rejected_streams = 0

    async def enter(self, maximum: int) -> bool:
        async with self._lock:
            if self._active >= maximum:
                self._rejected_concurrency += 1
                return False
            self._active += 1
            return True

    async def leave(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    async def rate_limited(self) -> None:
        async with self._lock:
            self._rate_limited += 1

    async def enter_stream(
        self,
        maximum: int,
        *,
        tenant_id: str | None = None,
        maximum_per_tenant: int | None = None,
    ) -> bool:
        async with self._lock:
            tenant_active = self._active_streams_by_tenant.get(tenant_id, 0)
            tenant_full = (
                tenant_id is not None
                and maximum_per_tenant is not None
                and tenant_active >= maximum_per_tenant
            )
            if self._active_streams >= maximum or tenant_full:
                self._rejected_streams += 1
                return False
            self._active_streams += 1
            if tenant_id is not None:
                self._active_streams_by_tenant[tenant_id] = tenant_active + 1
            return True

    async def leave_stream(self, *, tenant_id: str | None = None) -> None:
        async with self._lock:
            self._active_streams = max(0, self._active_streams - 1)
            if tenant_id is not None:
                remaining = self._active_streams_by_tenant.get(tenant_id, 0) - 1
                if remaining > 0:
                    self._active_streams_by_tenant[tenant_id] = remaining
                else:
                    self._active_streams_by_tenant.pop(tenant_id, None)

    async def observe(self, method: str, route: str, status: int, duration: float) -> None:
        async with self._lock:
            key = (method, route, status)
            self._requests[key] = self._requests.get(key, 0) + 1
            duration_key = (method, route)
            self._duration_seconds[duration_key] = (
                self._duration_seconds.get(duration_key, 0.0) + duration
            )

    async def render(self) -> str:
        async with self._lock:
            lines = [
                "# HELP automl_http_requests_total Total HTTP requests.",
                "# TYPE automl_http_requests_total counter",
            ]
            for (method, route, status), count in sorted(self._requests.items()):
                labels = f'method="{method}",route="{route}",status="{status}"'
                lines.append(f"automl_http_requests_total{{{labels}}} {count}")
            lines.extend(
                [
                    "# HELP automl_http_request_duration_seconds_total Cumulative request duration.",
                    "# TYPE automl_http_request_duration_seconds_total counter",
                ]
            )
            for (method, route), duration in sorted(self._duration_seconds.items()):
                labels = f'method="{method}",route="{route}"'
                lines.append(
                    f"automl_http_request_duration_seconds_total{{{labels}}} {duration:.6f}"
                )
            lines.extend(
                [
                    "# TYPE automl_http_active_requests gauge",
                    f"automl_http_active_requests {self._active}",
                    "# TYPE automl_http_rate_limited_total counter",
                    f"automl_http_rate_limited_total {self._rate_limited}",
                    "# TYPE automl_http_concurrency_rejected_total counter",
                    f"automl_http_concurrency_rejected_total {self._rejected_concurrency}",
                    "# TYPE automl_sse_active_connections gauge",
                    f"automl_sse_active_connections {self._active_streams}",
                    "# TYPE automl_sse_rejected_connections_total counter",
                    f"automl_sse_rejected_connections_total {self._rejected_streams}",
                ]
            )
            return "\n".join(lines) + "\n"


def _credential_key(request: Request) -> str:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        return "token:" + sha256(authorization.encode("utf-8")).hexdigest()[:24]
    return "token:anonymous"


def _client_key(request: Request) -> str:
    client = request.client.host if request.client is not None else "unknown"
    return f"client:{client}"


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)


def _audit_context(request: Request) -> dict[str, Any]:
    context: dict[str, Any] = {}
    route = request.scope.get("route")
    operation_id = getattr(route, "operation_id", None)
    if isinstance(operation_id, str) and operation_id:
        context["operation_id"] = operation_id
    identity = getattr(request.state, "audit_principal", None)
    if isinstance(identity, Mapping):
        context.update(
            {
                key: value
                for key, value in identity.items()
                if key in {"tenant_id", "subject", "actor_type"} and isinstance(value, str)
            }
        )
    resource_ids = {
        key: str(value)
        for key, value in request.path_params.items()
        if key.endswith("_id") and isinstance(value, (str, int)) and len(str(value)) <= 128
    }
    if resource_ids:
        context["resource_ids"] = resource_ids
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if idempotency_key:
        context["idempotency_key_hash"] = sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    revision = request.headers.get("If-Match", "").strip()
    if revision and len(revision) <= 128:
        context["if_match"] = revision
    return context


def _problem_response(status: int, code: str, title: str, detail: str, **headers: str) -> Response:
    correlation_id = "corr_" + sha256(f"{time()}:{id(headers)}".encode()).hexdigest()[:32]
    return JSONResponse(
        status_code=status,
        content={
            "type": f"/problems/{code}",
            "title": title,
            "status": status,
            "code": code,
            "detail": detail,
            "retriable": True,
            "correlation_id": correlation_id,
        },
        media_type="application/problem+json",
        headers={"X-Correlation-ID": correlation_id, **headers},
    )


def _apply_security_headers(response: Response, *, tls_terminated: bool) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
    )
    if tls_terminated:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )


def install_runtime_hardening(
    application: FastAPI, settings: RuntimeHardeningSettings
) -> ServiceMetrics:
    if settings.allowed_hosts != ("*",):
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(settings.allowed_hosts),
        )

    credential_limiter = _RateLimiter(
        settings.requests_per_minute,
        max_buckets=settings.max_rate_limit_buckets,
    )
    client_limiter = _RateLimiter(
        settings.requests_per_minute,
        max_buckets=settings.max_rate_limit_buckets,
    )
    metrics = ServiceMetrics()
    exempt = {"/healthz", "/readyz", "/metrics"}
    if settings.audit_log_enabled:
        AUDIT_LOGGER.setLevel(logging.INFO)

    async def finalize(
        request: Request,
        response: Response,
        *,
        started: float,
        actor_key: str,
        route: str,
    ) -> Response:
        duration = monotonic() - started
        _apply_security_headers(response, tls_terminated=settings.tls_terminated)
        await metrics.observe(request.method, route, response.status_code, duration)
        if settings.audit_log_enabled:
            record = {
                "event": "http_request",
                "method": request.method,
                "route": route,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 3),
                "actor_key": actor_key,
                "correlation_id": response.headers.get("X-Correlation-ID"),
                **_audit_context(request),
            }
            AUDIT_LOGGER.info(
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return response

    @application.middleware("http")
    async def harden_request(request: Request, call_next: Any) -> Response:
        started = monotonic()
        credential_key = _credential_key(request)
        client_key = _client_key(request)
        if request.url.path not in exempt:
            for limiter, key in (
                (client_limiter, client_key),
                (credential_limiter, credential_key),
            ):
                allowed, retry_after = await limiter.allow(key)
                if not allowed:
                    await metrics.rate_limited()
                    response = _problem_response(
                        429,
                        "rate_limit_exceeded",
                        "Request rate limit exceeded",
                        "Retry after the interval declared by Retry-After.",
                        **{"Retry-After": str(retry_after)},
                    )
                    return await finalize(
                        request,
                        response,
                        started=started,
                        actor_key=credential_key,
                        route="rate_limited",
                    )

        entered = request.url.path not in exempt
        if entered and not await metrics.enter(settings.max_concurrent_requests):
            response = _problem_response(
                503,
                "server_busy",
                "Server request capacity reached",
                "Retry when another request has completed.",
                **{"Retry-After": "1"},
            )
            return await finalize(
                request,
                response,
                started=started,
                actor_key=credential_key,
                route="concurrency_rejected",
            )

        try:
            response = await call_next(request)
        except Exception:
            response = _problem_response(
                500,
                "internal_error",
                "Internal server error",
                "The request could not be completed.",
            )
        finally:
            if entered:
                await metrics.leave()
        route = _route_label(request)
        return await finalize(
            request,
            response,
            started=started,
            actor_key=credential_key,
            route=route,
        )

    return metrics


__all__ = [
    "RuntimeHardeningSettings",
    "ServiceMetrics",
    "install_runtime_hardening",
]
