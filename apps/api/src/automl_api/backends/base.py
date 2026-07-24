"""Stable contracts shared by pluggable tabular AutoML backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..ml_engine import MLEngineError, Source, TabularAutoMLResult, TaskType


class BackendError(MLEngineError):
    """Base class for backend selection and execution errors."""

    code = "BACKEND_ERROR"


class BackendNotFoundError(BackendError):
    code = "BACKEND_NOT_FOUND"


class BackendUnavailableError(BackendError):
    code = "BACKEND_UNAVAILABLE"


class BackendTaskUnsupportedError(BackendError):
    code = "BACKEND_TASK_UNSUPPORTED"


class BackendMediaTypeUnsupportedError(BackendError):
    code = "BACKEND_MEDIA_TYPE_UNSUPPORTED"


class BackendRegistrationError(BackendError):
    code = "BACKEND_REGISTRATION_CONFLICT"


@dataclass(frozen=True)
class BackendCapabilities:
    """Machine-readable execution boundaries for one backend."""

    task_types: tuple[TaskType, ...]
    media_types: tuple[str, ...] = (
        "text/csv",
        "application/vnd.apache.parquet",
    )
    supports_categorical_features: bool = True
    supports_missing_values: bool = True
    supports_probability_predictions: bool = True
    supports_cross_validation: bool = True
    supports_sealed_holdout: bool = True
    supports_cpu: bool = True
    supports_gpu: bool = False
    limits: dict[str, int] = field(default_factory=dict)
    runtime_requirements: tuple[str, ...] = ()
    required_attributions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_types": list(self.task_types),
            "media_types": list(self.media_types),
            "supports_categorical_features": self.supports_categorical_features,
            "supports_missing_values": self.supports_missing_values,
            "supports_probability_predictions": self.supports_probability_predictions,
            "supports_cross_validation": self.supports_cross_validation,
            "supports_sealed_holdout": self.supports_sealed_holdout,
            "supports_cpu": self.supports_cpu,
            "supports_gpu": self.supports_gpu,
            "limits": dict(self.limits),
            "runtime_requirements": list(self.runtime_requirements),
            "required_attributions": list(self.required_attributions),
        }


@dataclass(frozen=True)
class BackendDescriptor:
    """Runtime status and artifact contract exposed to API and Agent clients."""

    backend_id: str
    display_name: str
    engine_version: str
    backend_version: str | None
    available: bool
    capabilities: BackendCapabilities
    artifact_kind: str
    artifact_media_type: str
    artifact_serialization: str
    deterministic: bool
    installed: bool = True
    optional_dependency: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.backend_id or self.backend_id.strip().lower() != self.backend_id:
            raise ValueError("backend_id must be a non-empty lowercase identifier")
        if self.available and self.unavailable_reason is not None:
            raise ValueError("an available backend cannot have unavailable_reason")
        if self.available and not self.installed:
            raise ValueError("an available backend must be installed")
        if not self.available and not self.unavailable_reason:
            raise ValueError("an unavailable backend must explain unavailable_reason")

    @property
    def status(self) -> str:
        return "AVAILABLE" if self.available else "UNAVAILABLE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "display_name": self.display_name,
            "engine_version": self.engine_version,
            "backend_version": self.backend_version,
            "status": self.status,
            "installed": self.installed,
            "available": self.available,
            "optional_dependency": self.optional_dependency,
            "unavailable_reason": self.unavailable_reason,
            "capabilities": self.capabilities.as_dict(),
            "artifact": {
                "kind": self.artifact_kind,
                "media_type": self.artifact_media_type,
                "serialization": self.artifact_serialization,
            },
            "deterministic": self.deterministic,
            "production_eligible": False,
        }


@runtime_checkable
class TabularBackend(Protocol):
    """Contract implemented by every in-process tabular backend adapter."""

    @property
    def descriptor(self) -> BackendDescriptor: ...

    def run(
        self,
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
    ) -> TabularAutoMLResult: ...


__all__ = [
    "BackendCapabilities",
    "BackendDescriptor",
    "BackendError",
    "BackendMediaTypeUnsupportedError",
    "BackendNotFoundError",
    "BackendRegistrationError",
    "BackendTaskUnsupportedError",
    "BackendUnavailableError",
    "TabularBackend",
]
