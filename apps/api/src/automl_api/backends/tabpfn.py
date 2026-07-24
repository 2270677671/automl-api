"""Lazy local TabPFN adapter with explicit model-access and license gates."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.preprocessing import OrdinalEncoder

from ..ml_engine import (
    ModelTrainingError,
    Source,
    TabularAutoMLResult,
    TaskType,
    _holdout_metrics,
    prepare_tabular_data,
)
from ._execution import assemble_native_result
from .base import BackendCapabilities, BackendDescriptor, BackendUnavailableError


ENGINE_VERSION = "tabular-tabpfn.v1"
_DISTRIBUTION = "tabpfn"
_CPU_MAX_ROWS = 1_000
_GPU_MAX_ROWS = 100_000
_MAX_FEATURES = 2_000
_MODEL_SOURCES = {"auto", "public-v2"}
_PUBLIC_V2_FILENAMES: dict[TaskType, str] = {
    "BINARY_CLASSIFICATION": "tabpfn-v2-classifier.ckpt",
    "REGRESSION": "tabpfn-v2-regressor.ckpt",
}


def _installed_version() -> str | None:
    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _model_source() -> str:
    return os.environ.get("AUTOML_TABPFN_MODEL_SOURCE", "auto").strip().lower() or "auto"


def _public_v2_paths() -> dict[TaskType, Path]:
    cache = Path(
        os.environ.get("TABPFN_MODEL_CACHE_DIR", "~/.cache/tabpfn").strip() or "~/.cache/tabpfn"
    ).expanduser()
    return {task_type: cache / filename for task_type, filename in _PUBLIC_V2_FILENAMES.items()}


def _model_access_status(source: str) -> tuple[bool, bool]:
    """Return whether model access is ready and whether missing local paths caused failure."""
    if source == "public-v2":
        return all(path.is_file() for path in _public_v2_paths().values()), True

    specific = {
        "BINARY_CLASSIFICATION": os.environ.get("AUTOML_TABPFN_CLASSIFIER_MODEL_PATH", "").strip(),
        "REGRESSION": os.environ.get("AUTOML_TABPFN_REGRESSOR_MODEL_PATH", "").strip(),
    }
    if any(specific.values()):
        ready = all(value and Path(value).expanduser().is_file() for value in specific.values())
        return ready, True

    configured = os.environ.get("AUTOML_TABPFN_MODEL_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().is_file(), True

    return bool(os.environ.get("TABPFN_TOKEN", "").strip()), False


def _model_path_for_task(task_type: TaskType, source: str) -> str:
    if source == "public-v2":
        return str(_public_v2_paths()[task_type])
    variable = (
        "AUTOML_TABPFN_CLASSIFIER_MODEL_PATH"
        if task_type == "BINARY_CLASSIFICATION"
        else "AUTOML_TABPFN_REGRESSOR_MODEL_PATH"
    )
    configured = os.environ.get(variable, "").strip()
    if not configured:
        configured = os.environ.get("AUTOML_TABPFN_MODEL_PATH", "").strip()
    return str(Path(configured).expanduser()) if configured else "auto"


class _TabPFNPreprocessor:
    """Fold-fitted ordinal/missing transform without scaling or one-hot expansion."""

    def __init__(self, numeric_columns: list[str], categorical_columns: list[str]) -> None:
        self.numeric_columns = list(numeric_columns)
        self.categorical_columns = list(categorical_columns)
        self.numeric_medians: dict[str, float] = {}
        self.encoder = OrdinalEncoder(
            handle_unknown="use_encoded_value",
            unknown_value=-1,
            encoded_missing_value=-2,
            dtype=np.float32,
        )

    @property
    def categorical_indices(self) -> list[int]:
        start = len(self.numeric_columns)
        return list(range(start, start + len(self.categorical_columns)))

    def fit(self, features: pd.DataFrame) -> _TabPFNPreprocessor:
        self.numeric_medians = {
            column: float(features[column].median()) for column in self.numeric_columns
        }
        if self.categorical_columns:
            self.encoder.fit(self._categorical_frame(features))
        return self

    def transform(self, features: pd.DataFrame) -> np.ndarray:
        blocks: list[np.ndarray] = []
        if self.numeric_columns:
            numeric = features[self.numeric_columns].copy(deep=True)
            for column, median in self.numeric_medians.items():
                numeric[column] = numeric[column].fillna(median)
            blocks.append(numeric.to_numpy(dtype=np.float32))
        if self.categorical_columns:
            blocks.append(self.encoder.transform(self._categorical_frame(features)))
        if not blocks:
            return np.empty((len(features), 0), dtype=np.float32)
        return np.column_stack(blocks).astype(np.float32, copy=False)

    def fit_transform(self, features: pd.DataFrame) -> np.ndarray:
        return self.fit(features).transform(features)

    def _categorical_frame(self, features: pd.DataFrame) -> pd.DataFrame:
        categorical = features[self.categorical_columns].copy(deep=True)
        for column in self.categorical_columns:
            categorical[column] = categorical[column].fillna("__AUTOML_MISSING__").astype(str)
        return categorical


class _TabPFNModel:
    def __init__(
        self,
        estimator: Any,
        preprocessor: _TabPFNPreprocessor,
        task_type: TaskType,
    ) -> None:
        self.estimator = estimator
        self.preprocessor = preprocessor
        self.task_type = task_type
        self.classes_ = np.asarray([0, 1]) if task_type == "BINARY_CLASSIFICATION" else None

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.estimator.predict(self.preprocessor.transform(features)))

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        if self.task_type != "BINARY_CLASSIFICATION":
            raise AttributeError("predict_proba is only available for classification")
        values = np.asarray(
            self.estimator.predict_proba(self.preprocessor.transform(features)),
            dtype="float64",
        )
        totals = values.sum(axis=1, keepdims=True)
        return np.divide(values, totals, out=np.zeros_like(values), where=totals != 0)


def _serialize_evaluation_metadata(
    *,
    backend_version: str,
    task_type: TaskType,
    config: dict[str, Any],
) -> bytes:
    return json.dumps(
        {
            "schema_version": "tabpfn-evaluation-metadata.v1",
            "backend_id": "tabpfn",
            "backend_version": backend_version,
            "engine_version": ENGINE_VERSION,
            "task_type": task_type,
            "family": "TABPFN_FOUNDATION_MODEL",
            "config": config,
            "exportable": False,
            "contains_model_state": False,
            "contains_training_data": False,
            "contains_category_vocabulary": False,
            "model_weights_included": False,
            "reason": "TABPFN_FIT_STATE_CONTAINS_DEVELOPMENT_DATA",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class BackendWallTimeExceededError(ModelTrainingError):
    code = "BACKEND_WALL_TIME_EXCEEDED"


def _check_deadline(deadline: float | None, *, stage: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise BackendWallTimeExceededError(
            "The TabPFN backend exhausted its wall-time budget between execution steps.",
            backend_id="tabpfn",
            stage=stage,
        )


def _development_metrics(
    *,
    estimator_class: Any,
    prepared: Any,
    model_path: str,
    device: str,
    n_estimators: int,
    deadline: float | None,
) -> dict[str, dict[str, float]]:
    values: dict[str, list[float]] = {}
    for fold, (train, validation) in enumerate(prepared.cv_splits):
        _check_deadline(deadline, stage=f"before_fold_{fold}")
        train_features = prepared.development_features.iloc[train]
        validation_features = prepared.development_features.iloc[validation]
        train_target = prepared.development_target.iloc[train]
        validation_target = prepared.development_target.iloc[validation]
        preprocessor = _TabPFNPreprocessor(
            prepared.numeric_columns,
            prepared.categorical_columns,
        )
        transformed = preprocessor.fit_transform(train_features)
        estimator = estimator_class(
            n_estimators=n_estimators,
            auto_scale_n_estimators=False,
            categorical_features_indices=preprocessor.categorical_indices,
            model_path=model_path,
            device=device,
            fit_mode="low_memory",
            keep_cache_on_device=False,
            random_state=prepared.seed + fold,
            n_preprocessing_jobs=1,
            show_progress_bar=False,
        )
        estimator.fit(transformed, train_target.to_numpy())
        metrics = _holdout_metrics(
            _TabPFNModel(estimator, preprocessor, prepared.task_type),
            validation_features,
            validation_target,
            prepared.task_type,
        )
        for name, value in metrics.items():
            values.setdefault(name, []).append(float(value))
        _check_deadline(deadline, stage=f"after_fold_{fold}")
    return {
        name: {
            "mean": float(np.asarray(items).mean()),
            "std": float(np.asarray(items).std(ddof=0)),
        }
        for name, items in values.items()
    }


class TabPFNBackend:
    """Small-data TabPFN adapter; model access and licensing stay operator-owned."""

    @property
    def descriptor(self) -> BackendDescriptor:
        version = _installed_version()
        importable = importlib.util.find_spec("tabpfn") is not None
        installed = version is not None and importable
        license_accepted = _enabled(os.environ.get("AUTOML_TABPFN_LICENSE_ACCEPTED"))
        model_source = _model_source()
        model_access, local_paths_expected = _model_access_status(model_source)
        available = installed and license_accepted and model_access
        if not installed:
            unavailable_reason = "STANDARD_DEPENDENCY_NOT_INSTALLED"
        elif not license_accepted:
            unavailable_reason = "MODEL_LICENSE_NOT_ACCEPTED"
        elif model_source not in _MODEL_SOURCES:
            unavailable_reason = "MODEL_SOURCE_INVALID"
        elif local_paths_expected and not model_access:
            unavailable_reason = "MODEL_PATH_NOT_FOUND"
        elif not model_access:
            unavailable_reason = "MODEL_ACCESS_NOT_CONFIGURED"
        else:
            unavailable_reason = None
        return BackendDescriptor(
            backend_id="tabpfn",
            display_name="TabPFN local small-data backend",
            engine_version=ENGINE_VERSION,
            backend_version=version,
            available=available,
            capabilities=BackendCapabilities(
                task_types=("BINARY_CLASSIFICATION", "REGRESSION"),
                supports_gpu=True,
                limits={
                    "max_cpu_rows": _CPU_MAX_ROWS,
                    "max_gpu_rows": _GPU_MAX_ROWS,
                    "max_features": _MAX_FEATURES,
                },
                runtime_requirements=(
                    "Operator acceptance of the selected TabPFN model-weight license",
                    "public-v2 cache, TABPFN_TOKEN, or configured checkpoint paths",
                    "Writable model cache; only data-free evaluation metadata is downloadable",
                    "GPU recommended; CPU execution is limited to small datasets",
                    "Wall-time deadline is cooperative between folds, not a hard fit interruption",
                ),
            ),
            artifact_kind="TABPFN_EVALUATION_METADATA",
            artifact_media_type="application/json",
            artifact_serialization="json",
            deterministic=False,
            installed=installed,
            optional_dependency=None,
            unavailable_reason=unavailable_reason,
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
        if max_wall_time_seconds is not None and max_wall_time_seconds < 1:
            raise ValueError("max_wall_time_seconds must be positive")
        deadline = (
            time.monotonic() + max_wall_time_seconds if max_wall_time_seconds is not None else None
        )
        descriptor = self.descriptor
        if not descriptor.available or descriptor.backend_version is None:
            raise BackendUnavailableError(
                "TabPFN is not ready in this runtime.",
                backend_id="tabpfn",
                installed=descriptor.installed,
                optional_dependency=descriptor.optional_dependency,
                reason=descriptor.unavailable_reason,
            )
        try:
            from tabpfn import TabPFNClassifier, TabPFNRegressor
        except Exception as error:
            raise BackendUnavailableError(
                "TabPFN could not be imported in this runtime.",
                backend_id="tabpfn",
                error_type=type(error).__name__,
            ) from error

        prepared = prepare_tabular_data(
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
            engine_version=ENGINE_VERSION,
        )
        _check_deadline(deadline, stage="after_data_preparation")
        model_source = _model_source()
        model_path = _model_path_for_task(prepared.task_type, model_source)
        device = os.environ.get("AUTOML_TABPFN_DEVICE", "cpu").strip() or "cpu"
        max_rows = _CPU_MAX_ROWS if device == "cpu" else _GPU_MAX_ROWS
        if len(prepared.features) > max_rows:
            raise ModelTrainingError(
                "The dataset exceeds the configured TabPFN row limit.",
                backend_id="tabpfn",
                row_count=len(prepared.features),
                max_rows=max_rows,
                device=device,
            )
        if prepared.features.shape[1] > _MAX_FEATURES:
            raise ModelTrainingError(
                "The dataset exceeds the configured TabPFN feature limit.",
                backend_id="tabpfn",
                feature_count=prepared.features.shape[1],
                max_features=_MAX_FEATURES,
            )
        n_estimators = 4
        estimator_class = (
            TabPFNClassifier if prepared.task_type == "BINARY_CLASSIFICATION" else TabPFNRegressor
        )
        config = {
            "checkpoint": model_source
            if model_source == "public-v2"
            else ("configured" if model_path != "auto" else "auto"),
            "device": device,
            "n_estimators": n_estimators,
            "fit_mode": "low_memory",
            "preprocessing": "median_plus_ordinal_no_scaling",
            "artifact_exportable": False,
        }
        try:
            development_metrics = _development_metrics(
                estimator_class=estimator_class,
                prepared=prepared,
                model_path=model_path,
                device=device,
                n_estimators=n_estimators,
                deadline=deadline,
            )
            _check_deadline(deadline, stage="before_final_fit")
            preprocessor = _TabPFNPreprocessor(
                prepared.numeric_columns,
                prepared.categorical_columns,
            )
            transformed = preprocessor.fit_transform(prepared.development_features)
            estimator = estimator_class(
                n_estimators=n_estimators,
                auto_scale_n_estimators=False,
                categorical_features_indices=preprocessor.categorical_indices,
                model_path=model_path,
                device=device,
                fit_mode="low_memory",
                keep_cache_on_device=False,
                random_state=seed,
                n_preprocessing_jobs=1,
                show_progress_bar=False,
            )
            estimator.fit(transformed, prepared.development_target.to_numpy())
            _check_deadline(deadline, stage="after_final_fit")
            model = _TabPFNModel(estimator, preprocessor, prepared.task_type)
            model_bytes = _serialize_evaluation_metadata(
                backend_version=descriptor.backend_version,
                task_type=prepared.task_type,
                config=config,
            )
            return assemble_native_result(
                prepared,
                backend_id="tabpfn",
                engine_version=ENGINE_VERSION,
                backend_version=descriptor.backend_version,
                family="TABPFN_FOUNDATION_MODEL",
                config=config,
                development_metrics=development_metrics,
                fitted_candidate=model,
                model_bytes=model_bytes,
                artifact_kind=descriptor.artifact_kind,
                artifact_media_type=descriptor.artifact_media_type,
                serialization=descriptor.artifact_serialization,
                library_versions={
                    "pandas": pd.__version__,
                    "numpy": np.__version__,
                },
                limitations=[
                    "TabPFN model-weight access and license compliance are operator responsibilities.",
                    "No fitted TabPFN state is exported because its native fit state contains development data.",
                    "CPU execution is intended only for datasets with at most 1000 rows; GPU is recommended.",
                    "Wall-time checks occur between folds and fits; one in-process forward pass cannot be interrupted.",
                    "No production eligibility or deployment approval was evaluated.",
                ],
                exportable=False,
            )
        except (BackendUnavailableError, ModelTrainingError):
            raise
        except Exception as error:
            raise ModelTrainingError(
                "TabPFN could not train a bounded small-data candidate.",
                backend_id="tabpfn",
                error_type=type(error).__name__,
            ) from error


__all__ = ["TabPFNBackend"]
