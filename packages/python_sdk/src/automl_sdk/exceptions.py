"""Exceptions raised by the managed AutoML SDK."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class AutoMLError(Exception):
    """Base class for all SDK errors."""


class TransportError(AutoMLError):
    """The API could not be reached after transport retries."""


class ProtocolError(AutoMLError):
    """The API returned a response that violates the public contract."""


class APIError(AutoMLError):
    """An HTTP error represented by an RFC 9457 problem document."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        problem: Mapping[str, Any] | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.problem = dict(problem or {})
        self.response = response
        self.retriable = bool(self.problem.get("retriable", False))
        self.correlation_id = self.problem.get("correlation_id")
        self.retry_after = _retry_after_seconds(response)


class BadRequestError(APIError):
    """The request was malformed."""


class AuthenticationError(APIError):
    """Authentication is missing or invalid."""


class AuthorizationError(APIError):
    """The authenticated principal cannot access the resource."""


class NotFoundError(APIError):
    """The resource does not exist or is not visible to the principal."""


class ConflictError(APIError):
    """The request conflicts with current resource state."""


class GoneError(APIError):
    """The requested resource is no longer available."""


class PreconditionFailedError(APIError):
    """An If-Match precondition used a stale revision."""


class ValidationError(APIError):
    """The request failed semantic validation."""


class RateLimitError(APIError):
    """The service rejected the request due to a rate or concurrency limit."""


class ServerError(APIError):
    """The service failed while processing the request."""


class CursorExpiredError(GoneError):
    """Base class for an expired continuation cursor."""

    @property
    def recovery(self) -> Mapping[str, Any]:
        value = self.problem.get("recovery")
        return value if isinstance(value, Mapping) else {}


class EventCursorExpiredError(CursorExpiredError):
    """Event history was truncated; recovery must continue from a snapshot."""

    @property
    def retained_from_seq(self) -> int | None:
        value = self.problem.get("retained_from_seq")
        return value if isinstance(value, int) else None

    @property
    def lost_event_range(self) -> Mapping[str, Any]:
        value = self.problem.get("lost_event_range")
        return value if isinstance(value, Mapping) else {}


class CollectionCursorExpiredError(CursorExpiredError):
    """A collection query must be restarted from its first page."""


class WaitTimeoutError(AutoMLError):
    """A high-level wait operation exceeded its deadline."""

    def __init__(self, operation: str, resource_id: str, timeout: float | None) -> None:
        self.operation = operation
        self.resource_id = resource_id
        self.timeout = timeout
        suffix = "without a deadline" if timeout is None else f"after {timeout:g}s"
        super().__init__(f"Timed out waiting for {operation} on {resource_id} {suffix}")


class CommandFailedError(AutoMLError):
    """An asynchronous command reached FAILED."""

    def __init__(self, command: Mapping[str, Any]) -> None:
        self.command = dict(command)
        problem = self.command.get("problem")
        detail = problem.get("detail") if isinstance(problem, Mapping) else None
        command_id = self.command.get("command_id", "<unknown>")
        super().__init__(detail or f"Command {command_id} failed")


class RunTerminalError(AutoMLError):
    """A Run terminated before the awaited interaction occurred."""

    def __init__(self, run: Mapping[str, Any]) -> None:
        self.run = dict(run)
        run_id = self.run.get("run_id", "<unknown>")
        outcome = self.run.get("outcome") or "UNKNOWN"
        super().__init__(f"Run {run_id} reached terminal outcome {outcome}")


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


# A descriptive alias for users who prefer the package-qualified name.
AutoMLAPIError = APIError
