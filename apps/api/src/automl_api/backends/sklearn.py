"""Adapter for the built-in deterministic scikit-learn engine."""

from __future__ import annotations

from typing import Any

import sklearn

from ..ml_engine import (
    ENGINE_VERSION,
    Source,
    TabularAutoMLResult,
    TaskType,
    run_tabular_automl,
)
from .base import BackendCapabilities, BackendDescriptor


class SklearnBackend:
    """Preserve the original bounded sklearn behavior behind the backend contract."""

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_id="sklearn",
            display_name="scikit-learn bounded search",
            engine_version=ENGINE_VERSION,
            backend_version=sklearn.__version__,
            available=True,
            capabilities=BackendCapabilities(
                task_types=("BINARY_CLASSIFICATION", "REGRESSION"),
            ),
            artifact_kind="SKLEARN_JOBLIB",
            artifact_media_type="application/octet-stream",
            artifact_serialization="joblib",
            deterministic=True,
        )

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
    ) -> TabularAutoMLResult:
        del max_wall_time_seconds
        return run_tabular_automl(
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
        )


__all__ = ["SklearnBackend"]
