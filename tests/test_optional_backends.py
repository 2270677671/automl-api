from __future__ import annotations

import sys
import tarfile
import types
import json
import io

import numpy as np
import pandas as pd
import pytest

from automl_api.backends import autogluon as autogluon_module
from automl_api.backends import tabpfn as tabpfn_module
from automl_api.backends.autogluon import AutoGluonBackend
from automl_api.backends.base import BackendUnavailableError
from automl_api.backends.tabpfn import (
    BackendWallTimeExceededError,
    TabPFNBackend,
    _TabPFNPreprocessor,
    _model_path_for_task,
)


def _classification_csv(rows: int = 72) -> bytes:
    signal = np.linspace(-3.0, 3.0, rows)
    frame = pd.DataFrame(
        {
            "signal": signal,
            "noise": np.sin(np.arange(rows)),
            "segment": np.where(np.arange(rows) % 3, "consumer", "enterprise"),
            "target": (signal + 0.2 * np.sin(np.arange(rows)) > 0).astype(int),
        }
    )
    frame.loc[frame.index % 17 == 0, "segment"] = None
    return frame.to_csv(index=False).encode()


def test_autogluon_descriptor_and_budget_are_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autogluon_module, "_installed_version", lambda: "1.5.0")
    monkeypatch.setattr(autogluon_module.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS", "20")

    descriptor = AutoGluonBackend().descriptor

    assert descriptor.available is True
    assert descriptor.backend_version == "1.5.0"
    assert descriptor.artifact_kind == "AUTOGLUON_PREDICTOR_TAR_GZ"
    assert descriptor.capabilities.limits["default_num_cpus"] == 1
    assert autogluon_module._time_limit_seconds(12) == 9


def test_autogluon_unavailable_error_does_not_import_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autogluon_module, "_installed_version", lambda: None)
    monkeypatch.setattr(autogluon_module.importlib.util, "find_spec", lambda _: None)

    with pytest.raises(BackendUnavailableError) as error:
        AutoGluonBackend().run(
            _classification_csv(),
            target_column="target",
            media_type="text/csv",
            iid_confirmed=True,
        )

    assert error.value.code == "BACKEND_UNAVAILABLE"
    assert error.value.context["backend_id"] == "autogluon"


def test_autogluon_descriptor_handles_missing_parent_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autogluon_module, "_installed_version", lambda: None)

    def missing_parent(name: str):
        if name == "autogluon":
            return None
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(autogluon_module.importlib.util, "find_spec", missing_parent)

    descriptor = AutoGluonBackend().descriptor

    assert descriptor.available is False
    assert descriptor.unavailable_reason == "STANDARD_DEPENDENCY_NOT_INSTALLED"


@pytest.mark.skipif(
    AutoGluonBackend().descriptor.available is False,
    reason="AutoGluon is not installed in this test environment",
)
def test_real_autogluon_smoke_produces_deployment_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS", "5")
    result = AutoGluonBackend().run(
        _classification_csv(),
        target_column="target",
        media_type="text/csv",
        iid_confirmed=True,
        seed=19,
        max_trials=1,
        max_wall_time_seconds=20,
    )

    assert result.model_metadata["backend_id"] == "autogluon"
    assert result.model_metadata["config"]["hyperparameters"] == ["RF"]
    assert result.model_metadata["artifact"]["size_bytes"] == len(result.model_bytes)
    assert result.evaluation["holdout_evaluations"] == 1
    with tarfile.open(fileobj=io.BytesIO(result.model_bytes), mode="r:gz") as archive:
        names = archive.getnames()
    assert "predictor/learner.pkl" in names
    assert not any("utils/data" in name.lower() for name in names)


def test_tabpfn_preprocessor_is_fold_fitted_and_handles_unknown_categories() -> None:
    train = pd.DataFrame(
        {
            "number": [1.0, np.nan, 5.0],
            "category": ["a", "b", None],
        }
    )
    validation = pd.DataFrame({"number": [np.nan], "category": ["unseen"]})
    preprocessor = _TabPFNPreprocessor(["number"], ["category"]).fit(train)

    transformed = preprocessor.transform(validation)

    assert transformed.shape == (1, 2)
    assert transformed[0, 0] == 3.0
    assert transformed[0, 1] == -1.0
    assert preprocessor.categorical_indices == [1]


class _FakeTabPFNClassifier:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.classes_ = np.asarray([0, 1])

    def fit(self, features: np.ndarray, target: np.ndarray) -> _FakeTabPFNClassifier:
        self.center_ = float(np.median(features[:, 0]))
        self.target_mean_ = float(np.mean(target))
        return self

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        positive = 1.0 / (1.0 + np.exp(-(features[:, 0] - self.center_)))
        return np.column_stack([1.0 - positive, positive])

    def predict(self, features: np.ndarray) -> np.ndarray:
        return (self.predict_proba(features)[:, 1] >= 0.5).astype(int)

    def save_fit_state(self, path: object) -> None:
        raise AssertionError("the API must not export TabPFN fit state")


class _FakeTabPFNRegressor(_FakeTabPFNClassifier):
    def predict(self, features: np.ndarray) -> np.ndarray:
        return features[:, 0]


