"""Registry and request validation for tabular AutoML backend adapters."""

from __future__ import annotations

from importlib import import_module
from threading import RLock
from typing import Any

from ..ml_engine import Source, TabularAutoMLResult, TaskType
from .base import (
    BackendDescriptor,
    BackendMediaTypeUnsupportedError,
    BackendNotFoundError,
    BackendRegistrationError,
    BackendTaskUnsupportedError,
    BackendUnavailableError,
    TabularBackend,
)
from .sklearn import SklearnBackend


class BackendRegistry:
    """Thread-safe registry with deterministic lookup and stable failures."""

    def __init__(self, *, default_backend_id: str = "sklearn") -> None:
        self.default_backend_id = self.normalize_backend_id(default_backend_id)
        self._backends: dict[str, TabularBackend] = {}
        self._lock = RLock()

    @staticmethod
    def normalize_backend_id(backend_id: str | None) -> str:
        if backend_id is None:
            return "sklearn"
        if not isinstance(backend_id, str):
            raise BackendNotFoundError(
                "backend_id must be a string.",
                backend_id=backend_id,
            )
        normalized = backend_id.strip().lower()
        if not normalized:
            raise BackendNotFoundError("backend_id must not be empty.", backend_id=backend_id)
        return normalized

    def register(self, backend: TabularBackend, *, replace: bool = False) -> None:
        if not isinstance(backend, TabularBackend):
            raise TypeError("backend must implement the TabularBackend protocol")
        descriptor = backend.descriptor
        backend_id = self.normalize_backend_id(descriptor.backend_id)
        with self._lock:
            if backend_id in self._backends and not replace:
                raise BackendRegistrationError(
                    "A backend with this identifier is already registered.",
                    backend_id=backend_id,
                )
            self._backends[backend_id] = backend

    def get(self, backend_id: str | None = None) -> TabularBackend:
        selected = self.default_backend_id if backend_id is None else backend_id
        normalized = self.normalize_backend_id(selected)
        with self._lock:
            backend = self._backends.get(normalized)
            known = sorted(self._backends)
        if backend is None:
            raise BackendNotFoundError(
                "The requested AutoML backend is not registered.",
                backend_id=normalized,
                known_backend_ids=known,
            )
        return backend

    def require_available(self, backend_id: str | None = None) -> TabularBackend:
        backend = self.get(backend_id)
        descriptor = backend.descriptor
        if not descriptor.available:
            raise BackendUnavailableError(
                "The requested AutoML backend is not ready in this runtime.",
                backend_id=descriptor.backend_id,
                optional_dependency=descriptor.optional_dependency,
                reason=descriptor.unavailable_reason,
            )
        return backend

    def validate_request(
        self,
        backend_id: str | None = None,
        *,
        task_type: TaskType | str | None = None,
        media_type: str | None = None,
    ) -> TabularBackend:
        """Resolve an available backend and validate its declared capabilities."""

        backend = self.require_available(backend_id)
        descriptor = backend.descriptor
        normalized_media_type = media_type.lower().strip() if media_type else None
        normalized_media_type = {
            "csv": "text/csv",
            "parquet": "application/vnd.apache.parquet",
        }.get(normalized_media_type, normalized_media_type)
        if (
            normalized_media_type
            and normalized_media_type not in descriptor.capabilities.media_types
        ):
            raise BackendMediaTypeUnsupportedError(
                "The selected backend does not support this dataset media type.",
                backend_id=descriptor.backend_id,
                media_type=normalized_media_type,
                supported_media_types=list(descriptor.capabilities.media_types),
            )
        task_value = getattr(task_type, "value", task_type)
        normalized_task_type = str(task_value).upper().strip() if task_value is not None else None
        if normalized_task_type and normalized_task_type not in descriptor.capabilities.task_types:
            raise BackendTaskUnsupportedError(
                "The selected backend does not support this task type.",
                backend_id=descriptor.backend_id,
                task_type=normalized_task_type,
                supported_task_types=list(descriptor.capabilities.task_types),
            )
        return backend

    def descriptors(self) -> list[BackendDescriptor]:
        with self._lock:
            backends = [self._backends[key] for key in sorted(self._backends)]
        return [backend.descriptor for backend in backends]

    def status(self) -> list[dict[str, Any]]:
        return [descriptor.as_dict() for descriptor in self.descriptors()]

    def run(
        self,
        backend_id: str | None,
        source: Source,
        *,
        target_column: str | None,
        media_type: str | None = None,
        task_type: TaskType | str | None = None,
        positive_class: Any | None = None,
        primary_metric: str | None = None,
        iid_confirmed: bool = False,
        seed: int = 1729,
        test_size: float = 0.2,
        cv_folds: int = 3,
        max_categories: int = 128,
        max_trials: int | None = None,
        max_wall_time_seconds: int | None = None,
    ) -> TabularAutoMLResult:
        backend = self.validate_request(
            backend_id,
            task_type=task_type,
            media_type=media_type,
        )
        return backend.run(
            source,
            target_column=target_column,
            media_type=media_type,
            task_type=task_type,
            positive_class=positive_class,
            primary_metric=primary_metric,
            iid_confirmed=iid_confirmed,
            seed=seed,
            test_size=test_size,
            cv_folds=cv_folds,
            max_categories=max_categories,
            max_trials=max_trials,
            max_wall_time_seconds=max_wall_time_seconds,
        )


_STANDARD_BACKENDS = (
    ("automl_api.backends.autogluon", "AutoGluonBackend"),
    ("automl_api.backends.tabpfn", "TabPFNBackend"),
)


def build_default_registry() -> BackendRegistry:
    """Build the service registry without importing heavy frameworks eagerly."""

    registry = BackendRegistry(default_backend_id="sklearn")
    registry.register(SklearnBackend())
    for module_name, class_name in _STANDARD_BACKENDS:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name == module_name:
                continue
            raise
        backend_class = getattr(module, class_name)
        registry.register(backend_class())
    return registry


default_backend_registry = build_default_registry()


__all__ = ["BackendRegistry", "build_default_registry", "default_backend_registry"]
