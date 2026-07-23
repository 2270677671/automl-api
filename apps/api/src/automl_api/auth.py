from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal, Protocol, runtime_checkable

from fastapi import Depends, Header

from .errors import APIProblem
from .security import (
    ActorType,
    HS256JWTVerifier,
    TokenVerificationError,
    TokenVerifier,
    normalize_scope,
    validate_shared_secret,
)


AuthMode = Literal["development", "production"]
OPERATION_SCOPE_PREFIX = "automl:operation:"
_OPERATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_INSECURE_CURSOR_SECRETS = (
    "milestone-1-only-change-this-secret-before-deployment",
    "replace-with-an-independent-random-secret",
)


class AuthConfigurationError(ValueError):
    """Raised for a fail-closed authentication configuration error."""


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    actor_type: ActorType | Literal["development"] = "service"
    authentication_mode: AuthMode | Literal["custom"] = "custom"
    issuer: str | None = None
    key_id: str | None = None

    @property
    def sub(self) -> str:
        """Expose the JWT-compatible subject name without breaking the existing API."""

        return self.subject


@dataclass(frozen=True, slots=True)
class AuthSettings:
    mode: AuthMode = "development"
    issuer: str | None = None
    audiences: tuple[str, ...] = ()
    signing_keys: Mapping[str, str | bytes] = field(default_factory=dict, repr=False)
    cursor_secret: str | bytes | None = field(default=None, repr=False)
    leeway_seconds: int = 30

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthSettings:
        source = os.environ if environ is None else environ
        mode = _read_auth_mode(source)
        if mode == "development":
            return cls(mode=mode)

        issuer = _required_environment_value(source, "AUTOML_JWT_ISSUER")
        audience_value = _required_environment_value(source, "AUTOML_JWT_AUDIENCE")
        audiences = tuple(part.strip() for part in audience_value.split(",") if part.strip())
        if not audiences:
            raise AuthConfigurationError("AUTOML_JWT_AUDIENCE cannot be empty.")
        signing_keys = _read_signing_keys(source)
        cursor_secret = _read_cursor_secret(source)

        raw_leeway = source.get("AUTOML_JWT_LEEWAY_SECONDS", "30")
        try:
            leeway_seconds = int(raw_leeway)
        except ValueError as error:
            raise AuthConfigurationError("AUTOML_JWT_LEEWAY_SECONDS must be an integer.") from error
        if not 0 <= leeway_seconds <= 300:
            raise AuthConfigurationError("AUTOML_JWT_LEEWAY_SECONDS must be between 0 and 300.")
        return cls(
            mode=mode,
            issuer=issuer,
            audiences=audiences,
            signing_keys=signing_keys,
            cursor_secret=cursor_secret,
            leeway_seconds=leeway_seconds,
        )


@runtime_checkable
class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal: ...


class DevelopmentAuthenticator:
    """Synthetic local identity provider; never selected by production mode."""

    def authenticate(self, token: str) -> Principal:
        digest = sha256(token.encode("utf-8")).hexdigest()[:16]
        return Principal(
            subject=f"dev:{digest}",
            tenant_id=f"tenant_{digest}",
            scopes=frozenset({"*"}),
            roles=frozenset({"developer"}),
            actor_type="development",
            authentication_mode="development",
            issuer="automl-development",
        )


class VerifiedTokenAuthenticator:
    """Maps any conforming token verifier into the stable API principal contract."""

    def __init__(self, verifier: TokenVerifier) -> None:
        self._verifier = verifier

    def authenticate(self, token: str) -> Principal:
        verified = self._verifier.verify(token)
        return Principal(
            subject=verified.subject,
            tenant_id=verified.tenant_id,
            scopes=verified.scopes,
            roles=verified.roles,
            actor_type=verified.actor_type,
            authentication_mode="production",
            issuer=verified.issuer,
            key_id=verified.key_id,
        )


