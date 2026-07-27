"""Official synchronous Python SDK for the Managed AutoML HTTP API.

Live workflow helpers use reliable paged JSON event replay as the portable
fallback when an application does not run a dedicated SSE consumer.
"""

from .client import CANONICAL_OPERATION_METHODS, AutoMLClient
from .exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    AutoMLAPIError,
    AutoMLError,
    BadRequestError,
    CollectionCursorExpiredError,
    CommandFailedError,
    ConflictError,
    CursorExpiredError,
    EventCursorExpiredError,
    GoneError,
    NotFoundError,
    OAuthTokenError,
    PreconditionFailedError,
    ProtocolError,
    RateLimitError,
    RunTerminalError,
    ServerError,
    TransportError,
    ValidationError,
    WaitTimeoutError,
)
from .oauth import OAuth2ClientCredentialsTokenProvider

__all__ = [
    "APIError",
    "AuthenticationError",
    "AuthorizationError",
    "AutoMLAPIError",
    "AutoMLClient",
    "CANONICAL_OPERATION_METHODS",
    "AutoMLError",
    "BadRequestError",
    "CollectionCursorExpiredError",
    "CommandFailedError",
    "ConflictError",
    "CursorExpiredError",
    "EventCursorExpiredError",
    "GoneError",
    "NotFoundError",
    "OAuth2ClientCredentialsTokenProvider",
    "OAuthTokenError",
    "PreconditionFailedError",
    "ProtocolError",
    "RateLimitError",
    "RunTerminalError",
    "ServerError",
    "TransportError",
    "ValidationError",
    "WaitTimeoutError",
]

__version__ = "0.8.0"
