from __future__ import annotations

from fastapi.testclient import TestClient

from automl_api.app import create_app
from automl_api.store import InMemoryStore

from .helpers import create_ready_dataset, mutation_headers, run_request


def test_declared_dataset_size_is_limited(monkeypatch) -> None:
    monkeypatch.setenv("AUTOML_MAX_DATASET_BYTES", "32")
    with TestClient(create_app(InMemoryStore())) as client:
        response = client.post(
            "/v1/datasets",
            headers=mutation_headers("too-large-dataset-key"),
            json={
                "name": "too-large",
                "filename": "data.csv",
                "media_type": "text/csv",
                "size_bytes": 33,
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "dataset_too_large"


def test_tenant_storage_limit_returns_retryable_429(monkeypatch) -> None:
    monkeypatch.setenv("AUTOML_MAX_STORAGE_BYTES_PER_TENANT", "200")
    with TestClient(create_app(InMemoryStore())) as client:
        first = create_ready_dataset(client, "quota-0001")
        assert first["dataset_version_id"]
        response = client.post(
            "/v1/datasets",
            headers=mutation_headers("quota-dataset-key-0002"),
            json={
                "name": "quota",
                "filename": "data.csv",
                "media_type": "text/csv",
                "size_bytes": 100,
            },
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3600"
    assert response.json()["code"] == "tenant_storage_limit_exceeded"


def test_active_run_limit_and_budget_limit_are_enforced(monkeypatch) -> None:
    monkeypatch.setenv("AUTOML_MAX_ACTIVE_RUNS_PER_TENANT", "1")
    monkeypatch.setenv("AUTOML_MAX_TRIALS_PER_RUN", "1")
    with TestClient(create_app(InMemoryStore())) as client:
        dataset = create_ready_dataset(client, "limit-run-0001")
        request = run_request(str(dataset["dataset_version_id"]))
        too_many_trials = client.post(
            "/v1/runs",
            headers=mutation_headers("too-many-trials-key"),
            json={**request, "budget": {**request["budget"], "max_trials": 2}},
        )
        assert too_many_trials.status_code == 422
        assert too_many_trials.json()["code"] == "budget_limit_exceeded"

        accepted = client.post(
            "/v1/runs",
            headers=mutation_headers("first-active-run-key"),
            json={**request, "budget": {**request["budget"], "max_trials": 1}},
        )
        assert accepted.status_code == 202, accepted.text
        second = client.post(
            "/v1/runs",
            headers=mutation_headers("second-active-run-key"),
            json={**request, "budget": {**request["budget"], "max_trials": 1}},
        )

    assert second.status_code == 429
    assert second.headers["retry-after"] == "30"
    assert second.json()["code"] == "active_run_limit_exceeded"