def build_authenticator(
    settings: AuthSettings | None = None,
    *,
    verifier: TokenVerifier | None = None,
) -> Authenticator:
    settings = settings or AuthSettings.from_env()
    if settings.mode == "development":
        if verifier is not None:
            raise AuthConfigurationError(
                "A token verifier cannot be configured in development authentication mode."
            )
        return DevelopmentAuthenticator()
    if settings.mode != "production":
        raise AuthConfigurationError("Unsupported authentication mode.")

    if settings.cursor_secret is None:
        raise AuthConfigurationError(
            "Production authentication requires a dedicated cursor signing secret."
        )
    try:
        validate_shared_secret(
            settings.cursor_secret,
            name="Cursor signing secret",
            rejected_values=_INSECURE_CURSOR_SECRETS,
        )
    except ValueError as error:
        raise AuthConfigurationError("The production cursor signing secret is invalid.") from error

    if verifier is None:
        if settings.issuer is None or not settings.audiences or not settings.signing_keys:
            raise AuthConfigurationError(
                "Production authentication requires issuer, audience, and signing keys."
            )
        try:
            verifier = HS256JWTVerifier(
                issuer=settings.issuer,
                audience=settings.audiences,
                keys=settings.signing_keys,
                leeway_seconds=settings.leeway_seconds,
            )
        except ValueError as error:
            raise AuthConfigurationError("The production JWT configuration is invalid.") from error
    return VerifiedTokenAuthenticator(verifier)


def authenticate_bearer_token(
    token: str,
    *,
    settings: AuthSettings | None = None,
    verifier: TokenVerifier | None = None,
) -> Principal:
    if not isinstance(token, str) or not token.strip():
        raise TokenVerificationError("The bearer token is empty.")
    return build_authenticator(settings, verifier=verifier).authenticate(token.strip())


async def require_principal(authorization: str | None = Header(default=None)) -> Principal:
    token = _extract_bearer_token(authorization)
    try:
        return authenticate_bearer_token(token)
    except AuthConfigurationError as error:
        raise APIProblem(
            status=503,
            code="authentication_unavailable",
            title="Authentication is unavailable",
            detail="The service authentication provider is not configured correctly.",
            retriable=False,
        ) from error
    except TokenVerificationError as error:
        raise APIProblem(
            status=401,
            code="unauthorized",
            title="Invalid authentication token",
            detail="The Bearer token is invalid or expired.",
        ) from error


def has_scope(principal: Principal, scope: str) -> bool:
    normalized_scope = _normalize_scope(scope)
    return "*" in principal.scopes or normalized_scope in principal.scopes


def has_scopes(principal: Principal, required_scopes: Sequence[str]) -> bool:
    normalized = tuple(_normalize_scope(scope) for scope in required_scopes)
    return "*" in principal.scopes or all(scope in principal.scopes for scope in normalized)


def enforce_scopes(principal: Principal, *required_scopes: str) -> Principal:
    normalized = tuple(dict.fromkeys(_normalize_scope(scope) for scope in required_scopes))
    if not normalized:
        raise ValueError("At least one required scope must be provided.")
    missing = tuple(scope for scope in normalized if not has_scope(principal, scope))
    if not missing:
        return principal

    advertised = " ".join(normalized)
    raise APIProblem(
        status=403,
        code="insufficient_scope",
        title="Insufficient scope",
        detail="The principal does not have every scope required for this operation.",
        extras={"required_scopes": list(normalized), "missing_scopes": list(missing)},
        headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{advertised}"'},
    )


def require_scopes(*required_scopes: str) -> Callable[[Principal], Awaitable[Principal]]:
    normalized = tuple(dict.fromkeys(_normalize_scope(scope) for scope in required_scopes))
    if not normalized:
        raise ValueError("At least one required scope must be provided.")

    async def dependency(principal: Principal = Depends(require_principal)) -> Principal:
        return enforce_scopes(principal, *normalized)

    return dependency


def require_scope(scope: str) -> Callable[[Principal], Awaitable[Principal]]:
    return require_scopes(scope)


def scope_for_operation(operation_id: str) -> str:
    """Return the exact capability scope required by one canonical API operation."""

    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise ValueError("An operation id must be a valid OpenAPI operationId.")
    return f"{OPERATION_SCOPE_PREFIX}{operation_id}"


