from __future__ import annotations

import asyncio
import hashlib
import json
import threading
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

        original_write = application.state.store._write_checkpoint
        terminal_checkpoint_failed = False

        def fail_first_terminal_checkpoint(snapshot, **kwargs):
            nonlocal terminal_checkpoint_failed
            if (
                not terminal_checkpoint_failed
                and snapshot["runs"][run_id].get("status") == "TERMINAL"
            ):
                terminal_checkpoint_failed = True
                raise OSError("synthetic terminal checkpoint failure")
            return original_write(snapshot, **kwargs)

        application.state.store._write_checkpoint = fail_first_terminal_checkpoint

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
        assert terminal_checkpoint_failed is True
        assert terminal["outcome"] == "SUCCEEDED"
        assert terminal["budget_usage"]["trials"] == {"used": 1, "limit": 1}
        result = client.get(f"/v1/runs/{run_id}/result", headers=AUTH)
        assert result.status_code == 200, result.text
        result_body = result.json()
        assert result_body["model_disposition"] == "NO_ELIGIBLE_MODEL"
        assert result_body["backend_id"] == "sklearn"
        assert result_body["backend_version"]
        assert result_body["engine_version"] == "tabular-sklearn.v1"
        assert result_body["visualization_refs"]
        assert {item["media_type"] for item in result_body["visualization_refs"]} == {"image/png"}

        evaluation = client.get(
            f"/v1/runs/{run_id}/outputs",
            headers=AUTH,
            params={"type": "EVALUATION_REPORT"},
        ).json()["items"][0]
        assert evaluation["payload"]["visualization_status"] in {"COMPLETE", "PARTIAL"}
        assert evaluation["artifact_refs"] == result_body["visualization_refs"]

        stored_outputs = asyncio.run(application.state.store.list_outputs(run_id=run_id))
        output_types = [item["type"] for item in stored_outputs]
        for singleton_type in (
            "TASK_SPEC",
            "SPLIT_MANIFEST",
            "BASELINE_RESULT",
            "EVALUATION_REPORT",
            "MODEL_CARD",
            "RUN_REPORT",
        ):
            assert output_types.count(singleton_type) == 1
        trial_numbers = [
            item["payload"]["trial_number"]
            for item in stored_outputs
            if item["type"] == "TRIAL_RESULT"
        ]
        assert len(trial_numbers) == len(set(trial_numbers))
        stored_artifacts = asyncio.run(application.state.store.list_artifacts(run_id=run_id))
        artifact_keys = [(item["kind"], item["sha256"]) for item in stored_artifacts]
        assert len(artifact_keys) == len(set(artifact_keys))
        plot = evaluation["artifact_refs"][0]
        plot_ticket = client.post(
            f"/v1/artifacts/{plot['artifact_id']}:download",
            headers=mutation_headers("durable-download-plot-0001"),
        )
        assert plot_ticket.status_code == 201, plot_ticket.text
        plot_bytes = client.get(
            plot_ticket.json()["url"], headers=plot_ticket.json()["required_headers"]
        ).content
        assert plot_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(plot_bytes).hexdigest() == plot["sha256"]

        stage_events = client.get(
            f"/v1/runs/{run_id}/events",
            headers=AUTH,
            params={"types": "run.stage_completed.v1", "limit": 100},
        ).json()["items"]
        assert [item["payload"]["phase"] for item in stage_events] == [
            "INGEST",
            "PROFILE",
            "PLAN",
            "TRAIN",
            "EVALUATE",
            "PACKAGE",
        ]
        assert [item["payload"]["next_stage_ready"] for item in stage_events] == [
            True,
            False,
            True,
            True,
            True,
            False,
        ]
        assert stage_events[-1]["payload"]["reason"] == "WORKFLOW_COMPLETED"
        plan_event = next(item for item in stage_events if item["payload"]["phase"] == "PLAN")
        assert {ref["type"] for ref in plan_event["payload"]["output_refs"]} == {
            "TASK_SPEC",
            "SPLIT_MANIFEST",
        }
        package_event = stage_events[-1]
        assert package_event["run_revision"] == terminal["run_revision"]

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
        report_document = json.loads(downloaded.content)
        assert (
            report_document["evaluation"]["visualization_status"]
            == evaluation["payload"]["visualization_status"]
        )
        assert (
            report_document["evaluation"]["visualizations"]
            == evaluation["payload"]["visualizations"]
        )

    reopened = create_app()
    with TestClient(reopened) as client:
        recovered_run = client.get(f"/v1/runs/{run_id}", headers=AUTH)
        assert recovered_run.status_code == 200, recovered_run.text
        assert recovered_run.json()["status"] == "TERMINAL"
        assert client.get(f"/v1/runs/{run_id}/result", headers=AUTH).status_code == 200


