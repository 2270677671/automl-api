#!/usr/bin/env python3
"""Call the Agent manifest with an automatically refreshed OAuth access token."""

from __future__ import annotations

import os
from pathlib import Path
import ssl

import httpx

from automl_sdk import AutoMLClient, OAuth2ClientCredentialsTokenProvider


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> int:
    base_url = _required("AUTOML_API_URL")
    token_url = _required("AUTOML_OIDC_TOKEN_URL")
    client_id = _required("AUTOML_OIDC_CLIENT_ID")
    client_secret = _required("AUTOML_OIDC_CLIENT_SECRET")
    ca_file = Path(_required("AUTOML_CA_FILE"))
    if not ca_file.is_file():
        raise SystemExit(f"AUTOML_CA_FILE does not exist: {ca_file}")

    tls = ssl.create_default_context(cafile=str(ca_file))
    with (
        OAuth2ClientCredentialsTokenProvider(
            token_url,
            client_id=client_id,
            client_secret=client_secret,
            verify=tls,
        ) as tokens,
        httpx.Client(verify=tls, timeout=30) as http,
        AutoMLClient(base_url, token=tokens, http_client=http) as api,
    ):
        manifest = api.get_agent_manifest()
        print(
            f"service_version={manifest['service_version']} "
            f"profile_id={manifest['profile_id']} "
            f"planner_location={manifest['planner_location']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