def test_tabpfn_fake_runtime_exercises_cv_holdout_and_data_free_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    module = types.ModuleType("tabpfn")
    module.TabPFNClassifier = _FakeTabPFNClassifier
    module.TabPFNRegressor = _FakeTabPFNRegressor
    monkeypatch.setitem(sys.modules, "tabpfn", module)
    monkeypatch.setattr(tabpfn_module, "_installed_version", lambda: "8.1.0")
    monkeypatch.setattr(tabpfn_module.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("AUTOML_TABPFN_LICENSE_ACCEPTED", "true")
    checkpoint = tmp_path / "checkpoint.ckpt"
    checkpoint.write_bytes(b"fake-checkpoint")
    monkeypatch.setenv("AUTOML_TABPFN_MODEL_PATH", str(checkpoint))

    result = TabPFNBackend().run(
        _classification_csv(),
        target_column="target",
        media_type="text/csv",
        iid_confirmed=True,
        seed=23,
    )

    assert result.model_metadata["backend_id"] == "tabpfn"
    assert result.model_metadata["exportable"] is False
    assert result.evaluation["holdout_evaluations"] == 1
    assert set(result.trials[0]["cv_metrics"]) == {
        "average_precision",
        "roc_auc",
        "log_loss",
        "accuracy",
    }
    artifact = json.loads(result.model_bytes)
    assert artifact["exportable"] is False
    assert artifact["contains_model_state"] is False
    assert artifact["contains_training_data"] is False
    assert artifact["contains_category_vocabulary"] is False
    assert b"consumer" not in result.model_bytes
    assert b"enterprise" not in result.model_bytes
    assert b"fake-tabpfn-fit-state" not in result.model_bytes


def test_tabpfn_checks_wall_time_between_execution_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    module = types.ModuleType("tabpfn")
    module.TabPFNClassifier = _FakeTabPFNClassifier
    module.TabPFNRegressor = _FakeTabPFNRegressor
    monkeypatch.setitem(sys.modules, "tabpfn", module)
    monkeypatch.setattr(tabpfn_module, "_installed_version", lambda: "8.1.0")
    monkeypatch.setattr(tabpfn_module.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("AUTOML_TABPFN_LICENSE_ACCEPTED", "true")
    checkpoint = tmp_path / "checkpoint.ckpt"
    checkpoint.write_bytes(b"fake-checkpoint")
    monkeypatch.setenv("AUTOML_TABPFN_MODEL_PATH", str(checkpoint))
    clock = iter([100.0, 102.0])
    monkeypatch.setattr(tabpfn_module.time, "monotonic", lambda: next(clock))

    with pytest.raises(BackendWallTimeExceededError) as error:
        TabPFNBackend().run(
            _classification_csv(),
            target_column="target",
            media_type="text/csv",
            iid_confirmed=True,
            max_wall_time_seconds=1,
        )

    assert error.value.code == "BACKEND_WALL_TIME_EXCEEDED"


def test_tabpfn_reports_stable_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabpfn_module, "_installed_version", lambda: None)
    monkeypatch.setattr(tabpfn_module.importlib.util, "find_spec", lambda _: None)

    descriptor = TabPFNBackend().descriptor

    assert descriptor.available is False
    assert descriptor.unavailable_reason == "STANDARD_DEPENDENCY_NOT_INSTALLED"
    assert descriptor.capabilities.limits["max_cpu_rows"] == 1_000


def test_tabpfn_installed_is_distinct_from_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabpfn_module, "_installed_version", lambda: "8.1.0")
    monkeypatch.setattr(tabpfn_module.importlib.util, "find_spec", lambda _: object())
    monkeypatch.delenv("AUTOML_TABPFN_LICENSE_ACCEPTED", raising=False)
    monkeypatch.delenv("AUTOML_TABPFN_MODEL_PATH", raising=False)
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)

    descriptor = TabPFNBackend().descriptor

    assert descriptor.installed is True
    assert descriptor.available is False
    assert descriptor.unavailable_reason == "MODEL_LICENSE_NOT_ACCEPTED"


def test_tabpfn_public_v2_requires_both_task_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    monkeypatch.setattr(tabpfn_module, "_installed_version", lambda: "8.1.0")
    monkeypatch.setattr(tabpfn_module.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("AUTOML_TABPFN_LICENSE_ACCEPTED", "true")
    monkeypatch.setenv("AUTOML_TABPFN_MODEL_SOURCE", "public-v2")
    monkeypatch.setenv("TABPFN_MODEL_CACHE_DIR", str(tmp_path))
    classifier = tmp_path / "tabpfn-v2-classifier.ckpt"
    regressor = tmp_path / "tabpfn-v2-regressor.ckpt"
    classifier.write_bytes(b"classifier")

    incomplete = TabPFNBackend().descriptor
    assert incomplete.available is False
    assert incomplete.unavailable_reason == "MODEL_PATH_NOT_FOUND"

    regressor.write_bytes(b"regressor")
    ready = TabPFNBackend().descriptor
    assert ready.available is True
    assert ready.capabilities.required_attributions == ("Built with PriorLabs-TabPFN",)
    assert ready.as_dict()["capabilities"]["required_attributions"] == [
        "Built with PriorLabs-TabPFN"
    ]
    assert _model_path_for_task("BINARY_CLASSIFICATION", "public-v2") == str(classifier)
    assert _model_path_for_task("REGRESSION", "public-v2") == str(regressor)
