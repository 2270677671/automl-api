#!/usr/bin/env python3
"""Issue a short-lived operation-scoped token from a protected env file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import cast

from automl_api.credentials import ActorType, issue_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue an HS256 JWT containing exact canonical AutoML operation scopes."
    )
    parser.add_argument("--env-file", default=".env.production-single")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--operation", action="append", required=True)
    parser.add_argument("--actor-type", choices=("agent", "service", "human"), default="agent")
    parser.add_argument("--expires-in", type=int, default=3600, metavar="SECONDS")
    parser.add_argument("--key-id")
    return parser


def _load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SystemExit(f"cannot read {path}: {error}") from error
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not key.replace("_", "").isalnum():
            raise SystemExit(f"invalid assignment in {path}:{line_number}")
        os.environ[key] = value


def main() -> int:
    os.umask(0o077)
    args = _parser().parse_args()
    _load_env(Path(args.env_file))
    try:
        issued = issue_token(
            subject=args.subject,
            tenant_id=args.tenant,
            actor_type=cast(ActorType, args.actor_type),
            operations=args.operation,
            expires_in=args.expires_in,
            key_id=args.key_id,
        )
    except (TypeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(issued["token"])
    print(
        f"issued tenant={issued['tenant_id']} subject={issued['subject']} "
        f"expires_in={issued['expires_in']}s scopes={len(issued['scopes'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
