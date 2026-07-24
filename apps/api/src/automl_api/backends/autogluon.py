"""Lazy AutoGluon Tabular adapter."""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import tarfile
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..ml_engine import (
    ModelTrainingError,
    Source,
    TabularAutoMLResult,
    TaskType,
    _stable_hash,
    prepare_tabular_data,
)
from ._execution import assemble_native_result
from .base import BackendCapabilities, BackendDescriptor, BackendUnavailableError


ENGINE_VERSION = "tabular-autogluon.v1"
_DISTRIBUTION = "autogluon.tabular"


def _installed_version() -> str | None:
    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


class _AutoGluonModel:
    def __init__(self, predictor: Any, task_type: TaskType) -> None:
        self.predictor = predictor
        self.task_type = task_type
        self.classes_ = np.asarray([0, 1]) if task_type == "BINARY_CLASSIFICATION" else None

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.predictor.predict(features, as_pandas=False))

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        if self.task_type != "BINARY_CLASSIFICATION":
            raise AttributeError("predict_proba is only available for classification")
        probabilities = self.predictor.predict_proba(
            features,
            as_pandas=True,
            as_multiclass=True,
        )
        if isinstance(probabilities, pd.DataFrame):
            values = probabilities.reindex(columns=[0, 1]).to_numpy(dtype="float64")
        else:
            values = np.asarray(probabilities, dtype="float64")
        if values.ndim == 1:
            values = np.column_stack([1.0 - values, values])
        totals = values.sum(axis=1, keepdims=True)
        return np.divide(values, totals, out=np.zeros_like(values), where=totals != 0)