def has_operation_scope(principal: Principal, operation_id: str) -> bool:
    return has_scope(principal, scope_for_operation(operation_id))


def enforce_operation_scope(principal: Principal, operation_id: str) -> Principal:
    return enforce_scopes(principal, scope_for_operation(operation_id))


def require_operation_scope(operation_id: str) -> Callable[[Principal], Awaitable[Principal]]:
    return require_scope(scope_for_operation(operation_id))


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise APIProblem(
            status=401,
            code="unauthorized",
            title="Authentication required",
            detail="Provide an Authorization Bearer token.",
        )
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
        or any(character.isspace() for character in token.strip())
    ):
        raise APIProblem(
            status=401,
            code="unauthorized",
            title="Authentication required",
            detail="Provide a non-empty Authorization Bearer token.",
        )
    return token.strip()


def _read_auth_mode(environ: Mapping[str, str]) -> AuthMode:
    configured = environ.get("AUTOML_AUTH_MODE")
    if configured is None:
        configured = environ.get("AUTOML_ENV", "development")
    normalized = configured.strip().lower()
    if normalized in {"development", "dev", "local", "test"}:
        return "development"
    if normalized in {"production", "prod", "staging", "stage", "jwt"}:
        return "production"
    raise AuthConfigurationError("AUTOML_AUTH_MODE must be development or production.")


def _required_environment_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise AuthConfigurationError(f"{name} is required in production mode.")
    return value.strip()


def _read_signing_keys(environ: Mapping[str, str]) -> dict[str, str]:
    serialized_keys = environ.get("AUTOML_JWT_KEYS")
    single_secret = environ.get("AUTOML_JWT_SECRET")
    if serialized_keys and single_secret:
        raise AuthConfigurationError("Configure AUTOML_JWT_KEYS or AUTOML_JWT_SECRET, not both.")
    if serialized_keys:
        try:
            parsed = json.loads(serialized_keys)
        except json.JSONDecodeError as error:
            raise AuthConfigurationError(
                "AUTOML_JWT_KEYS must be a JSON object mapping key ids to secrets."
            ) from error
        if not isinstance(parsed, dict) or not parsed:
            raise AuthConfigurationError("AUTOML_JWT_KEYS must be a non-empty JSON object.")
        keys: dict[str, str] = {}
        for key_id, secret in parsed.items():
            if (
                not isinstance(key_id, str)
                or not key_id.strip()
                or not isinstance(secret, str)
                or not secret
            ):
                raise AuthConfigurationError(
                    "AUTOML_JWT_KEYS key ids and secrets must be non-empty strings."
                )
            keys[key_id] = secret
        return keys
    if single_secret is not None and single_secret:
        key_id = environ.get("AUTOML_JWT_KID", "default").strip()
        if not key_id:
            raise AuthConfigurationError("AUTOML_JWT_KID cannot be empty.")
        return {key_id: single_secret}
    raise AuthConfigurationError("Production mode requires AUTOML_JWT_KEYS or AUTOML_JWT_SECRET.")


def _read_cursor_secret(environ: Mapping[str, str]) -> bytes:
    cursor_secret = _required_environment_value(environ, "AUTOML_CURSOR_SECRET")
    try:
        return validate_shared_secret(
            cursor_secret,
            name="AUTOML_CURSOR_SECRET",
            rejected_values=_INSECURE_CURSOR_SECRETS,
        )
    except ValueError as error:
        raise AuthConfigurationError("AUTOML_CURSOR_SECRET is not production-safe.") from error


def _normalize_scope(scope: str) -> str:
    return normalize_scope(scope)


__all__ = [
    "AuthConfigurationError",
    "AuthSettings",
    "Authenticator",
    "DevelopmentAuthenticator",
    "OPERATION_SCOPE_PREFIX",
    "Principal",
    "VerifiedTokenAuthenticator",
    "authenticate_bearer_token",
    "build_authenticator",
    "enforce_operation_scope",
    "enforce_scopes",
    "has_operation_scope",
    "has_scope",
    "has_scopes",
    "require_principal",
    "require_operation_scope",
    "require_scope",
    "require_scopes",
    "scope_for_operation",
]
