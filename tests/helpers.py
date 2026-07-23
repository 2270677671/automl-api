from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


AUTH = {"Authorization": "Bearer test-tenant-token"}


def mutation_headers(key: str, **extra: str) -> dict[str, str]:
    return {**AUTH, "Idempotency-Key": key, **extra}


def run_request(dataset_version_id: str) -> dict[str, Any]:
    return {
        "dataset_version_id": dataset_version_id,
        "objective": {},
        "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
        "policy": {
            "allow_pii": False,
            "allow_external_llm": False,
            "risk_tier": "STANDARD",
        },
        "budget": {
            "max_trials": 2,
            "max_compute_credits": 1,
            "max_wall_time_seconds": 60,
            "max_llm_tokens": 0,
        },
    }


def create_ready_dataset(client: TestClient, suffix: str = "0001") -> dict[str, Any]:
    created = client.post(
        "/v1/datasets",
        headers=mutation_headers(f"dataset-key-{suffix}"),
        json={
            "name": "synthetic",
            "filename": "synthetic.csv",
            "media_type": "text/csv",
            "size_bytes": 128,
        },
    )
    assert created.status_code == 201, created.text
    upload = created.json()
    finalized = client.post(
        f"/v1/dataset-versions/{upload['dataset_version_id']}:finalize",
        headers=mutation_headers(f"finalize-key-{suffix}"),
        json={
            "upload_id": upload["upload_id"],
            "parts": [{"part_number": 1, "etag": "synthetic-part"}],
            "sha256": "a" * 64,
        },
    )
    assert finalized.status_code == 202, finalized.text
    assert finalized.json()["status"] == "READY"
    return upload


def create_waiting_run(client: TestClient, suffix: str = "0001") -> dict[str, Any]:
    upload = create_ready_dataset(client, suffix)
    response = client.post(
        "/v1/runs",
        headers=mutation_headers(f"create-run-key-{suffix}"),
        json=run_request(upload["dataset_version_id"]),
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "WAITING_USER"
    return run