def _archive_predictor(directory: Path) -> bytes:
    """Create a path-independent tar.gz with stable archive headers."""

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ModelTrainingError(
                        "AutoGluon produced an unsupported symbolic link.",
                        backend_id="autogluon",
                    )
                if not path.is_file():
                    continue
                relative = Path("predictor") / path.relative_to(directory)
                payload = path.read_bytes()
                info = tarfile.TarInfo(relative.as_posix())
                info.size = len(payload)
                info.mode = 0o600
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _time_limit_seconds(max_wall_time_seconds: int | None) -> int:
    raw = os.environ.get("AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS", "20")
    try:
        value = int(raw)
    except ValueError as error:
        raise ModelTrainingError(
            "AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS must be an integer.",
            backend_id="autogluon",
        ) from error
    if not 5 <= value <= 3600:
        raise ModelTrainingError(
            "AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS must be between 5 and 3600.",
            backend_id="autogluon",
        )
    if max_wall_time_seconds is None:
        return value
    if max_wall_time_seconds < 1:
        raise ModelTrainingError(
            "max_wall_time_seconds must be positive.",
            backend_id="autogluon",
        )
    reserve = min(10, max(1, max_wall_time_seconds // 4))
    return max(1, min(value, max_wall_time_seconds - reserve))


def _backend_work_dir() -> str | None:
    configured = os.environ.get("AUTOML_BACKEND_WORK_DIR")
    if not configured:
        return None
    path = Path(configured)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ModelTrainingError(
            "The configured backend work directory is not writable.",
            backend_id="autogluon",
            error_type=type(error).__name__,
        ) from error
    return str(path)


class AutoGluonBackend:
    """Bounded AutoGluon model selection on the development partition only."""

    @property
    def descriptor(self) -> BackendDescriptor:
        version = _installed_version()
        importable = importlib.util.find_spec("autogluon") is not None and (
            importlib.util.find_spec("autogluon.tabular") is not None
        )
        available = version is not None and importable
        return BackendDescriptor(
            backend_id="autogluon",
            display_name="AutoGluon Tabular bounded model selection",
            engine_version=ENGINE_VERSION,
            backend_version=version,
            available=available,
            capabilities=BackendCapabilities(
                task_types=("BINARY_CLASSIFICATION", "REGRESSION"),
                supports_cross_validation=False,
                supports_gpu=False,
                limits={
                    "default_time_limit_seconds": 20,
                    "max_configured_time_limit_seconds": 3600,
                    "default_num_cpus": 1,
                    "default_num_gpus": 0,
                },
                runtime_requirements=(
                    "Bounded by RunBudget.max_wall_time_seconds with packaging reserve",
                    "Writable AUTOML_BACKEND_WORK_DIR for predictor directories",
                    "Default portfolio uses RF, XT, and LR without native booster add-ons",
                ),
            ),
            artifact_kind="AUTOGLUON_PREDICTOR_TAR_GZ",
            artifact_media_type="application/gzip",
            artifact_serialization="autogluon_predictor_tar_gz",
            deterministic=False,
            installed=available,
            optional_dependency=None,
            unavailable_reason=None if available else "STANDARD_DEPENDENCY_NOT_INSTALLED",
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
        descriptor = self.descriptor
        if not descriptor.available or descriptor.backend_version is None:
            raise BackendUnavailableError(
                "AutoGluon is not available in this installation.",
                backend_id="autogluon",
                installed=descriptor.installed,
                optional_dependency=descriptor.optional_dependency,
                reason=descriptor.unavailable_reason,
            )
        try:
            from autogluon.tabular import TabularPredictor
        except Exception as error:
            raise BackendUnavailableError(
                "AutoGluon could not be imported in this runtime.",
                backend_id="autogluon",
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
        label = "__managed_automl_target__"
        while label in prepared.development_features.columns:
            label = f"_{label}"
        problem_type = "binary" if prepared.task_type == "BINARY_CLASSIFICATION" else "regression"
        metric = prepared.task["primary_metric"]
        ag_metric = "root_mean_squared_error" if metric == "rmse" else metric
        time_limit = _time_limit_seconds(max_wall_time_seconds)
        portfolio = ["RF", "XT", "LR"]
        portfolio = portfolio[: min(len(portfolio), max_trials or len(portfolio))]
        config = {
            "presets": ["medium_quality"],
            "hyperparameters": portfolio,
            "time_limit_seconds": time_limit,
            "num_cpus": 1,
            "num_gpus": 0,
            "weighted_ensemble": False,
        }
        first_train, first_validation = prepared.cv_splits[0]
        train_data = prepared.development_features.iloc[first_train].copy(deep=True)
        train_data[label] = prepared.development_target.iloc[first_train].to_numpy()
        tuning_data = prepared.development_features.iloc[first_validation].copy(deep=True)
        tuning_data[label] = prepared.development_target.iloc[first_validation].to_numpy()

        try:
            with tempfile.TemporaryDirectory(
                prefix="automl-autogluon-",
                dir=_backend_work_dir(),
            ) as temporary:
                predictor_path = Path(temporary) / "predictor"
                predictor = TabularPredictor(
                    label=label,
                    problem_type=problem_type,
                    eval_metric=ag_metric,
                    path=str(predictor_path),
                    verbosity=0,
                    log_to_file=False,
                    positive_class=1 if prepared.task_type == "BINARY_CLASSIFICATION" else None,
                ).fit(
                    train_data=train_data,
                    tuning_data=tuning_data,
                    time_limit=max(1, time_limit // 2),
                    presets=config["presets"],
                    hyperparameters={family: {} for family in portfolio},
                    dynamic_stacking=False,
                    fit_weighted_ensemble=False,
                    fit_full_last_level_weighted_ensemble=False,
                    full_weighted_ensemble_additionally=False,
                    num_cpus=1,
                    num_gpus=0,
                    fit_strategy="sequential",
                )
                leaderboard = predictor.leaderboard(display=False)
                if leaderboard.empty or "score_val" not in leaderboard:
                    raise ModelTrainingError(
                        "AutoGluon did not produce a development leaderboard.",
                        backend_id="autogluon",
                    )
                selected_rows = leaderboard.loc[leaderboard["model"] == predictor.model_best]
                if selected_rows.empty:
                    raise ModelTrainingError(
                        "AutoGluon's selected model is missing from its leaderboard.",
                        backend_id="autogluon",
                    )
                selected_model_name = str(predictor.model_best)
                config["selected_model"] = selected_model_name
                score = float(selected_rows.iloc[0]["score_val"])
                if metric in {"log_loss", "rmse", "mae"}:
                    score = -score
                development_metrics = {metric: {"mean": score, "std": 0.0}}
                native_trials: list[dict[str, Any]] = []
                for trial_number, row in (
                    leaderboard.head(len(portfolio)).reset_index(drop=True).iterrows()
                ):
                    model_name = str(row["model"])
                    raw_score = row["score_val"]
                    if not np.isfinite(raw_score):
                        continue
                    trial_score = float(raw_score)
                    if metric in {"log_loss", "rmse", "mae"}:
                        trial_score = -trial_score
                    trial_family = f"AUTOGLUON_{model_name.upper().replace(' ', '_')}"
                    trial_config = {
                        "model": model_name,
                        "selection_split": "FROZEN_CV_FOLD_0",
                    }
                    trial_hash = _stable_hash(
                        {
                            "backend_id": "autogluon",
                            "family": trial_family,
                            "config": trial_config,
                            "seed": prepared.seed,
                        }
                    )
                    trial_metrics = {metric: {"mean": trial_score, "std": 0.0}}
                    native_trials.append(
                        {
                            "kind": "TRIAL_RESULT",
                            "experiment_id": f"exp_{trial_hash[:16]}",
                            "trial_number": int(trial_number),
                            "status": "SUCCEEDED",
                            "family": trial_family,
                            "model_family": trial_family,
                            "backend_id": "autogluon",
                            "config": trial_config,
                            "normalized_config": trial_config,
                            "config_hash": trial_hash,
                            "cv_metrics": trial_metrics,
                            "metrics": [
                                {
                                    "name": metric,
                                    "value": trial_score,
                                    "direction": (
                                        "MAXIMIZE"
                                        if metric not in {"log_loss", "rmse", "mae"}
                                        else "MINIMIZE"
                                    ),
                                    "confidence_interval": None,
                                }
                            ],
                            "primary_metric": metric,
                            "primary_score": trial_score,
                            "compute_credits": 0.0,
                            "failure_code": None,
                            "evaluation_protocol": "HOLDOUT_VALIDATION",
                        }
                    )
                if not native_trials:
                    raise ModelTrainingError(
                        "AutoGluon did not produce any finite model scores.",
                        backend_id="autogluon",
                    )
                predictor.refit_full(
                    model=selected_model_name,
                    set_best_to_refit_full=True,
                    num_cpus=1,
                    num_gpus=0,
                    fit_strategy="sequential",
                )
                config["refit_full"] = True
                deployment_path = Path(temporary) / "deployment-predictor"
                deployment_predictor = predictor.clone_for_deployment(
                    path=str(deployment_path),
                    return_clone=True,
                )
                model = _AutoGluonModel(deployment_predictor, prepared.task_type)
                model_bytes = _archive_predictor(deployment_path)
                return assemble_native_result(
                    prepared,
                    backend_id="autogluon",
                    engine_version=ENGINE_VERSION,
                    backend_version=descriptor.backend_version,
                    family=f"AUTOGLUON_{selected_model_name.upper().replace(' ', '_')}",
                    config=config,
                    development_metrics=development_metrics,
                    fitted_candidate=model,
                    model_bytes=model_bytes,
                    artifact_kind=descriptor.artifact_kind,
                    artifact_media_type=descriptor.artifact_media_type,
                    serialization=descriptor.artifact_serialization,
                    library_versions={"pandas": pd.__version__, "numpy": np.__version__},
                    limitations=[
                        "This backend evaluates one i.i.d. table and uses a group-safe development fold for model selection.",
                        "The packaged predictor is for offline evaluation and requires a compatible AutoGluon runtime.",
                        "No production eligibility or deployment approval was evaluated.",
                    ],
                    native_trials=native_trials,
                )
        except (BackendUnavailableError, ModelTrainingError):
            raise
        except Exception as error:
            raise ModelTrainingError(
                "AutoGluon could not train a bounded development candidate.",
                backend_id="autogluon",
                error_type=type(error).__name__,
            ) from error


__all__ = ["AutoGluonBackend"]
