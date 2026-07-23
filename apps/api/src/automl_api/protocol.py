from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

from .errors import APIProblem
from .security import validate_shared_secret


_CURSOR_SECRET = os.urandom(32)


def configure_cursor_secret(secret: str | bytes) -> None:
    """Install the HMAC key used for opaque API cursors."""

    global _CURSOR_SECRET
    _CURSOR_SECRET = validate_shared_secret(secret, name="Cursor signing secret")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def request_fingerprint(
    *,
    method: str,
    path: str,
    query: dict[str, Any] | None,
    body: Any,
    conditions: dict[str, str] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "method": method.upper(),
            "path": path,
            "query": query or {},
            "body": body,
            "conditions": conditions or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        combined = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw, supplied_signature = combined[:-32], combined[-32:]
        expected_signature = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("cursor payload must be an object")
        return value
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise APIProblem(
            status=400,
            code="invalid_cursor",
            title="Invalid cursor",
            detail="The cursor is malformed or was issued for a different query.",
        ) from exc


def parse_revision_etag(value: str, *, field: str) -> int:
    if len(value) < 3 or value[0] != '"' or value[-1] != '"':
        raise APIProblem(
            status=400,
            code="invalid_if_match",
            title="Invalid If-Match",
            detail=f"If-Match must be a quoted positive integer for {field}.",
        )
    try:
        revision = int(value[1:-1])
    except ValueError as exc:
        raise APIProblem(
            status=400,
            code="invalid_if_match",
            title="Invalid If-Match",
            detail=f"If-Match must be a quoted positive integer for {field}.",
        ) from exc
    if revision < 1:
        raise APIProblem(
            status=400,
            code="invalid_if_match",
            title="Invalid If-Match",
            detail=f"If-Match must be a quoted positive integer for {field}.",
        )
    return revision
