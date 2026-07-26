from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import json
import os
import time
from typing import Literal, cast
from uuid import uuid4

import jwt

from .auth import AuthConfigurationError, AuthSettings, scope_for_operation
from .security import HS256JWTVerifier


ActorType = Literal["human", "agent", "service"]


def issue_token(
    *,
    subject: str,
    tenant_id: str,
    actor_type: ActorType,
    operations: Sequence[str],
    expires_in: int,
    key_id: str | None = None,
) -> dict[str, str | int | list[str]]:
    if not 60 <= expires_in <= 86_400:
        raise ValueError("expires_in must be between 60 and 86400 seconds")
    settings = AuthSettings.from_env()
    if settings.mode != "production" or not settings.signing_keys:
        raise AuthConfigurationError(
            "Local credential issuance requires production HS256 keys; it is unavailable in JWKS mode."
        )
    selected_key_id = key_id or next(iter(settings.signing_keys))
    signing_key = settings.signing_keys.get(selected_key_id)
    if signing_key is None:
        raise ValueError("the selected key id is not configured")
    scopes = sorted({scope_for_operation(operation) for operation in operations})
    if not scopes:
        raise ValueError("at least one --operation is required")

    issued_at = int(time.time())
    expires_at = issued_at + expires_in
    payload = {
        "iss": settings.issuer,
        "aud": settings.audiences[0],
        "sub": subject,
        "tenant_id": tenant_id,
        "actor_type": actor_type,
        "scopes": scopes,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": expires_at,
        "jti": f"cred_{uuid4().hex}",
    }
    token = jwt.encode(
        payload,
        signing_key,
        algorithm="HS256",
        headers={"kid": selected_key_id, "typ": "JWT"},
    )
    verifier = HS256JWTVerifier(
        issuer=str(settings.issuer),
        audience=settings.audiences,
        keys=settings.signing_keys,
        leeway_seconds=settings.leeway_seconds,
    )
    verifier.verify(token, now=issued_at)
    return {
        "token": token,
        "token_type": "Bearer",
        "subject": subject,
        "tenant_id": tenant_id,
        "actor_type": actor_type,
        "key_id": selected_key_id,
        "scopes": scopes,
        "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat().replace("+00:00", "Z"),
        "expires_in": expires_in,
    }


def _positive_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expires-in must be an integer") from error
    if not 60 <= parsed <= 86_400:
        raise argparse.ArgumentTypeError("expires-in must be between 60 and 86400")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue a short-lived, operation-scoped HS256 credential for an Agent platform."
    )
    parser.add_argument("--subject", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--actor-type", choices=("human", "agent", "service"), default="service")
    parser.add_argument("--operation", action="append", required=True)
    parser.add_argument("--expires-in", type=_positive_seconds, default=3600)
    parser.add_argument("--key-id")
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="print only the encoded token for shell integration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        result = issue_token(
            subject=args.subject,
            tenant_id=args.tenant,
            actor_type=cast(ActorType, args.actor_type),
            operations=args.operation,
            expires_in=args.expires_in,
            key_id=args.key_id,
        )
    except (AuthConfigurationError, ValueError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False))
        return 1
    if args.token_only:
        print(result["token"])
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["issue_token"]
