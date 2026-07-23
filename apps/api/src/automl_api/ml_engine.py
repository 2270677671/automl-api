"""Deterministic, leakage-conscious tabular AutoML execution slice.

This module is intentionally independent from the HTTP workflow. It accepts one
CSV or Parquet table, evaluates a small allowlist of scikit-learn pipelines, and
returns protocol-ready structured records plus serialized artifacts. The final
holdout is sealed away from model selection and can be opened exactly once.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
from pandas.api.types import (
    is_bool_dtype,
    is_datetime64_any_dtype,
    is_float_dtype,
    is_integer_dtype,
    is_numeric_dtype,
    is_object_dtype,
    is_string_dtype,
)
from sklearn.base import BaseEstimator, clone
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    log_loss,
    mean_absolute_error,
    r2_score,
    root_mean_squared_error,
    roc_auc_score,
)
from sklearn.model_selection import (
    GroupKFold,
    StratifiedGroupKFold,
    cross_validate,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from threadpoolctl import threadpool_limits


TaskType = Literal["BINARY_CLASSIFICATION", "REGRESSION"]
Source = bytes | bytearray | memoryview | str | Path

ENGINE_VERSION = "tabular-sklearn.v1"
_CSV_MEDIA_TYPES = {"csv", "text/csv"}
_PARQUET_MEDIA_TYPES = {"parquet", "application/vnd.apache.parquet"}


class MLEngineError(ValueError):
    """Base error with a stable code suitable for a FailureReport."""

    code = "ML_ENGINE_ERROR"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "context": _jsonify(self.context)}


class DatasetParseError(MLEngineError):
    code = "DATASET_PARSE_FAILED"


class UnsupportedMediaTypeError(MLEngineError):
    code = "UNSUPPORTED_DATASET_MEDIA_TYPE"


class InvalidDatasetError(MLEngineError):
    code = "INVALID_TABULAR_DATASET"


class MissingTargetError(MLEngineError):
    code = "TARGET_REQUIRED"


class AllNullTargetError(MLEngineError):
    code = "TARGET_ALL_NULL"


class TargetContainsMissingError(MLEngineError):
    code = "TARGET_CONTAINS_MISSING_VALUES"


class ConstantTargetError(MLEngineError):
    code = "TARGET_CONSTANT"


class MulticlassTargetError(MLEngineError):
    code = "UNSUPPORTED_MULTICLASS_TARGET"


class InvalidTargetError(MLEngineError):
    code = "INVALID_TARGET"


class PositiveClassRequiredError(MLEngineError):
    code = "POSITIVE_CLASS_REQUIRED"


class NoUsableFeaturesError(MLEngineError):
    code = "NO_USABLE_FEATURES"


class ConflictingDuplicateLabelsError(MLEngineError):
    code = "CONFLICTING_DUPLICATE_LABELS"


class TargetLeakageError(MLEngineError):
    code = "DIRECT_TARGET_LEAKAGE"


class IIDAssumptionRequiredError(MLEngineError):
    code = "IID_ASSUMPTION_REQUIRED"


class InsufficientDataError(MLEngineError):
    code = "INSUFFICIENT_DATA_FOR_SPLIT"


class ModelTrainingError(MLEngineError):
    code = "MODEL_TRAINING_FAILED"


@dataclass(frozen=True)
class TabularAutoMLResult:
    """Structured engine result and immutable artifact bytes."""

    profile: dict[str, Any]
    task: dict[str, Any]
    split: dict[str, Any]
    baseline: dict[str, Any]
    trials: list[dict[str, Any]]
    evaluation: dict[str, Any]
    model_metadata: dict[str, Any]
    model_bytes: bytes
    report_bytes: bytes

    def structured(self) -> dict[str, Any]:
        """Return JSON-compatible records without embedding artifact bytes."""

        return {
            "profile": self.profile,
            "task": self.task,
            "split": self.split,
            "baseline": self.baseline,
            "trials": self.trials,
            "evaluation": self.evaluation,
            "model_metadata": self.model_metadata,
        }


@dataclass(frozen=True)
class PreparedTabularData:
    """Validated data partitions shared by pluggable tabular backends.

    The sealed holdout can be opened exactly once. Backends may use the development
    folds for model selection, but must not inspect the holdout until their final
    candidate has been frozen.
    """

    dataset_sha256: str
    source_name: str
    media_type: str
    target_column: str
    task_type: TaskType
    seed: int
    features: pd.DataFrame
    development_features: pd.DataFrame
    development_target: pd.Series
    development_groups: np.ndarray
    sealed_holdout: _SealedHoldout
    cv_splits: list[tuple[np.ndarray, np.ndarray]]
    numeric_columns: list[str]
    categorical_columns: list[str]
    profile: dict[str, Any]
    task: dict[str, Any]
    split: dict[str, Any]


@dataclass(frozen=True)
class _CandidateSpec:
    family: str
    estimator: BaseEstimator
    config: dict[str, Any]


class _SealedHoldout:
    """One-shot holder kept outside every model-selection function."""

    def __init__(self, features: pd.DataFrame, target: pd.Series) -> None:
        self._features = features
        self._target = target
        self._opened = False
        self._open_count = 0

    @property
    def open_count(self) -> int:
        return self._open_count

    def open_once(self) -> tuple[pd.DataFrame, pd.Series]:
        if self._opened:
            raise RuntimeError("sealed holdout can only be opened once")
        self._opened = True
        self._open_count += 1
        return self._features.copy(deep=True), self._target.copy(deep=True)


def inspect_tabular_dataset(source: Source, media_type: str | None = None) -> dict[str, Any]:
    """Return a target-safe structural profile without exposing cell values."""

    raw_bytes, resolved_media_type, source_name = _read_source_bytes(source, media_type)
    frame = _parse_table(raw_bytes, resolved_media_type)
    unique_count_cap = 100
    columns: list[dict[str, Any]] = []
    for column in frame.columns:
        series = frame[column]
        try:
            observed_unique_count = int(series.nunique(dropna=True))
        except (TypeError, ValueError):
            observed_unique_count = unique_count_cap + 1
        columns.append(
            {
                "name": str(column),
                "physical_dtype": str(series.dtype),
                "semantic_type": _semantic_type(series),
                "non_null_count": int(series.notna().sum()),
                "missing_count": int(series.isna().sum()),
                "unique_count": min(observed_unique_count, unique_count_cap),
                "unique_count_capped": observed_unique_count > unique_count_cap,
            }
        )
    return {
        "kind": "PRE_SPLIT_PROFILE",
        "engine_version": ENGINE_VERSION,
        "dataset_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "source_name": source_name,
        "media_type": resolved_media_type,
        "row_count": int(len(frame)),
        "column_count": int(frame.shape[1]),
        "unique_count_cap": unique_count_cap,
        "columns": columns,
    }


def prepare_tabular_data(
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
    engine_version: str = ENGINE_VERSION,
) -> PreparedTabularData:
    """Validate and freeze the data partitions used by every tabular backend.

    The caller must explicitly confirm that rows are independent and identically
    distributed. The returned holdout is one-shot and remains unavailable to model
    selection code until the backend explicitly opens it.
    """

    if not iid_confirmed:
        raise IIDAssumptionRequiredError(
            "Confirm row independence or provide a group/time split before training."
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if not 0.1 <= test_size <= 0.4:
        raise ValueError("test_size must be between 0.1 and 0.4")
    if isinstance(cv_folds, bool) or not 2 <= cv_folds <= 10:
        raise ValueError("cv_folds must be between 2 and 10")
    if max_categories < 2:
        raise ValueError("max_categories must be at least 2")
    if max_trials is not None and (
        isinstance(max_trials, bool) or not isinstance(max_trials, int) or max_trials < 1
    ):
        raise ValueError("max_trials must be a positive integer")

    raw_bytes, resolved_media_type, source_name = _read_source_bytes(source, media_type)
    frame = _parse_table(raw_bytes, resolved_media_type)
    dataset_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    return _prepare_frame(
        frame,
        dataset_sha256=dataset_sha256,
        source_name=source_name,
        media_type=resolved_media_type,
        target_column=target_column,
        requested_task_type=task_type,
        positive_class=positive_class,
        requested_primary_metric=primary_metric,
        seed=seed,
        test_size=test_size,
        cv_folds=cv_folds,
        max_categories=max_categories,
        engine_version=engine_version,
    )


def run_tabular_automl(
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
) -> TabularAutoMLResult:
    """Run the bounded scikit-learn tabular engine."""

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
    )

    with threadpool_limits(limits=1):
        return _execute(
            prepared,
            max_trials=max_trials,
        )


def _prepare_frame(
    frame: pd.DataFrame,
    *,
    dataset_sha256: str,
    source_name: str,
    media_type: str,
    target_column: str | None,
    requested_task_type: TaskType | str | None,
    positive_class: Any | None,
    requested_primary_metric: str | None,
    seed: int,
    test_size: float,
    cv_folds: int,
    max_categories: int,
    engine_version: str,
) -> PreparedTabularData:
    if not target_column or not target_column.strip():
        raise MissingTargetError("A target column is required before training.")
    if target_column not in frame.columns:
        raise MissingTargetError(
            "The requested target column does not exist.", target_column=target_column
        )

    target = frame[target_column]
    if target.isna().all():
        raise AllNullTargetError("The target column contains no observed values.")
    missing_target_count = int(target.isna().sum())
    if missing_target_count:
        raise TargetContainsMissingError(
            "Rows with a missing target must be remediated explicitly.",
            missing_rows=missing_target_count,
        )
    unique_target_count = int(target.nunique(dropna=False))
    if unique_target_count < 2:
        raise ConstantTargetError("The target must contain at least two distinct values.")

    resolved_task = _resolve_task_type(target, requested_task_type)
    encoded_target, task_record = _prepare_target(
        target,
        task_type=resolved_task,
        positive_class=positive_class,
        primary_metric=requested_primary_metric,
    )
    task_record.update(
        {
            "target_column_id": target_column,
            "split_strategy": "STRATIFIED_HOLDOUT"
            if resolved_task == "BINARY_CLASSIFICATION"
            else "RANDOM_HOLDOUT",
            "confidence": 1.0,
            "assumptions": [
                "The caller confirmed that rows are i.i.d.",
                "Exact duplicate feature rows remain in one partition.",
            ],
            "guardrail_metrics": ["roc_auc", "log_loss", "accuracy"]
            if resolved_task == "BINARY_CLASSIFICATION"
            else ["mae", "r2"],
            "confirmed_by": "USER",
        }
    )

    raw_features = frame.drop(columns=[target_column])
    if raw_features.shape[1] == 0:
        raise NoUsableFeaturesError("The table contains only the target column.")
    leaking_columns = [
        str(column)
        for column in raw_features.columns
        if _series_values_equal(raw_features[column], target)
    ]
    if leaking_columns:
        raise TargetLeakageError(
            "A feature is an exact copy of the target.", columns=leaking_columns
        )

    features, numeric_columns, categorical_columns, excluded_columns, infinite_values = (
        _prepare_features(raw_features, max_categories=max_categories)
    )
    if not numeric_columns and not categorical_columns:
        raise NoUsableFeaturesError(
            "No non-constant numeric or bounded-cardinality categorical features remain.",
            excluded_columns=excluded_columns,
        )

    groups = _row_fingerprints(features)
    _validate_duplicate_labels(groups, encoded_target)
    duplicate_group_count = int(pd.Series(groups).value_counts().gt(1).sum())

    quality_issues = [
        {
            "code": f"FEATURE_EXCLUDED_{item['reason']}",
            "severity": "WARNING",
            "message": f"Feature {item['column']} was excluded: {item['reason']}.",
            "remediation": None,
            "evidence_refs": [],
        }
        for item in excluded_columns
    ]
    if infinite_values:
        quality_issues.append(
            {
                "code": "INFINITE_VALUES_NORMALIZED",
                "severity": "WARNING",
                "message": "Infinite numeric values were treated as missing before fold fitting.",
                "remediation": "Inspect the source data for invalid numeric values.",
                "evidence_refs": [],
            }
        )
    profile = {
        "kind": "DATA_QUALITY_REPORT",
        "engine_version": engine_version,
        "dataset_sha256": dataset_sha256,
        "source_name": source_name,
        "media_type": media_type,
        "row_count": int(len(frame)),
        "column_count": int(frame.shape[1]),
        "target_column": target_column,
        "target_missing_count": missing_target_count,
        "numeric_feature_columns": numeric_columns,
        "categorical_feature_columns": categorical_columns,
        "excluded_columns": excluded_columns,
        "infinite_values_replaced_with_missing": infinite_values,
        "exact_duplicate_feature_groups": duplicate_group_count,
        "quality_score": float(max(0, 100 - min(80, 5 * len(quality_issues)))),
        "issues": quality_issues,
    }

    (
        development_features,
        development_target,
        development_groups,
        sealed_holdout,
        cv_splits,
        split_record,
    ) = _freeze_split(
        features,
        encoded_target,
        groups,
        task_type=resolved_task,
        dataset_sha256=dataset_sha256,
        seed=seed,
        test_size=test_size,
        cv_folds=cv_folds,
    )

    return PreparedTabularData(
        dataset_sha256=dataset_sha256,
        source_name=source_name,
        media_type=media_type,
        target_column=target_column,
        task_type=resolved_task,
        seed=seed,
        features=features,
        development_features=development_features,
        development_target=development_target,
        development_groups=development_groups,
        sealed_holdout=sealed_holdout,
        cv_splits=cv_splits,
        numeric_columns=numeric_columns,
        categorical_columns=categorical_columns,
        profile=profile,
        task=task_record,
        split=split_record,
    )


def _execute(
    prepared: PreparedTabularData,
    *,
    max_trials: int | None,
) -> TabularAutoMLResult:
    dataset_sha256 = prepared.dataset_sha256
    target_column = prepared.target_column
    seed = prepared.seed
    resolved_task = prepared.task_type
    profile = prepared.profile
    task_record = prepared.task
    split_record = prepared.split
    numeric_columns = prepared.numeric_columns
    categorical_columns = prepared.categorical_columns
    development_features = prepared.development_features
    development_target = prepared.development_target
    cv_splits = prepared.cv_splits
    sealed_holdout = prepared.sealed_holdout

    preprocessor = _build_preprocessor(numeric_columns, categorical_columns)
    baseline_spec, candidate_specs = _model_specs(resolved_task, seed)
    if max_trials is not None:
        candidate_specs = candidate_specs[:max_trials]
    scoring = _scoring(resolved_task)
    baseline_pipeline = _pipeline(preprocessor, baseline_spec.estimator)
    try:
        baseline_metrics = _cross_validated_metrics(
            baseline_pipeline,
            development_features,
            development_target,
            cv_splits,
            scoring=scoring,
            task_type=resolved_task,
        )
    except Exception as error:
        raise ModelTrainingError(
            "The deterministic baseline could not be evaluated.",
            family=baseline_spec.family,
            error_type=type(error).__name__,
        ) from error

    baseline = {
        "kind": "BASELINE_RESULT",
        "family": baseline_spec.family,
        "config": baseline_spec.config,
        "primary_metric": task_record["primary_metric"],
        "cv_metrics": baseline_metrics,
        "baselines": [
            {
                "name": baseline_spec.family,
                "metrics": _metric_value_list(baseline_metrics, from_cv=True),
                "compute_credits": 0.0,
            }
        ],
    }

    trials: list[dict[str, Any]] = []
    successful: list[tuple[_CandidateSpec, Pipeline, dict[str, Any], float]] = []
    for trial_number, spec in enumerate(candidate_specs):
        config_hash = _stable_hash({"family": spec.family, "config": spec.config, "seed": seed})
        candidate_pipeline = _pipeline(preprocessor, spec.estimator)
        try:
            metrics = _cross_validated_metrics(
                candidate_pipeline,
                development_features,
                development_target,
                cv_splits,
                scoring=scoring,
                task_type=resolved_task,
            )
            primary_score = float(metrics[task_record["primary_metric"]]["mean"])
            trial = {
                "kind": "TRIAL_RESULT",
                "experiment_id": f"exp_{config_hash[:16]}",
                "trial_number": trial_number,
                "status": "SUCCEEDED",
                "family": spec.family,
                "model_family": spec.family,
                "config": spec.config,
                "normalized_config": spec.config,
                "config_hash": config_hash,
                "cv_metrics": metrics,
                "metrics": _metric_value_list(metrics, from_cv=True),
                "primary_metric": task_record["primary_metric"],
                "primary_score": primary_score,
                "compute_credits": 0.0,
                "failure_code": None,
            }
            successful.append((spec, candidate_pipeline, metrics, primary_score))
        except Exception as error:
            trial = {
                "kind": "TRIAL_RESULT",
                "experiment_id": f"exp_{config_hash[:16]}",
                "trial_number": trial_number,
                "status": "FAILED",
                "family": spec.family,
                "model_family": spec.family,
                "config": spec.config,
                "normalized_config": spec.config,
                "config_hash": config_hash,
                "metrics": [],
                "compute_credits": 0.0,
                "failure_code": "CANDIDATE_EVALUATION_FAILED",
                "error_type": type(error).__name__,
            }
        trials.append(trial)

    if not successful:
        raise ModelTrainingError(
            "Every candidate failed during development-only cross-validation.",
            failed_families=[spec.family for spec in candidate_specs],
        )

    primary_metric_name = task_record["primary_metric"]
    maximize = _metric_is_maximized(primary_metric_name)
    selected_spec, selected_pipeline, selected_cv_metrics, selected_score = (
        max(successful, key=lambda item: item[3])
        if maximize
        else min(successful, key=lambda item: item[3])
    )

    fitted_baseline = clone(baseline_pipeline).fit(development_features, development_target)
    fitted_candidate = clone(selected_pipeline).fit(development_features, development_target)
    holdout_features, holdout_target = sealed_holdout.open_once()
    baseline_holdout_metrics = _holdout_metrics(
        fitted_baseline, holdout_features, holdout_target, resolved_task
    )
    candidate_holdout_metrics = _holdout_metrics(
        fitted_candidate, holdout_features, holdout_target, resolved_task
    )
    baseline_primary = float(baseline_holdout_metrics[primary_metric_name])
    candidate_primary = float(candidate_holdout_metrics[primary_metric_name])
    improvement = (
        candidate_primary - baseline_primary if maximize else baseline_primary - candidate_primary
    )
    evaluation = {
        "kind": "EVALUATION_REPORT",
        "primary_metric": primary_metric_name,
        "baseline": _metric_value(primary_metric_name, baseline_primary),
        "candidate": _metric_value(primary_metric_name, candidate_primary),
        "paired_delta": {
            "name": f"{primary_metric_name}_paired_improvement",
            "value": float(improvement),
            "direction": "MAXIMIZE",
            "confidence_interval": None,
        },
        "paired_improvement": float(improvement),
        "all_metrics": {
            "baseline": baseline_holdout_metrics,
            "candidate": candidate_holdout_metrics,
        },
        "selected_family": selected_spec.family,
        "selected_cv_score": selected_score,
        "holdout_evaluations": sealed_holdout.open_count,
        "evaluated_candidate": True,
        "production_eligible": False,
        "guardrails_passed": True,
        "eligible_candidate": False,
        "failed_gates": ["PRODUCTION_ELIGIBILITY_NOT_EVALUATED"],
        "eligibility_reason": "EVALUATION_ONLY_NO_PRODUCTION_GATE",
        "limitations": [
            "This slice assumes one i.i.d. table.",
            "No production eligibility or deployment approval was evaluated.",
        ],
    }

    model_buffer = io.BytesIO()
    joblib.dump(fitted_candidate, model_buffer, compress=3)
    model_bytes = model_buffer.getvalue()
    model_sha256 = hashlib.sha256(model_bytes).hexdigest()
    library_versions = {
        "engine": ENGINE_VERSION,
        "python_model_format": "joblib",
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }
    model_metadata = {
        "kind": "EVALUATED_MODEL",
        "disposition": "EVALUATED_CANDIDATE",
        "production_eligible": False,
        "backend_id": "sklearn",
        "engine_version": ENGINE_VERSION,
        "family": selected_spec.family,
        "config": selected_spec.config,
        "config_hash": _stable_hash(
            {"family": selected_spec.family, "config": selected_spec.config, "seed": seed}
        ),
        "dataset_sha256": dataset_sha256,
        "split_fingerprint": split_record["fingerprint"],
        "task_type": resolved_task,
        "target_column": target_column,
        "feature_columns": [*numeric_columns, *categorical_columns],
        "numeric_columns": numeric_columns,
        "categorical_columns": categorical_columns,
        "primary_metric": primary_metric_name,
        "cv_metrics": selected_cv_metrics,
        "seed": seed,
        "serialization": "joblib",
        "artifact": {
            "kind": "SKLEARN_JOBLIB",
            "media_type": "application/octet-stream",
            "serialization": "joblib",
            "size_bytes": len(model_bytes),
            "sha256": model_sha256,
        },
        "sha256": model_sha256,
        "library_versions": library_versions,
        "loading_warning": "Only load this artifact from the trusted AutoML artifact store.",
    }

    report = {
        "schema_version": "tabular-report.v1",
        "profile": profile,
        "task": task_record,
        "split": split_record,
        "baseline": baseline,
        "trials": trials,
        "evaluation": evaluation,
        "model_metadata": model_metadata,
    }
    report_bytes = json.dumps(
        _jsonify(report),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return TabularAutoMLResult(
        profile=profile,
        task=task_record,
        split=split_record,
        baseline=baseline,
        trials=trials,
        evaluation=evaluation,
        model_metadata=model_metadata,
        model_bytes=model_bytes,
        report_bytes=report_bytes,
    )


def _read_source_bytes(source: Source, media_type: str | None) -> tuple[bytes, str, str]:
    source_name = "uploaded-table"
    suffix = ""
    if isinstance(source, (str, Path)):
        path = Path(source)
        source_name = path.name
        suffix = path.suffix.lower()
        try:
            raw_bytes = path.read_bytes()
        except OSError as error:
            raise DatasetParseError(
                "The dataset object could not be read.", source_name=source_name
            ) from error
    elif isinstance(source, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(source)
    else:
        raise TypeError("source must be bytes or a filesystem path")
    if not raw_bytes:
        raise InvalidDatasetError("The dataset object is empty.")

    normalized = media_type.lower().strip() if media_type else None
    if normalized in _CSV_MEDIA_TYPES or (normalized is None and suffix == ".csv"):
        resolved = "text/csv"
    elif normalized in _PARQUET_MEDIA_TYPES or (
        normalized is None and suffix in {".parquet", ".pq"}
    ):
        resolved = "application/vnd.apache.parquet"
    else:
        raise UnsupportedMediaTypeError(
            "Only CSV and Parquet are supported.", media_type=media_type, suffix=suffix
        )
    return raw_bytes, resolved, source_name


def _parse_table(raw_bytes: bytes, media_type: str) -> pd.DataFrame:
    try:
        if media_type == "text/csv":
            _validate_csv_header(raw_bytes)
            frame = pd.read_csv(io.BytesIO(raw_bytes), encoding="utf-8-sig", low_memory=False)
        else:
            frame = pd.read_parquet(io.BytesIO(raw_bytes), engine="pyarrow")
    except MLEngineError:
        raise
    except Exception as error:
        raise DatasetParseError(
            "The tabular dataset could not be parsed.", error_type=type(error).__name__
        ) from error
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise InvalidDatasetError("The dataset must contain at least one data row.")
    columns = [str(column) for column in frame.columns]
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise InvalidDatasetError("Column names must be unique.", duplicate_columns=duplicates)
    frame = frame.copy(deep=True)
    frame.columns = columns
    return frame.reset_index(drop=True)


def _semantic_type(series: pd.Series) -> str:
    """Map a pandas dtype to a stable, value-free semantic category."""

    dtype = series.dtype
    if is_bool_dtype(dtype):
        return "BOOLEAN"
    if is_integer_dtype(dtype):
        return "INTEGER"
    if is_float_dtype(dtype):
        return "FLOAT"
    if is_numeric_dtype(dtype):
        return "NUMERIC"
    if is_datetime64_any_dtype(dtype):
        return "DATETIME"
    if isinstance(dtype, pd.CategoricalDtype):
        return "CATEGORICAL"
    if is_string_dtype(dtype) or is_object_dtype(dtype):
        return "STRING"
    return "OTHER"


def _validate_csv_header(raw_bytes: bytes) -> None:
    try:
        text = raw_bytes.decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(text)))
    except (UnicodeDecodeError, csv.Error, StopIteration) as error:
        raise DatasetParseError("The CSV header is invalid or is not UTF-8.") from error
    if not header or any(not name.strip() for name in header):
        raise InvalidDatasetError("Every CSV column must have a non-empty name.")
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise InvalidDatasetError("Column names must be unique.", duplicate_columns=duplicates)


def _resolve_task_type(target: pd.Series, requested: TaskType | str | None) -> TaskType:
    if requested is not None:
        normalized = str(requested).upper().strip()
        if normalized not in {"BINARY_CLASSIFICATION", "REGRESSION"}:
            raise InvalidTargetError("Only binary classification and regression are supported.")
        if normalized == "BINARY_CLASSIFICATION":
            if target.nunique(dropna=False) != 2:
                raise MulticlassTargetError(
                    "Binary classification requires exactly two target classes.",
                    class_count=int(target.nunique(dropna=False)),
                )
            return "BINARY_CLASSIFICATION"
        if not is_numeric_dtype(target.dtype) or is_bool_dtype(target.dtype):
            raise InvalidTargetError("Regression requires a numeric target.")
        return "REGRESSION"

    unique_count = int(target.nunique(dropna=False))
    if unique_count == 2:
        return "BINARY_CLASSIFICATION"
    if not is_numeric_dtype(target.dtype) or is_bool_dtype(target.dtype):
        raise MulticlassTargetError(
            "Targets with more than two categorical classes are not supported.",
            class_count=unique_count,
        )
    if pd.api.types.is_integer_dtype(target.dtype) and unique_count <= 20:
        raise MulticlassTargetError(
            "A low-cardinality integer target is ambiguous and is not auto-treated as regression.",
            class_count=unique_count,
        )
    return "REGRESSION"


def _prepare_target(
    target: pd.Series,
    *,
    task_type: TaskType,
    positive_class: Any | None,
    primary_metric: str | None,
) -> tuple[pd.Series, dict[str, Any]]:
    if task_type == "REGRESSION":
        numeric = pd.to_numeric(target, errors="coerce").astype("float64")
        if numeric.isna().any() or not np.isfinite(numeric.to_numpy()).all():
            raise InvalidTargetError("Regression targets must be finite numeric values.")
        metric = _validate_primary_metric(primary_metric or "rmse", task_type)
        return numeric.reset_index(drop=True), {
            "kind": "TASK_SPEC",
            "task_type": task_type,
            "primary_metric": metric,
            "positive_class": None,
            "positive_class_inferred": False,
        }

    classes = sorted((_json_scalar(value) for value in pd.unique(target)), key=_stable_sort_key)
    inferred = positive_class is None
    selected_positive = _choose_positive_class(classes, positive_class)
    encoded = target.map(lambda value: int(_scalar_equal(_json_scalar(value), selected_positive)))
    if set(encoded.unique()) != {0, 1}:
        raise InvalidTargetError("The positive class did not produce a binary target encoding.")
    negative_class = next(value for value in classes if not _scalar_equal(value, selected_positive))
    metric = _validate_primary_metric(primary_metric or "average_precision", task_type)
    return encoded.astype("int8").reset_index(drop=True), {
        "kind": "TASK_SPEC",
        "task_type": task_type,
        "primary_metric": metric,
        "positive_class": selected_positive,
        "negative_class": negative_class,
        "positive_class_inferred": inferred,
    }


def _choose_positive_class(classes: Sequence[Any], requested: Any | None) -> Any:
    if requested is not None:
        requested_value = _json_scalar(requested)
        for value in classes:
            if _scalar_equal(value, requested_value):
                return value
        raise InvalidTargetError("positive_class is not present in the target.")
    if len(classes) == 2 and all(isinstance(value, bool) for value in classes):
        return next(value for value in classes if value is True)
    if len(classes) == 2 and all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in classes
    ):
        numeric_values = {float(value) for value in classes}
        if numeric_values == {0.0, 1.0}:
            return next(value for value in classes if float(value) == 1.0)
    raise PositiveClassRequiredError(
        "positive_class is required unless binary labels are boolean or numeric {0, 1}.",
        class_count=len(classes),
        classes=list(classes),
    )


def _validate_primary_metric(metric: str, task_type: TaskType) -> str:
    normalized = metric.lower().strip()
    allowed = (
        {"average_precision", "roc_auc", "log_loss", "accuracy"}
        if task_type == "BINARY_CLASSIFICATION"
        else {"rmse", "mae", "r2"}
    )
    if normalized not in allowed:
        raise InvalidTargetError(
            "The primary metric is not valid for the resolved task.",
            primary_metric=metric,
            task_type=task_type,
        )
    return normalized


def _prepare_features(
    frame: pd.DataFrame, *, max_categories: int
) -> tuple[pd.DataFrame, list[str], list[str], list[dict[str, str]], int]:
    prepared = pd.DataFrame(index=frame.index)
    numeric_columns: list[str] = []
    categorical_columns: list[str] = []
    excluded: list[dict[str, str]] = []
    infinite_values = 0
    for column in frame.columns:
        series = frame[column]
        if series.isna().all():
            excluded.append({"column": str(column), "reason": "ALL_MISSING"})
            continue
        if is_datetime64_any_dtype(series.dtype):
            excluded.append({"column": str(column), "reason": "DATETIME_REQUIRES_POLICY"})
            continue
        if is_numeric_dtype(series.dtype) and not is_bool_dtype(series.dtype):
            numeric = pd.to_numeric(series, errors="coerce").astype("float64")
            infinite_mask = np.isinf(numeric.to_numpy())
            infinite_values += int(infinite_mask.sum())
            if infinite_mask.any():
                numeric = numeric.mask(np.isinf(numeric), np.nan)
            if numeric.nunique(dropna=True) <= 1:
                excluded.append({"column": str(column), "reason": "CONSTANT"})
                continue
            prepared[str(column)] = numeric
            numeric_columns.append(str(column))
            continue
        if (
            is_bool_dtype(series.dtype)
            or is_object_dtype(series.dtype)
            or is_string_dtype(series.dtype)
            or isinstance(series.dtype, pd.CategoricalDtype)
        ):
            if any(
                isinstance(value, (dict, list, set, tuple)) for value in series.dropna().head(1000)
            ):
                excluded.append({"column": str(column), "reason": "NESTED_VALUES"})
                continue
            cardinality = int(series.nunique(dropna=True))
            if cardinality <= 1:
                excluded.append({"column": str(column), "reason": "CONSTANT"})
                continue
            if cardinality > max_categories:
                excluded.append({"column": str(column), "reason": "HIGH_CARDINALITY"})
                continue
            prepared[str(column)] = series.map(
                lambda value: np.nan if pd.isna(value) else str(value)
            )
            categorical_columns.append(str(column))
            continue
        excluded.append({"column": str(column), "reason": "UNSUPPORTED_DTYPE"})
    return prepared, numeric_columns, categorical_columns, excluded, infinite_values


def _row_fingerprints(features: pd.DataFrame) -> np.ndarray:
    try:
        hashes = pd.util.hash_pandas_object(features, index=False, categorize=True)
    except (TypeError, ValueError) as error:
        raise InvalidDatasetError(
            "Feature rows could not be deterministically fingerprinted."
        ) from error
    return hashes.to_numpy(dtype="uint64")


def _validate_duplicate_labels(groups: np.ndarray, target: pd.Series) -> None:
    grouped = pd.DataFrame({"group": groups, "target": target.to_numpy()})
    conflicts = grouped.groupby("group", sort=False)["target"].nunique(dropna=False)
    conflicting = [str(int(value)) for value in conflicts[conflicts > 1].index]
    if conflicting:
        raise ConflictingDuplicateLabelsError(
            "Identical usable feature rows have conflicting target values.",
            conflicting_group_count=len(conflicting),
            group_fingerprints=conflicting[:20],
        )


def _freeze_split(
    features: pd.DataFrame,
    target: pd.Series,
    groups: np.ndarray,
    *,
    task_type: TaskType,
    dataset_sha256: str,
    seed: int,
    test_size: float,
    cv_folds: int,
) -> tuple[
    pd.DataFrame,
    pd.Series,
    np.ndarray,
    _SealedHoldout,
    list[tuple[np.ndarray, np.ndarray]],
    dict[str, Any],
]:
    group_frame = pd.DataFrame({"group": groups, "target": target.to_numpy()}).drop_duplicates(
        "group", keep="first"
    )
    unique_groups = group_frame["group"].to_numpy(dtype="uint64")
    group_targets = group_frame["target"].to_numpy()
    if len(unique_groups) < max(2 * cv_folds, 10):
        raise InsufficientDataError(
            "Too few independent feature groups for holdout and cross-validation.",
            independent_groups=len(unique_groups),
            cv_folds=cv_folds,
        )
    stratify = group_targets if task_type == "BINARY_CLASSIFICATION" else None
    if stratify is not None:
        counts = pd.Series(stratify).value_counts()
        if len(counts) != 2 or int(counts.min()) < cv_folds + 1:
            raise InsufficientDataError(
                "Each class needs enough independent groups for holdout and every CV fold.",
                minimum_class_groups=int(counts.min()) if len(counts) else 0,
                cv_folds=cv_folds,
            )
    try:
        development_group_ids, test_group_ids = train_test_split(
            unique_groups,
            test_size=test_size,
            random_state=seed,
            shuffle=True,
            stratify=stratify,
        )
    except ValueError as error:
        raise InsufficientDataError(
            "The requested sealed holdout could not be created.", error_type=type(error).__name__
        ) from error

    test_group_set = set(int(value) for value in test_group_ids)
    test_mask = np.fromiter((int(value) in test_group_set for value in groups), dtype=bool)
    development_positions = np.flatnonzero(~test_mask)
    test_positions = np.flatnonzero(test_mask)
    if not len(development_positions) or not len(test_positions):
        raise InsufficientDataError("Both development and holdout partitions must be non-empty.")

    development_features = features.iloc[development_positions].reset_index(drop=True)
    development_target = target.iloc[development_positions].reset_index(drop=True)
    development_groups = groups[development_positions]
    holdout_features = features.iloc[test_positions].reset_index(drop=True)
    holdout_target = target.iloc[test_positions].reset_index(drop=True)
    if task_type == "BINARY_CLASSIFICATION" and (
        development_target.nunique() != 2 or holdout_target.nunique() != 2
    ):
        raise InsufficientDataError("Both development and holdout must contain both classes.")

    if task_type == "BINARY_CLASSIFICATION":
        splitter = StratifiedGroupKFold(n_splits=cv_folds, shuffle=True, random_state=seed)
        raw_splits = splitter.split(
            development_features, development_target, groups=development_groups
        )
    else:
        splitter = GroupKFold(n_splits=cv_folds)
        raw_splits = splitter.split(
            development_features, development_target, groups=development_groups
        )
    cv_splits = [(np.asarray(train), np.asarray(validation)) for train, validation in raw_splits]
    for train, validation in cv_splits:
        if task_type == "BINARY_CLASSIFICATION" and (
            development_target.iloc[train].nunique() != 2
            or development_target.iloc[validation].nunique() != 2
        ):
            raise InsufficientDataError("Every classification CV fold must contain both classes.")

    row_ids = [
        hashlib.sha256(f"{dataset_sha256}:{position}".encode()).hexdigest()[:24]
        for position in range(len(features))
    ]
    fold_records: list[dict[str, Any]] = []
    for fold, (train, validation) in enumerate(cv_splits):
        fold_records.append(
            {
                "fold": fold,
                "train_row_ids": [row_ids[int(development_positions[index])] for index in train],
                "validation_row_ids": [
                    row_ids[int(development_positions[index])] for index in validation
                ],
            }
        )
    split_core = {
        "strategy": "STRATIFIED_GROUP_HOLDOUT"
        if task_type == "BINARY_CLASSIFICATION"
        else "GROUP_HOLDOUT",
        "dataset_sha256": dataset_sha256,
        "seed": seed,
        "test_size": test_size,
        "cv_folds": cv_folds,
        "development_row_ids": [row_ids[int(index)] for index in development_positions],
        "test_row_ids": [row_ids[int(index)] for index in test_positions],
        "folds": fold_records,
    }
    split_record = {
        "kind": "SPLIT_MANIFEST",
        **split_core,
        "train_rows": int(len(development_positions)),
        "validation_rows": 0,
        "development_rows": int(len(development_positions)),
        "test_rows": int(len(test_positions)),
        "fingerprint": _stable_hash(split_core),
        "holdout_sealed": True,
        "leakage_checks": [
            {
                "code": "EXACT_DUPLICATES_GROUPED",
                "severity": "INFO",
                "message": "Exact duplicate usable feature rows were assigned as one group.",
                "remediation": None,
                "evidence_refs": [],
            },
            {
                "code": "FINAL_HOLDOUT_SEALED",
                "severity": "INFO",
                "message": "The final holdout was unavailable to cross-validation and selection.",
                "remediation": None,
                "evidence_refs": [],
            },
        ],
    }
    return (
        development_features,
        development_target,
        development_groups,
        _SealedHoldout(holdout_features, holdout_target),
        cv_splits,
        split_record,
    )


def _build_preprocessor(
    numeric_columns: Sequence[str], categorical_columns: Sequence[str]
) -> ColumnTransformer:
    transformers: list[tuple[str, Pipeline, list[str]]] = []
    if numeric_columns:
        numeric_pipeline = Pipeline(
            [
                (
                    "imputer",
                    SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                ),
                ("scaler", StandardScaler()),
            ]
        )
        transformers.append(("numeric", numeric_pipeline, list(numeric_columns)))
    if categorical_columns:
        categorical_pipeline = Pipeline(
            [
                (
                    "imputer",
                    SimpleImputer(strategy="most_frequent", keep_empty_features=True),
                ),
                (
                    "one_hot",
                    OneHotEncoder(handle_unknown="ignore", sparse_output=True),
                ),
            ]
        )
        transformers.append(("categorical", categorical_pipeline, list(categorical_columns)))
    return ColumnTransformer(transformers, remainder="drop", sparse_threshold=0.3)


def _model_specs(task_type: TaskType, seed: int) -> tuple[_CandidateSpec, list[_CandidateSpec]]:
    if task_type == "BINARY_CLASSIFICATION":
        return (
            _CandidateSpec(
                "DUMMY_CLASSIFIER", DummyClassifier(strategy="prior"), {"strategy": "prior"}
            ),
            [
                _CandidateSpec(
                    "LOGISTIC_REGRESSION",
                    LogisticRegression(
                        C=1.0,
                        class_weight="balanced",
                        max_iter=1000,
                        random_state=seed,
                        solver="liblinear",
                    ),
                    {
                        "C": 1.0,
                        "class_weight": "balanced",
                        "max_iter": 1000,
                        "solver": "liblinear",
                    },
                ),
                _CandidateSpec(
                    "RANDOM_FOREST_CLASSIFIER",
                    RandomForestClassifier(
                        n_estimators=64,
                        min_samples_leaf=2,
                        class_weight="balanced_subsample",
                        random_state=seed,
                        n_jobs=1,
                    ),
                    {
                        "n_estimators": 64,
                        "min_samples_leaf": 2,
                        "class_weight": "balanced_subsample",
                        "n_jobs": 1,
                    },
                ),
            ],
        )
    return (
        _CandidateSpec("DUMMY_REGRESSOR", DummyRegressor(strategy="mean"), {"strategy": "mean"}),
        [
            _CandidateSpec(
                "RIDGE",
                Ridge(alpha=1.0, solver="lsqr"),
                {"alpha": 1.0, "solver": "lsqr"},
            ),
            _CandidateSpec(
                "RANDOM_FOREST_REGRESSOR",
                RandomForestRegressor(
                    n_estimators=64,
                    min_samples_leaf=2,
                    random_state=seed,
                    n_jobs=1,
                ),
                {"n_estimators": 64, "min_samples_leaf": 2, "n_jobs": 1},
            ),
        ],
    )


def _pipeline(preprocessor: ColumnTransformer, estimator: BaseEstimator) -> Pipeline:
    return Pipeline([("preprocessor", clone(preprocessor)), ("estimator", clone(estimator))])


def _scoring(task_type: TaskType) -> dict[str, str]:
    if task_type == "BINARY_CLASSIFICATION":
        return {
            "average_precision": "average_precision",
            "roc_auc": "roc_auc",
            "log_loss": "neg_log_loss",
            "accuracy": "accuracy",
        }
    return {
        "rmse": "neg_root_mean_squared_error",
        "mae": "neg_mean_absolute_error",
        "r2": "r2",
    }


def _cross_validated_metrics(
    pipeline: Pipeline,
    features: pd.DataFrame,
    target: pd.Series,
    cv_splits: Sequence[tuple[np.ndarray, np.ndarray]],
    *,
    scoring: dict[str, str],
    task_type: TaskType,
) -> dict[str, dict[str, float]]:
    result = cross_validate(
        pipeline,
        features,
        target,
        cv=list(cv_splits),
        scoring=scoring,
        n_jobs=1,
        error_score="raise",
        return_train_score=False,
    )
    metrics: dict[str, dict[str, float]] = {}
    minimized = {"log_loss"} if task_type == "BINARY_CLASSIFICATION" else {"rmse", "mae"}
    for name in scoring:
        values = np.asarray(result[f"test_{name}"], dtype="float64")
        if name in minimized:
            values = -values
        if not np.isfinite(values).all():
            raise ModelTrainingError("Cross-validation produced a non-finite metric.", metric=name)
        metrics[name] = {"mean": float(values.mean()), "std": float(values.std(ddof=0))}
    return metrics


def _holdout_metrics(
    pipeline: Pipeline,
    features: pd.DataFrame,
    target: pd.Series,
    task_type: TaskType,
) -> dict[str, float]:
    if task_type == "BINARY_CLASSIFICATION":
        probabilities = pipeline.predict_proba(features)
        classes = list(pipeline.classes_)
        positive_index = classes.index(1)
        positive_probability = probabilities[:, positive_index]
        predictions = pipeline.predict(features)
        metrics = {
            "average_precision": average_precision_score(target, positive_probability),
            "roc_auc": roc_auc_score(target, positive_probability),
            "log_loss": log_loss(target, probabilities, labels=classes),
            "accuracy": accuracy_score(target, predictions),
        }
    else:
        predictions = pipeline.predict(features)
        metrics = {
            "rmse": root_mean_squared_error(target, predictions),
            "mae": mean_absolute_error(target, predictions),
            "r2": r2_score(target, predictions),
        }
    normalized = {name: float(value) for name, value in metrics.items()}
    if not all(math.isfinite(value) for value in normalized.values()):
        raise ModelTrainingError("Final holdout evaluation produced a non-finite metric.")
    return normalized


def _metric_is_maximized(metric: str) -> bool:
    return metric not in {"log_loss", "rmse", "mae"}


def _metric_value(name: str, value: float) -> dict[str, Any]:
    return {
        "name": name,
        "value": float(value),
        "direction": "MAXIMIZE" if _metric_is_maximized(name) else "MINIMIZE",
        "confidence_interval": None,
    }


def _metric_value_list(metrics: dict[str, Any], *, from_cv: bool = False) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for name, item in metrics.items():
        value = item["mean"] if from_cv else item
        values.append(_metric_value(name, float(value)))
    return values


def _series_values_equal(left: pd.Series, right: pd.Series) -> bool:
    if len(left) != len(right):
        return False
    for left_value, right_value in zip(left.to_numpy(), right.to_numpy(), strict=True):
        if pd.isna(left_value) and pd.isna(right_value):
            continue
        if not _scalar_equal(_json_scalar(left_value), _json_scalar(right_value)):
            return False
    return True


def _scalar_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _stable_sort_key(value: Any) -> str:
    return json.dumps(_json_scalar(value), ensure_ascii=True, sort_keys=True)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        _jsonify(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_jsonify(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite values cannot be serialized")
    return value


__all__ = [
    "AllNullTargetError",
    "ConflictingDuplicateLabelsError",
    "ConstantTargetError",
    "DatasetParseError",
    "IIDAssumptionRequiredError",
    "InsufficientDataError",
    "InvalidDatasetError",
    "InvalidTargetError",
    "MLEngineError",
    "MissingTargetError",
    "ModelTrainingError",
    "MulticlassTargetError",
    "NoUsableFeaturesError",
    "PositiveClassRequiredError",
    "PreparedTabularData",
    "TabularAutoMLResult",
    "TargetContainsMissingError",
    "TargetLeakageError",
    "UnsupportedMediaTypeError",
    "inspect_tabular_dataset",
    "prepare_tabular_data",
    "run_tabular_automl",
]
