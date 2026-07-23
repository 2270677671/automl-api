from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from automl_api.app import create_app

from .helpers import AUTH, mutation_headers, run_request


def _poll_run(client: TestClient, run_id: str, predicate, *, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/runs/{run_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        run = response.json()
        if predicate(run):
            return run
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not reach the expected state")


def _upload_csv(client: TestClient, content: bytes) -> str:
    created = client.post(
        "/v1/datasets",
        headers=mutation_headers("durable-create-dataset-0001"),
        json={
            "name": "durable-binary",
            "filename": "training.csv",
            "media_type": "text/csv",
            "size_bytes": len(content),
        },
    )
    assert created.status_code == 201, created.text
    upload = created.json()
    part = upload["parts"][0]
    uploaded = client.put(
        part["url"],
        headers={**AUTH, **part["required_headers"]},
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized = client.post(
        f"/v1/dataset-versions/{upload['dataset_version_id']}:finalize",
        headers=mutation_headers("durable-finalize-dataset-0001"),
        json={
            "upload_id": upload["upload_id"],
            "parts": [{"part_number": 1, "etag": uploaded.headers["etag"]}],
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert finalized.status_code == 202, finalized.text
    return str(upload["dataset_version_id"])


def test_default_app_runs_and_recovers_real_durable_workflow(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTOML_STATE_DIR", str(state_dir))
    rows = ["feature_a,feature_b,target"]
    rows.extend(f"{index},{index % 7},{index % 2}" for index in range(80))
    content = ("\n".join(rows) + "\n").encode()

    application = create_app()
    with TestClient(application) as client:
        assert client.get("/healthz").json()["mode"] == "milestone-2-local-durable"
        dataset_version_id = _upload_csv(client, content)
        request = run_request(dataset_version_id)
        request["budget"]["max_trials"] = 1
        created = client.post(
            "/v1/runs",
            headers=mutation_headers("durable-create-run-0001"),
            json=request,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]

        waiting = _poll_run(client, run_id, lambda run: run["status"] == "WAITING_USER")
        assert waiting["phase"] == "PLAN"
        packets = client.get(
            f"/v1/runs/{run_id}/decision-packets",
            headers=AUTH,
            params={"status": "OPEN"},
        )
        assert packets.status_code == 200, packets.text
        packet = packets.json()["items"][0]
        question_ids = {question["question_id"] for question in packet["questions"]}
        assert question_ids == {"q_target", "q_iid"}
        answered = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers=mutation_headers(
                "durable-answer-run-0001",
                **{"If-Match": f'"{packet["wait_set_revision"]}"'},
            ),
            json={
                "answers": [
                    {"question_id": "q_target", "value": "target"},
                    {"question_id": "q_iid", "value": True},
                ]
            },
        )
        assert answered.status_code == 202, answered.text
        assert answered.json()["status"] == "ACCEPTED"

        terminal = _poll_run(client, run_id, lambda run: run["status"] == "TERMINAL")
        assert terminal["outcome"] == "SUCCEEDED"
        assert terminal["budget_usage"]["trials"] == {"used": 1, "limit": 1}
        result = client.get(f"/v1/runs/{run_id}/result", headers=AUTH)
        assert result.status_code == 200, result.text
        result_body = result.json()
        assert result_body["model_disposition"] == "NO_ELIGIBLE_MODEL"
        assert result_body["backend_id"] == "sklearn"
        assert result_body["backend_version"]
        assert result_body["engine_version"] == "tabular-sklearn.v1"

        reports = client.get(
            f"/v1/runs/{run_id}/outputs",
            headers=AUTH,
            params={"type": "RUN_REPORT"},
        )
        assert reports.status_code == 200, reports.text
        artifact_id = reports.json()["items"][0]["artifact_refs"][0]["artifact_id"]
        trials = client.get(
            f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"type": "TRIAL_RESULT"}
        )
        assert trials.status_code == 200, trials.text
        assert len(trials.json()["items"]) == 1
        assert trials.json()["items"][0]["payload"]["backend_id"] == "sklearn"
        assert trials.json()["items"][0]["payload"]["engine_version"] == "tabular-sklearn.v1"
        task_spec = client.get(
            f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"type": "TASK_SPEC"}
        ).json()["items"][0]["payload"]
        assert task_spec["backend_id"] == "sklearn"
        assert task_spec["engine_version"] == "tabular-sklearn.v1"
        model_card = client.get(
            f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"type": "MODEL_CARD"}
        ).json()["items"][0]["payload"]
        assert model_card["backend_id"] == "sklearn"
        assert model_card["backend_version"]
        ticket = client.post(
            f"/v1/artifacts/{artifact_id}:download",
            headers=mutation_headers("durable-download-report-0001"),
        )
        assert ticket.status_code == 201, ticket.text
        downloaded = client.get(ticket.json()["url"], headers=ticket.json()["required_headers"])
        assert downloaded.status_code == 200, downloaded.text
        assert hashlib.sha256(downloaded.content).hexdigest() == ticket.json()["sha256"]

    reopened = create_app()
    with TestClient(reopened) as client:
        recovered_run = client.get(f"/v1/runs/{run_id}", headers=AUTH)
        assert recovered_run.status_code == 200, recovered_run.text
        assert recovered_run.json()["status"] == "TERMINAL"
        assert client.get(f"/v1/runs/{run_id}/result", headers=AUTH).status_code == 200
