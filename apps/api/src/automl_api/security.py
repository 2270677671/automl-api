from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast, runtime_checkable


_BASE64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
_SCOPE_VALUE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]{1,256}$")
_MAX_TOKEN_BYTES = 64 * 1024
_MAX_CLAIM_VALUE_LENGTH = 256
_MIN_SHARED_SECRET_BYTES = 32
_ACTOR_TYPES = frozenset({"human", "agent", "service"})

ActorType = Literal["human", "agent", "service"]


class TokenVerificationError(ValueError):
    """Raised when a token cannot be authenticated.

    The exception deliberately does not retain the token or signing key so callers can log the
    exception type without leaking credentials.
    """


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    subject: str
    tenant_id: str
    scopes: frozenset[str]
    roles: frozenset[str]
    actor_type: ActorType
    issuer: str
    key_id: str | None


@runtime_checkable
class TokenVerifier(Protocol):
    """Adapter boundary for HS256 preview verification and future OIDC/JWKS providers."""

    def verify(self, token: str, *, now: float | None = None) -> VerifiedToken: ...


class HS256JWTVerifier:
    """Strict HS256 JWT verifier for partner-preview deployments.

    This is intentionally not an OIDC implementation. A production OIDC/JWKS adapter can implement
    ``TokenVerifier`` without changing the FastAPI dependency or the principal contract.
    """

    algorithm = "HS256"

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | Iterable[str],
        keys: Mapping[str, str | bytes],
        leeway_seconds: int = 30,
    ) -> None:
        self._issuer = _nonempty_string(issuer, "issuer")
        audiences = (audience,) if isinstance(audience, str) else tuple(audience)
        self._audiences = frozenset(_nonempty_string(value, "audience") for value in audiences)
        if not self._audiences:
            raise ValueError("At least one expected audience is required.")
        if not isinstance(leeway_seconds, int) or isinstance(leeway_seconds, bool):
            raise ValueError("leeway_seconds must be an integer.")
        if not 0 <= leeway_seconds <= 300:
            raise ValueError("leeway_seconds must be between 0 and 300.")
        self._leeway_seconds = leeway_seconds

        normalized_keys: dict[str, bytes] = {}
        for key_id, secret in keys.items():
            normalized_key_id = _nonempty_string(key_id, "key id")
            if normalized_key_id in normalized_keys:
                raise ValueError(f"Duplicate key id: {normalized_key_id}.")
            secret_bytes = validate_shared_secret(
                secret,
                name=f"Signing key {normalized_key_id!r}",
            )
            normalized_keys[normalized_key_id] = secret_bytes
        if not normalized_keys:
            raise ValueError("At least one HS256 signing key is required.")
        self._keys = MappingProxyType(normalized_keys)

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def audiences(self) -> frozenset[str]:
        return self._audiences

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def verify(self, token: str, *, now: float | None = None) -> VerifiedToken:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise TokenVerificationError("The bearer token is invalid.")

        segments = token.split(".")
        if len(segments) != 3 or any(not segment for segment in segments):
            raise TokenVerificationError("The bearer token is invalid.")
        header_segment, payload_segment, signature_segment = segments
        header = _decode_json_object(header_segment)
        payload = _decode_json_object(payload_segment)

        if header.get("alg") != self.algorithm:
            raise TokenVerificationError("The bearer token uses an unsupported algorithm.")
        if "crit" in header:
            raise TokenVerificationError("Critical JWT headers are not supported.")

        key_id = header.get("kid")
        if key_id is not None and not isinstance(key_id, str):
            raise TokenVerificationError("The bearer token key id is invalid.")
        if key_id is None:
            if len(self._keys) != 1:
                raise TokenVerificationError("A key id is required while keys are rotating.")
            selected_key_id, signing_key = next(iter(self._keys.items()))
            verified_key_id: str | None = selected_key_id
        else:
            signing_key = self._keys.get(key_id)
            if signing_key is None:
                raise TokenVerificationError("The bearer token key id is unknown.")
            verified_key_id = key_id

        signature = _decode_base64url(signature_segment)
        if len(signature) != sha256().digest_size:
            raise TokenVerificationError("The bearer token signature is invalid.")
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        expected_signature = hmac.new(signing_key, signing_input, sha256).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise TokenVerificationError("The bearer token signature is invalid.")

        timestamp = time.time() if now is None else now
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValueError("now must be a finite Unix timestamp.")
        timestamp = float(timestamp)
        if not math.isfinite(timestamp):
            raise ValueError("now must be a finite Unix timestamp.")

        expiration = _numeric_date(payload.get("exp"), "exp", required=True)
        assert expiration is not None
        if timestamp >= expiration + self._leeway_seconds:
            raise TokenVerificationError("The bearer token has expired.")
        not_before = _numeric_date(payload.get("nbf"), "nbf", required=False)
        if not_before is not None and timestamp + self._leeway_seconds < not_before:
            raise TokenVerificationError("The bearer token is not active yet.")

        if payload.get("iss") != self._issuer:
            raise TokenVerificationError("The bearer token issuer is invalid.")
        token_audiences = _audience_claim(payload.get("aud"))
        if self._audiences.isdisjoint(token_audiences):
            raise TokenVerificationError("The bearer token audience is invalid.")

        subject = _identifier_claim(payload.get("sub"), "sub")
        tenant_id = _tenant_id_claim(payload.get("tenant_id"))
        scopes = _claim_set(payload.get("scope"), "scope") | _claim_set(
            payload.get("scopes"), "scopes"
        )
        roles = _claim_set(payload.get("role"), "role") | _claim_set(payload.get("roles"), "roles")
        actor_type = _actor_type_claim(payload.get("actor_type", "service"))

        return VerifiedToken(
            subject=subject,
            tenant_id=tenant_id,
            scopes=scopes,
            roles=roles,
            actor_type=actor_type,
            issuer=self._issuer,
            key_id=verified_key_id,
        )


