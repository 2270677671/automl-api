"""Shared result assembly for native tabular backends."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from sklearn.base import clone

from ..ml_engine import (
    ModelTrainingError,
    PreparedTabularData,
    TabularAutoMLResult,
    _build_preprocessor,
    _cross_validated_metrics,
    _holdout_metrics,
    _jsonify,
    _metric_is_maximized,
    _metric_value,
    _metric_value_list,
    _model_specs,
    _pipeline,
    _scoring,
    _stable_hash,
)


class PredictiveModel(Protocol):
    def predict(self, features: Any) -> Any: ...

    def predict_proba(self, features: Any) -> Any: ...


def assemble_native_result(
    prepared: PreparedTabularData,
    *,
    backend_id: str,
    engine_version: str,
    backend_version: str,
    family: str,
    config: dict[str, Any],
    development_metrics: dict[str, dict[str, float]],
    fitted_candidate: PredictiveModel,
    model_bytes: bytes,
    artifact_kind: str,
    artifact_media_type: str,
    serialization: str,
    library_versions: dict[str, str],
    limitations: list[str],
    exportable: bool = True,
    native_trials: list[dict[str, Any]] | None = None,
) -> TabularAutoMLResult:
    """Evaluate a frozen native candidate once and build protocol-ready records."""

    primary_metric = prepared.task["primary_metric"]
    if primary_metric not in development_metrics:
        raise ModelTrainingError(
            "The backend did not report its primary development metric.",
            backend_id=backend_id,
            primary_metric=primary_metric,
        )

    preprocessor = _build_preprocessor(
        prepared.numeric_columns,
        prepared.categorical_columns,
    )
    baseline_spec, _ = _model_specs(prepared.task_type, prepared.seed)
    baseline_pipeline = _pipeline(preprocessor, baseline_spec.estimator)
    try:
        baseline_metrics = _cross_validated_metrics(
            baseline_pipeline,
            prepared.development_features,
            prepared.development_target,
            prepared.cv_splits,
            scoring=_scoring(prepared.task_type),
            task_type=prepared.task_type,
        )
        fitted_baseline = clone(baseline_pipeline).fit(
            prepared.development_features,
            prepared.development_target,
        )
    except Exception as error:
        raise ModelTrainingError(
            "The deterministic baseline could not be evaluated.",
            family=baseline_spec.family,
            error_type=type(error).__name__,
        ) from error

    holdout_features, holdout_target = prepared.sealed_holdout.open_once()
    baseline_holdout_metrics = _holdout_metrics(
        fitted_baseline,
        holdout_features,
        holdout_target,
        prepared.task_type,
    )
    candidate_holdout_metrics = _holdout_metrics(
        fitted_candidate,
        holdout_features,
        holdout_target,
        prepared.task_type,
    )
    baseline_primary = float(baseline_holdout_metrics[primary_metric])
    candidate_primary = float(candidate_holdout_metrics[primary_metric])
    improvement = (
        candidate_primary - baseline_primary
        if _metric_is_maximized(primary_metric)
        else baseline_primary - candidate_primary
    )

    baseline = {
        "kind": "BASELINE_RESULT",
        "family": baseline_spec.family,
        "config": baseline_spec.config,
        "primary_metric": primary_metric,
        "cv_metrics": baseline_metrics,
        "baselines": [
            {
                "name": baseline_spec.family,
                "metrics": _metric_value_list(baseline_metrics, from_cv=True),
                "compute_credits": 0.0,
            }
        ],
    }
    config_hash = _stable_hash(
        {
            "backend_id": backend_id,
            "family": family,
            "config": config,
            "seed": prepared.seed,
        }
    )
    primary_score = float(development_metrics[primary_metric]["mean"])
    trials = native_trials or [
        {
            "kind": "TRIAL_RESULT",
            "experiment_id": f"exp_{config_hash[:16]}",
            "trial_number": 0,
            "status": "SUCCEEDED",
            "family": family,
            "model_family": family,
            "backend_id": backend_id,
            "config": config,
            "normalized_config": config,
            "config_hash": config_hash,
            "cv_metrics": development_metrics,
            "metrics": _metric_value_list(development_metrics, from_cv=True),
            "primary_metric": primary_metric,
            "primary_score": primary_score,
            "compute_credits": 0.0,
            "failure_code": None,
            "evaluation_protocol": "CROSS_VALIDATION",
        }
    ]
    evaluation = {
        "kind": "EVALUATION_REPORT",
        "primary_metric": primary_metric,
        "baseline": _metric_value(primary_metric, baseline_primary),
        "candidate": _metric_value(primary_metric, candidate_primary),
        "paired_delta": {
            "name": f"{primary_metric}_paired_improvement",
            "value": float(improvement),
            "direction": "MAXIMIZE",
            "confidence_interval": None,
        },
        "paired_improvement": float(improvement),
        "all_metrics": {
            "baseline": baseline_holdout_metrics,
            "candidate": candidate_holdout_metrics,
        },
        "selected_family": family,
        "selected_cv_score": primary_score,
        "evaluation_protocol": (
            native_trials[0].get("evaluation_protocol", "HOLDOUT_VALIDATION")
            if native_trials
            else "CROSS_VALIDATION"
        ),
        "holdout_evaluations": prepared.sealed_holdout.open_count,
        "evaluated_candidate": True,
        "production_eligible": False,
        "exportable": exportable,
        "artifact_exportable": exportable,
        "guardrails_passed": True,
        "eligible_candidate": False,
        "failed_gates": ["PRODUCTION_ELIGIBILITY_NOT_EVALUATED"],
        "eligibility_reason": "EVALUATION_ONLY_NO_PRODUCTION_GATE",
        "limitations": limitations,
    }

    model_sha256 = hashlib.sha256(model_bytes).hexdigest()
    model_metadata = {
        "kind": "EVALUATED_MODEL",
        "disposition": "EVALUATED_CANDIDATE",
        "production_eligible": False,
        "exportable": exportable,
        "backend_id": backend_id,
        "engine_version": engine_version,
        "family": family,
        "config": config,
        "config_hash": config_hash,
        "dataset_sha256": prepared.dataset_sha256,
        "split_fingerprint": prepared.split["fingerprint"],
        "task_type": prepared.task_type,
        "target_column": prepared.target_column,
        "feature_columns": [
            *prepared.numeric_columns,
            *prepared.categorical_columns,
        ],
        "numeric_columns": prepared.numeric_columns,
        "categorical_columns": prepared.categorical_columns,
        "primary_metric": primary_metric,
        "cv_metrics": development_metrics,
        "seed": prepared.seed,
        "serialization": serialization,
        "sha256": model_sha256,
        "artifact": {
            "kind": artifact_kind,
            "media_type": artifact_media_type,
            "serialization": serialization,
            "size_bytes": len(model_bytes),
            "sha256": model_sha256,
            "exportable": exportable,
            "contains_training_data": False,
        },
        "library_versions": {
            "engine": engine_version,
            backend_id: backend_version,
            **library_versions,
        },
        "loading_warning": (
            "Only load this artifact from the trusted AutoML artifact store."
            if exportable
            else "This artifact contains evaluation metadata only and has no model loader."
        ),
    }
    report = {
        "schema_version": "tabular-report.v1",
        "profile": prepared.profile,
        "task": prepared.task,
        "split": prepared.split,
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
        profile=prepared.profile,
        task=prepared.task,
        split=prepared.split,
        baseline=baseline,
        trials=trials,
        evaluation=evaluation,
        model_metadata=model_metadata,
        model_bytes=model_bytes,
        report_bytes=report_bytes,
    )


__all__ = ["PredictiveModel", "assemble_native_result"]