def test_plan_completion_waits_for_positive_class_and_plan_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    rows = ["feature_a,feature_b,target"]
    rows.extend(f"{index},{index % 7},{'yes' if index % 2 else 'no'}" for index in range(80))
    content = ("\n".join(rows) + "\n").encode()

    with TestClient(create_app()) as client:
        dataset_version_id = _upload_csv(client, content)
        request = run_request(dataset_version_id)
        request["objective"] = {
            "backend_id": "sklearn",
            "target_column": "target",
            "task_type": "BINARY_CLASSIFICATION",
            "iid_confirmed": True,
        }
        request["budget"]["max_trials"] = 1
        created = client.post(
            "/v1/runs",
            headers=mutation_headers("durable-positive-class-run-0001"),
            json=request,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]

        _poll_run(client, run_id, lambda run: run["status"] == "WAITING_USER")
        packet = client.get(
            f"/v1/runs/{run_id}/decision-packets",
            headers=AUTH,
            params={"status": "OPEN"},
        ).json()["items"][0]
        assert [question["question_id"] for question in packet["questions"]] == ["q_positive_class"]
        before_answer = client.get(
            f"/v1/runs/{run_id}/events",
            headers=AUTH,
            params={"types": "run.stage_completed.v1", "limit": 100},
        ).json()["items"]
        assert [item["payload"]["phase"] for item in before_answer] == ["INGEST", "PROFILE"]
        assert before_answer[-1]["payload"]["next_stage_ready"] is True
        assert before_answer[-1]["payload"]["reason"] is None

        answered = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers=mutation_headers(
                "durable-positive-class-answer-0001",
                **{"If-Match": f'"{packet["wait_set_revision"]}"'},
            ),
            json={"answers": [{"question_id": "q_positive_class", "value": "yes"}]},
        )
        assert answered.status_code == 202, answered.text
        _poll_run(client, run_id, lambda run: run["status"] == "TERMINAL")

        stage_events = client.get(
            f"/v1/runs/{run_id}/events",
            headers=AUTH,
            params={"types": "run.stage_completed.v1", "limit": 100},
        ).json()["items"]
        plan_event = next(item for item in stage_events if item["payload"]["phase"] == "PLAN")
        assert {ref["type"] for ref in plan_event["payload"]["output_refs"]} == {
            "TASK_SPEC",
            "SPLIT_MANIFEST",
        }