class JWKSJWTVerifier:
    """OIDC/JWKS verifier for formal production deployments."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | Iterable[str],
        algorithms: Iterable[str],
        jwks_url: str | None = None,
        jwks_json: Mapping[str, Any] | None = None,
        leeway_seconds: int = 30,
    ) -> None:
        try:
            import jwt
        except ImportError as error:
            raise ValueError("PyJWT[crypto] is required for OIDC/JWKS verification.") from error
        self._jwt = jwt
        self._issuer = _nonempty_string(issuer, "issuer")
        audiences = (audience,) if isinstance(audience, str) else tuple(audience)
        self._audiences = frozenset(_nonempty_string(value, "audience") for value in audiences)
        if not self._audiences:
            raise ValueError("At least one expected audience is required.")
        normalized_algorithms = tuple(_nonempty_string(value, "algorithm") for value in algorithms)
        if not normalized_algorithms:
            raise ValueError("At least one JWT algorithm is required.")
        if any(
            value.upper() == "NONE" or value.upper().startswith("HS")
            for value in normalized_algorithms
        ):
            raise ValueError("JWKS verification only accepts asymmetric JWT algorithms.")
        self._algorithms = normalized_algorithms
        if not isinstance(leeway_seconds, int) or isinstance(leeway_seconds, bool):
            raise ValueError("leeway_seconds must be an integer.")
        if not 0 <= leeway_seconds <= 300:
            raise ValueError("leeway_seconds must be between 0 and 300.")
        self._leeway_seconds = leeway_seconds
        if bool(jwks_url) == bool(jwks_json):
            raise ValueError("Configure exactly one of jwks_url or jwks_json.")
        self._client = jwt.PyJWKClient(jwks_url) if jwks_url else None
        self._jwks = jwt.PyJWKSet.from_dict(dict(jwks_json or {})) if jwks_json else None

    def verify(self, token: str, *, now: float | None = None) -> VerifiedToken:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise TokenVerificationError("The bearer token is invalid.")
        try:
            header = self._jwt.get_unverified_header(token)
            if header.get("crit"):
                raise TokenVerificationError("Critical JWT headers are not supported.")
            key_id = header.get("kid")
            if key_id is not None and not isinstance(key_id, str):
                raise TokenVerificationError("The bearer token key id is invalid.")
            signing_key = self._signing_key(token, key_id)
            payload = self._jwt.decode(
                token,
                key=signing_key,
                algorithms=list(self._algorithms),
                audience=list(self._audiences),
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except TokenVerificationError:
            raise
        except Exception as error:
            raise TokenVerificationError("The bearer token is invalid or expired.") from error
        if not isinstance(payload, dict):
            raise TokenVerificationError("The bearer token payload is invalid.")
        return VerifiedToken(
            subject=_identifier_claim(payload.get("sub"), "sub"),
            tenant_id=_tenant_id_claim(payload.get("tenant_id")),
            scopes=_claim_set(payload.get("scope"), "scope")
            | _claim_set(payload.get("scopes"), "scopes"),
            roles=_claim_set(payload.get("role"), "role")
            | _claim_set(payload.get("roles"), "roles"),
            actor_type=_actor_type_claim(payload.get("actor_type", "service")),
            issuer=self._issuer,
            key_id=key_id,
        )

    def _signing_key(self, token: str, key_id: str | None) -> Any:
        if self._client is not None:
            return self._client.get_signing_key_from_jwt(token).key
        assert self._jwks is not None
        if key_id is None:
            if len(self._jwks.keys) != 1:
                raise TokenVerificationError("A key id is required while keys are rotating.")
            return self._jwks.keys[0].key
        for key in self._jwks.keys:
            if key.key_id == key_id:
                return key.key
        raise TokenVerificationError("The bearer token key id is unknown.")


def _decode_json_object(segment: str) -> dict[str, Any]:
    try:
        decoded = _decode_base64url(segment).decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenVerificationError("The bearer token is invalid.") from error
    if not isinstance(value, dict):
        raise TokenVerificationError("JWT header and payload must be JSON objects.")
    return value


def _decode_base64url(segment: str) -> bytes:
    if not _BASE64URL_SEGMENT.fullmatch(segment):
        raise TokenVerificationError("The bearer token is invalid.")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.b64decode(segment + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise TokenVerificationError("The bearer token is invalid.") from error


def _numeric_date(value: Any, name: str, *, required: bool) -> float | None:
    if value is None:
        if required:
            raise TokenVerificationError(f"The bearer token is missing {name}.")
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TokenVerificationError(f"The bearer token {name} claim is invalid.")
    result = float(value)
    if not math.isfinite(result):
        raise TokenVerificationError(f"The bearer token {name} claim is invalid.")
    return result


def _audience_claim(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        audiences = (value,)
    elif isinstance(value, list):
        audiences = tuple(value)
    else:
        raise TokenVerificationError("The bearer token audience is invalid.")
    try:
        normalized = frozenset(_nonempty_string(item, "audience") for item in audiences)
    except (TypeError, ValueError) as error:
        raise TokenVerificationError("The bearer token audience is invalid.") from error
    if not normalized:
        raise TokenVerificationError("The bearer token audience is invalid.")
    return normalized


def _identifier_claim(value: Any, name: str) -> str:
    try:
        normalized = _nonempty_string(value, name)
    except (TypeError, ValueError) as error:
        raise TokenVerificationError(f"The bearer token {name} claim is invalid.") from error
    if len(normalized) > _MAX_CLAIM_VALUE_LENGTH or any(
        ord(character) < 32 for character in normalized
    ):
        raise TokenVerificationError(f"The bearer token {name} claim is invalid.")
    return normalized


def _tenant_id_claim(value: Any) -> str:
    tenant_id = _identifier_claim(value, "tenant_id")
    if not _TENANT_ID.fullmatch(tenant_id):
        raise TokenVerificationError("The bearer token tenant_id claim is invalid.")
    return tenant_id


def _actor_type_claim(value: Any) -> ActorType:
    actor_type = _identifier_claim(value, "actor_type")
    if actor_type not in _ACTOR_TYPES:
        raise TokenVerificationError("The bearer token actor_type claim is invalid.")
    return cast(ActorType, actor_type)


def _claim_set(value: Any, name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, list):
        items = value
    else:
        raise TokenVerificationError(f"The bearer token {name} claim is invalid.")

    normalized: set[str] = set()
    for item in items:
        try:
            claim_value = normalize_scope(item)
        except (TypeError, ValueError) as error:
            raise TokenVerificationError(f"The bearer token {name} claim is invalid.") from error
        normalized.add(claim_value)
    return frozenset(normalized)


def normalize_scope(value: Any) -> str:
    """Validate an OAuth scope-token value safe for authorization headers and logs."""

    if not isinstance(value, str) or _SCOPE_VALUE.fullmatch(value) is None:
        raise ValueError("A scope must be a valid visible ASCII OAuth scope-token.")
    return value


def validate_shared_secret(
    secret: str | bytes,
    *,
    name: str = "Shared secret",
    rejected_values: Iterable[str | bytes] = (),
) -> bytes:
    """Validate secret material used by an HMAC boundary without exposing its value."""

    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(secret_bytes, bytes):
        raise ValueError(f"{name} must be a string or bytes.")
    if len(secret_bytes) < _MIN_SHARED_SECRET_BYTES:
        raise ValueError(f"{name} must contain at least {_MIN_SHARED_SECRET_BYTES} bytes.")
    for rejected in rejected_values:
        rejected_bytes = rejected.encode("utf-8") if isinstance(rejected, str) else rejected
        if hmac.compare_digest(secret_bytes, rejected_bytes):
            raise ValueError(f"{name} uses a publicly known placeholder value.")
    return secret_bytes


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace.")
    return value


__all__ = [
    "ActorType",
    "HS256JWTVerifier",
    "JWKSJWTVerifier",
    "TokenVerificationError",
    "TokenVerifier",
    "VerifiedToken",
    "normalize_scope",
    "validate_shared_secret",
]
