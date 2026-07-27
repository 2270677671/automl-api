"""OAuth 2.0 client-credentials token acquisition for machine Agent platforms."""

from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from .exceptions import OAuthTokenError


class OAuth2ClientCredentialsTokenProvider:
    """Acquire and cache a short-lived OAuth access token.

    Pass an instance as ``AutoMLClient(token=provider)``. The provider uses
    HTTP Basic client authentication, keeps the client secret out of AutoML
    requests, and obtains a new token before the cached credential expires.
    """

    def __init__(
        self,
        token_url: str,
        *,
        client_id: str,
        client_secret: str | Callable[[], str],
        scopes: str | Sequence[str] | None = None,
        timeout: float | httpx.Timeout = 15.0,
        verify: ssl.SSLContext | str | bool = True,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        refresh_skew_seconds: float = 30.0,
        allow_insecure_http: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            parsed_url = httpx.URL(token_url)
        except (httpx.InvalidURL, ValueError) as error:
            raise ValueError("token_url is invalid") from error
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise ValueError("token_url must be an absolute HTTP(S) URL")
        if parsed_url.scheme != "https" and not allow_insecure_http:
            raise ValueError("token_url must use HTTPS outside an explicit local test")
        if not isinstance(client_id, str) or not client_id or client_id != client_id.strip():
            raise ValueError("client_id must be a non-empty string without surrounding whitespace")
        if not callable(client_secret):
            self._validate_secret(client_secret)
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be non-negative")
        if http_client is not None and transport is not None:
            raise ValueError("transport cannot be combined with http_client")

        self._token_url = str(parsed_url)
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = self._normalize_scopes(scopes)
        self._refresh_skew_seconds = float(refresh_skew_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout,
            verify=verify,
            transport=transport,
        )

    def __enter__(self) -> OAuth2ClientCredentialsTokenProvider:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __call__(self) -> str:
        now = self._clock()
        with self._lock:
            if (
                self._access_token is not None
                and now < self._expires_at - self._refresh_skew_seconds
            ):
                return self._access_token
            token, expires_in = self._request_token()
            self._access_token = token
            self._expires_at = self._clock() + expires_in
            return token

    def invalidate(self) -> None:
        """Discard the cached access token without exposing its value."""

        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def close(self) -> None:
        """Close the internally owned token-endpoint connection pool."""

        if self._owns_client:
            self._client.close()

    def _request_token(self) -> tuple[str, int]:
        secret = self._resolve_secret()
        form = {"grant_type": "client_credentials"}
        if self._scope is not None:
            form["scope"] = self._scope
        try:
            response = self._client.post(
                self._token_url,
                data=form,
                auth=httpx.BasicAuth(self._client_id, secret),
                headers={"Accept": "application/json"},
            )
        except httpx.TransportError as error:
            raise OAuthTokenError("The OAuth token endpoint could not be reached.") from error

        if response.status_code != 200:
            error_code = self._oauth_error_code(response)
            raise OAuthTokenError(
                "The OAuth token endpoint rejected the client credentials.",
                status_code=response.status_code,
                error=error_code,
            )
        try:
            payload: Any = response.json()
        except ValueError as error:
            raise OAuthTokenError("The OAuth token response is not valid JSON.") from error
        if not isinstance(payload, dict):
            raise OAuthTokenError("The OAuth token response must be a JSON object.")

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthTokenError("The OAuth token response is missing access_token.")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise OAuthTokenError("The OAuth token response token_type is not Bearer.")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= 86_400
        ):
            raise OAuthTokenError(
                "The OAuth token response expires_in must be between 1 and 86400 seconds."
            )
        return access_token, expires_in

    def _resolve_secret(self) -> str:
        value = self._client_secret() if callable(self._client_secret) else self._client_secret
        return self._validate_secret(value)

    @staticmethod
    def _validate_secret(value: Any) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(
                "client_secret must be a non-empty string without surrounding whitespace"
            )
        return value

    @staticmethod
    def _normalize_scopes(scopes: str | Sequence[str] | None) -> str | None:
        if scopes is None:
            return None
        values = scopes.split() if isinstance(scopes, str) else list(scopes)
        if not values:
            raise ValueError("scopes must not be empty")
        normalized: list[str] = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or any(character.isspace() or ord(character) < 33 for character in value)
            ):
                raise ValueError("each OAuth scope must be a visible token without whitespace")
            normalized.append(value)
        return " ".join(dict.fromkeys(normalized))

    @staticmethod
    def _oauth_error_code(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        value = payload.get("error") if isinstance(payload, dict) else None
        return value if isinstance(value, str) and len(value) <= 128 else None


__all__ = ["OAuth2ClientCredentialsTokenProvider"]
