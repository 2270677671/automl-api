"""Pluggable execution backends for tabular AutoML."""

from .base import (
    BackendCapabilities,
    BackendDescriptor,
    BackendError,
    BackendMediaTypeUnsupportedError,
    BackendNotFoundError,
    BackendRegistrationError,
    BackendTaskUnsupportedError,
    BackendUnavailableError,
    TabularBackend,
)
from .registry import BackendRegistry, build_default_registry, default_backend_registry
from .sklearn import SklearnBackend

__all__ = [
    "BackendCapabilities",
    "BackendDescriptor",
    "BackendError",
    "BackendMediaTypeUnsupportedError",
    "BackendNotFoundError",
    "BackendRegistrationError",
    "BackendRegistry",
    "BackendTaskUnsupportedError",
    "BackendUnavailableError",
    "SklearnBackend",
    "TabularBackend",
    "build_default_registry",
    "default_backend_registry",
]