def test_stage_callbacks_are_durable_barriers_before_later_computation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    application = create_app()
    registry = application.state.workflow.backend_registry
    original_run = registry.run
    plan_published = threading.Event()
    allow_training = threading.Event()
    training_published = threading.Event()
    allow_evaluation = threading.Event()

    def gated_run(*args, **kwargs):
        callback = kwargs["stage_callback"]

        def gated_callback(phase: str, snapshot: dict[str, Any]) -> None:
            callback(phase, snapshot)
            if phase == "PLAN":
                plan_published.set()
                assert allow_training.wait(10)
            elif phase == "TRAIN":
                training_published.set()
                assert allow_evaluation.wait(10)

        kwargs["stage_callback"] = gated_callback
        return original_run(*args, **kwargs)

    registry.run = gated_run
    rows = ["feature_a,feature_b,target"]
    rows.extend(f"{index},{index % 7},{index % 2}" for index in range(80))
    content = ("\n".join(rows) + "\n").encode()

    try:
        with TestClient(application) as client:
            dataset_version_id = _upload_csv(client, content)
            request = run_request(dataset_version_id)
            request["objective"] = {
                "backend_id": "sklearn",
                "target_column": "target",
                "task_type": "BINARY_CLASSIFICATION",
                "positive_class": 1,
                "iid_confirmed": True,
            }
            request["budget"]["max_trials"] = 1
            created = client.post(
                "/v1/runs",
                headers=mutation_headers("durable-stage-barriers-0001"),
                json=request,
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]

            assert plan_published.wait(10)
            plan_outputs = client.get(
                f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"limit": 100}
            ).json()["items"]
            assert {item["type"] for item in plan_outputs} >= {
                "TASK_SPEC",
                "SPLIT_MANIFEST",
            }
            assert "BASELINE_RESULT" not in {item["type"] for item in plan_outputs}
            plan_events = client.get(
                f"/v1/runs/{run_id}/events",
                headers=AUTH,
                params={"types": "run.stage_completed.v1", "limit": 100},
            ).json()["items"]
            assert [item["payload"]["phase"] for item in plan_events] == [
                "INGEST",
                "PROFILE",
                "PLAN",
            ]

            allow_training.set()
            assert training_published.wait(10)
            training_outputs = client.get(
                f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"limit": 100}
            ).json()["items"]
            assert {item["type"] for item in training_outputs} >= {
                "BASELINE_RESULT",
                "TRIAL_RESULT",
            }
            assert "EVALUATION_REPORT" not in {item["type"] for item in training_outputs}
            training_events = client.get(
                f"/v1/runs/{run_id}/events",
                headers=AUTH,
                params={"types": "run.stage_completed.v1", "limit": 100},
            ).json()["items"]
            assert [item["payload"]["phase"] for item in training_events] == [
                "INGEST",
                "PROFILE",
                "PLAN",
                "TRAIN",
            ]

            allow_evaluation.set()
            terminal = _poll_run(client, run_id, lambda run: run["status"] == "TERMINAL")
            assert terminal["outcome"] == "SUCCEEDED"
    finally:
        allow_training.set()
        allow_evaluation.set()


def test_failed_run_event_remains_readable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    rows = ["feature_a,feature_b,target"]
    rows.extend(f"{index},{index % 7},{index * 0.5}" for index in range(80))
    content = ("\n".join(rows) + "\n").encode()

    application = create_app()
    with TestClient(application) as client:
        endpoint = client.post(
            "/v1/webhook-endpoints",
            headers=mutation_headers("durable-failed-run-webhook-0001"),
            json={
                "url": "https://agent.example.test/hooks/automl",
                "event_types": ["run.failed.v1"],
            },
        )
        assert endpoint.status_code == 201, endpoint.text
        dataset_version_id = _upload_csv(client, content)
        request = run_request(dataset_version_id)
        request["objective"] = {
            "backend_id": "sklearn",
            "target_column": "target",
            "task_type": "REGRESSION",
            "iid_confirmed": True,
            "primary_metric": "not-a-regression-metric",
        }
        request["callback_url"] = endpoint.json()["url"]
        request["webhook_endpoint_ids"] = [endpoint.json()["webhook_endpoint_id"]]
        created = client.post(
            "/v1/runs",
            headers=mutation_headers("durable-create-failed-run-0001"),
            json=request,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        terminal = _poll_run(client, run_id, lambda run: run["status"] == "TERMINAL")
        assert terminal["outcome"] == "FAILED"

        stored_events = asyncio.run(client.app.state.store.get_events(run_id))
        assert stored_events[-1]["payload"]["retriable"] is False
        assert stored_events[-1]["run_revision"] == terminal["run_revision"]
        deliveries = asyncio.run(
            application.state.store.list_webhook_deliveries(endpoint.json()["webhook_endpoint_id"])
        )
        assert len(deliveries) == 1
        assert deliveries[0]["event_type"] == "run.failed.v1"
        assert deliveries[0]["event_id"] == stored_events[-1]["event_id"]
        with client.app.state.store._lock:
            client.app.state.store._events[run_id][-1]["payload"].pop("retriable")

        events = client.get(
            f"/v1/runs/{run_id}/events",
            headers={**AUTH, "Accept": "application/json"},
            params={"after_seq": 0, "limit": 100},
        )
        assert events.status_code == 200, events.text
        failed = events.json()["items"][-1]
        assert failed["type"] == "run.failed.v1"
        assert failed["payload"] == {
            "outcome": "FAILED",
            "failure_code": "INVALID_TARGET",
            "retriable": False,
            "result_href": f"/v1/runs/{run_id}/result",
            "phase": terminal["phase"],
        }
