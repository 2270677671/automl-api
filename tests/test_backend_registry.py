from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from automl_api.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendMediaTypeUnsupportedError,
    BackendNotFoundError,
    BackendRegistry,
    BackendTaskUnsupportedError,
    BackendUnavailableError,
    SklearnBackend,
    build_default_registry,
)
from automl_api.ml_engine import Source, TabularAutoMLResult, TaskType
from automl_api.models import Objective

from .helpers import create_ready_dataset, mutation_headers, run_request


class _FakeBackend:
    def __init__(self, *, available: bool = True) -> None:
        self.calls: list[tuple[Source, dict[str, Any]]] = []
        self._descriptor = BackendDescriptor(
            backend_id="fake",
            display_name="Fake backend",
            engine_version="fake.v1",
            backend_version="1.2.3" if available else None,
            available=available,
            unavailable_reason=None if available else "OPTIONAL_DEPENDENCY_NOT_INSTALLED",
            optional_dependency="fake-extra",
            capabilities=BackendCapabilities(task_types=("REGRESSION",), media_types=("text/csv",)),
            artifact_kind="FAKE_ARCHIVE",
            artifact_media_type="application/zip",
            artifact_serialization="zip",
            deterministic=True,
        )

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

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
        options = {
            "target_column": target_column,
            "media_type": media_type,
            "task_type": task_type,
            "positive_class": positive_class,
            "primary_metric": primary_metric,
            "iid_confirmed": iid_confirmed,
            "seed": seed,
            "test_size": test_size,
            "cv_folds": cv_folds,
            "max_categories": max_categories,
            "max_trials": max_trials,
            "max_wall_time_seconds": max_wall_time_seconds,
        }
        self.calls.append((source, options))
        return TabularAutoMLResult(
            profile={},
            task={},
            split={},
            baseline={},
            trials=[],
            evaluation={},
            model_metadata={},
            model_bytes=b"model",
            report_bytes=b"report",
        )


def test_objective_defaults_to_the_backward_compatible_sklearn_backend() -> None:
    objective = Objective.model_validate({})

    assert objective.backend_id == "sklearn"
    assert objective.model_dump(mode="json")["backend_id"] == "sklearn"


def test_default_registry_exposes_sklearn_with_a_generic_artifact_contract() -> None:
    registry = build_default_registry()
    descriptor = registry.get("sklearn").descriptor

    assert isinstance(registry.get(), SklearnBackend)
    assert descriptor.available is True
    assert descriptor.status == "AVAILABLE"
    assert descriptor.capabilities.task_types == ("BINARY_CLASSIFICATION", "REGRESSION")
    assert descriptor.capabilities.limits == {}
    assert descriptor.capabilities.runtime_requirements == ()
    assert descriptor.as_dict()["artifact"] == {
        "kind": "SKLEARN_JOBLIB",
        "media_type": "application/octet-stream",
        "serialization": "joblib",
    }


def test_registry_has_stable_selection_and_capability_errors() -> None:
    registry = BackendRegistry(default_backend_id="fake")
    backend = _FakeBackend()
    registry.register(backend)

    with pytest.raises(BackendNotFoundError) as missing:
        registry.get("unknown")
    assert missing.value.code == "BACKEND_NOT_FOUND"
    assert missing.value.context["known_backend_ids"] == ["fake"]

    with pytest.raises(BackendTaskUnsupportedError) as task_error:
        registry.run(
            "fake",
            b"unused",
            target_column="target",
            media_type="text/csv",
            task_type="BINARY_CLASSIFICATION",
        )
    assert task_error.value.code == "BACKEND_TASK_UNSUPPORTED"

    with pytest.raises(BackendMediaTypeUnsupportedError) as media_error:
        registry.run(
            "fake",
            b"unused",
            target_column="target",
            media_type="application/vnd.apache.parquet",
            task_type="REGRESSION",
        )
    assert media_error.value.code == "BACKEND_MEDIA_TYPE_UNSUPPORTED"
    assert backend.calls == []


def test_registry_dispatches_normalized_media_aliases_and_reports_unavailable_backends() -> None:
    registry = BackendRegistry(default_backend_id="fake")
    backend = _FakeBackend()
    registry.register(backend)

    result = registry.run(
        None,
        b"unused",
        target_column="target",
        media_type="csv",
        task_type="REGRESSION",
        iid_confirmed=True,
        max_wall_time_seconds=180,
    )
    assert result.model_bytes == b"model"
    assert backend.calls[0][1]["media_type"] == "csv"
    assert backend.calls[0][1]["max_wall_time_seconds"] == 180

    unavailable = BackendRegistry(default_backend_id="fake")
    unavailable.register(_FakeBackend(available=False))
    with pytest.raises(BackendUnavailableError) as error:
        unavailable.require_available()
    assert error.value.code == "BACKEND_UNAVAILABLE"
    assert error.value.context == {
        "backend_id": "fake",
        "optional_dependency": "fake-extra",
        "reason": "OPTIONAL_DEPENDENCY_NOT_INSTALLED",
    }


def test_create_run_maps_backend_selection_errors_to_stable_api_problems(
    client: TestClient,
) -> None:
    upload = create_ready_dataset(client, "backend-errors-0001")
    request = run_request(str(upload["dataset_version_id"]))
    request["objective"]["backend_id"] = "not_registered"

    missing = client.post(
        "/v1/runs",
        headers=mutation_headers("backend-missing-0001"),
        json=request,
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "backend_not_found"
    assert missing.json()["backend_id"] == "not_registered"

    unavailable_registry = BackendRegistry(default_backend_id="fake")
    unavailable_registry.register(_FakeBackend(available=False))
    client.app.state.workflow.backend_registry = unavailable_registry
    request["objective"]["backend_id"] = "fake"
    unavailable = client.post(
        "/v1/runs",
        headers=mutation_headers("backend-unavailable-0001"),
        json=request,
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["code"] == "backend_unavailable"
    assert unavailable.json()["reason"] == "OPTIONAL_DEPENDENCY_NOT_INSTALLED"
