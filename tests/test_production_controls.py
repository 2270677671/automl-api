from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from automl_api.app import _require_human_approval, create_app
from automl_api.auth import Principal
from automl_api.errors import APIProblem
from automl_api.production import ProductionSettings
from automl_api.storage import LocalBlobStore
from automl_api.store import InMemoryStore

from .helpers import AUTH, create_ready_dataset, create_waiting_run, mutation_headers, run_request


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


def _upload_csv(client: TestClient, content: bytes, suffix: str) -> str:
    created = client.post(
        "/v1/datasets",
        headers=mutation_headers(f"production-dataset-{suffix}"),
        json={
            "name": "production-controls",
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
        headers=mutation_headers(f"production-finalize-{suffix}"),
        json={
            "upload_id": upload["upload_id"],
            "parts": [{"part_number": 1, "etag": uploaded.headers["etag"]}],
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert finalized.status_code == 202, finalized.text
    return str(upload["dataset_version_id"])


def test_webhook_outbox_and_deletion_saga_are_api_visible(client: TestClient) -> None:
    created = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("webhook-create-production-0001"),
        json={"url": "https://agent.example.test/hooks/automl", "event_types": ["*"]},
    )
    assert created.status_code == 201, created.text
    endpoint = created.json()
    assert len(endpoint["signing_secret"]) == 43

    create_waiting_run(client, "webhook-outbox-0001")
    deliveries = client.get(
        f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}/deliveries",
        headers=AUTH,
    )
    assert deliveries.status_code == 200, deliveries.text
    delivery_items = deliveries.json()["items"]
    assert delivery_items
    assert {item["status"] for item in delivery_items} == {"PENDING"}

    delivery_id = delivery_items[0]["delivery_id"]
    redelivery = client.post(
        f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}/deliveries/{delivery_id}:redeliver",
        headers=mutation_headers("webhook-redeliver-production-0001"),
    )
    assert redelivery.status_code == 202, redelivery.text
    assert redelivery.json()["delivery_id"] == delivery_id

    dataset_id = create_ready_dataset(client, "delete-saga-0001")["dataset_id"]
    deleted = client.delete(
        f"/v1/datasets/{dataset_id}",
        headers=mutation_headers("delete-saga-production-0001"),
    )
    assert deleted.status_code == 202, deleted.text
    deletion = deleted.json()
    assert deletion["status"] == "COMPLETED"
    fetched = client.get(f"/v1/deletions/{deletion['deletion_id']}", headers=AUTH)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == deletion


def test_local_deletion_removes_dataset_bytes_and_revokes_dataset_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    application = create_app()
    with TestClient(application) as client:
        version_id = _upload_csv(client, b"feature,target\n1,0\n2,1\n", "physical-delete-0001")
        version = asyncio.run(application.state.store.get_dataset_version(version_id))
        assert version is not None
        source_path = application.state.blob_store.path_for_key(version["blob_key"])
        assert source_path.is_file()

        deleted = client.delete(
            f"/v1/datasets/{version['dataset_id']}",
            headers=mutation_headers("physical-dataset-delete-0001"),
        )
        assert deleted.status_code == 202, deleted.text
        assert deleted.json()["status"] == "COMPLETED"
        assert not source_path.exists()

        inaccessible = client.get(f"/v1/dataset-versions/{version_id}", headers=AUTH)
        assert inaccessible.status_code == 404, inaccessible.text


def test_dataset_deletion_invalidates_an_existing_artifact_download_ticket(tmp_path: Path) -> None:
    state = InMemoryStore()
    blob_store = LocalBlobStore(tmp_path / "objects", ticket_secret=b"test-secret" * 4)
    application = create_app(state, blob_store=blob_store)
    content = b"feature,target\n1,0\n2,1\n3,0\n4,1\n"

    with TestClient(application) as client:
        version_id = _upload_csv(client, content, "ticket-revocation-0001")
        created = client.post(
            "/v1/runs",
            headers=mutation_headers("ticket-revocation-run-0001"),
            json=run_request(version_id),
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        packet = client.get(
            f"/v1/runs/{run_id}/decision-packets",
            headers=AUTH,
            params={"status": "OPEN"},
        ).json()["items"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers=mutation_headers(
                "ticket-revocation-answer-0001",
                **{"If-Match": f'"{packet["wait_set_revision"]}"'},
            ),
            json={"answers": [{"question_id": "q_target", "value": "target"}]},
        )
        assert answered.status_code == 202, answered.text

        outputs = client.get(
            f"/v1/runs/{run_id}/outputs",
            headers=AUTH,
            params={"type": "RUN_REPORT"},
        )
        assert outputs.status_code == 200, outputs.text
        artifact_id = outputs.json()["items"][0]["artifact_refs"][0]["artifact_id"]
        issued = client.post(
            f"/v1/artifacts/{artifact_id}:download",
            headers=mutation_headers("ticket-revocation-download-0001"),
        )
        assert issued.status_code == 201, issued.text
        ticket = issued.json()
        downloaded = client.get(ticket["url"], headers=ticket["required_headers"])
        assert downloaded.status_code == 200, downloaded.text

        version = asyncio.run(state.get_dataset_version(version_id))
        assert version is not None
        deleted = client.delete(
            f"/v1/datasets/{version['dataset_id']}",
            headers=mutation_headers("ticket-revocation-delete-0001"),
        )
        assert deleted.status_code == 202, deleted.text
        assert deleted.json()["status"] == "COMPLETED"

        revoked = client.get(ticket["url"], headers=ticket["required_headers"])
        assert revoked.status_code == 404, revoked.text
        assert revoked.json()["code"] == "not_found"


def test_approval_expired_returns_conflict_through_http_decide_route() -> None:
    state = InMemoryStore()
    application = create_app(state)

    with TestClient(application) as client:
        run = create_waiting_run(client, "expired-approval-0001")
        run_id = run["run_id"]
        waiting = asyncio.run(
            state.update_run(
                run_id,
                {
                    "status": "WAITING_APPROVAL",
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                },
            )
        )
        approval = asyncio.run(
            state.create_approval(
                run_id,
                {
                    "tenant_id": waiting["tenant_id"],
                    "run_revision": waiting["run_revision"],
                    "evidence_version": 1,
                    "kind": "PRODUCTION_DEPLOY",
                    "status": "OPEN",
                    "evidence_refs": [],
                    "decision_reason": None,
                    "created_at": "2000-01-01T00:00:00Z",
                    "expires_at": "2000-01-01T00:00:00Z",
                },
            )
        )

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}:decide",
            headers=mutation_headers(
                "expired-approval-decide-0001",
                **{"If-Match": '"1"'},
            ),
            json={
                "decision": "APPROVE",
                "reason": "The stale approval must not be accepted.",
                "evidence_version": 1,
            },
        )
        assert decided.status_code == 409, decided.text
        assert decided.json()["code"] == "approval_expired"

        expired = asyncio.run(state.get_approval(run_id, approval["approval_id"]))
        assert expired is not None
        assert expired["status"] == "EXPIRED"
        assert expired["evidence_version"] == 2


def test_formal_profile_never_claims_ready_without_external_runtime_adapters() -> None:
    settings = ProductionSettings.from_env(
        {
            "AUTOML_DEPLOYMENT_PROFILE": "production",
            "AUTOML_JWKS_URL": "https://identity.example.test/jwks",
            "AUTOML_DATABASE_URL": "postgresql://automl@example.test:5432/automl",
            "AUTOML_POSTGRES_RLS_REQUIRED": "true",
            "AUTOML_OBJECT_STORE": "s3",
            "AUTOML_S3_BUCKET": "automl",
            "AUTOML_KMS_KEY_ID": "kms-key",
            "AUTOML_DLP_MODE": "strict",
            "AUTOML_AGENT_CONTEXT_FIELD_ALLOWLIST": "run,objective",
            "AUTOML_WEBHOOK_DISPATCH_MODE": "outbox",
            "AUTOML_WEBHOOK_SIGNING_REQUIRED": "true",
            "AUTOML_DELETION_SAGA_ENABLED": "true",
            "AUTOML_MODEL_REGISTRY_MODE": "enabled",
            "AUTOML_WORKER_ISOLATION": "container",
            "AUTOML_REQUIRE_WORKER_ISOLATION": "true",
        }
    )
    runtime = next(check for check in settings.checks if check.name == "runtime_adapters")
    assert runtime.ok is False
    assert settings.ready is False


def test_production_approval_requires_a_human_principal() -> None:
    agent = Principal(
        subject="agent",
        tenant_id="tenant",
        actor_type="agent",
        authentication_mode="production",
    )
    with pytest.raises(APIProblem) as denied:
        _require_human_approval(agent)
    assert denied.value.code == "human_approval_required"

    _require_human_approval(
        Principal(
            subject="human",
            tenant_id="tenant",
            actor_type="human",
            authentication_mode="production",
        )
    )


def test_production_control_resources_survive_a_durable_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app()) as client:
        created = client.post(
            "/v1/webhook-endpoints",
            headers=mutation_headers("durable-webhook-create-0001"),
            json={"url": "https://agent.example.test/hooks/automl", "event_types": ["*"]},
        )
        assert created.status_code == 201, created.text
        endpoint_id = created.json()["webhook_endpoint_id"]

    with TestClient(create_app()) as restarted:
        listed = restarted.get("/v1/webhook-endpoints", headers=AUTH)
        assert listed.status_code == 200, listed.text
        assert [item["webhook_endpoint_id"] for item in listed.json()["items"]] == [endpoint_id]


def test_production_deploy_requires_approval_before_model_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOML_STATE_DIR", str(tmp_path / "state"))
    rows = ["feature_a,feature_b,target"]
    rows.extend(f"{index},{index % 5},{index % 2}" for index in range(80))
    content = ("\n".join(rows) + "\n").encode()

    application = create_app()
    with TestClient(application) as client:
        endpoint = client.post(
            "/v1/webhook-endpoints",
            headers=mutation_headers("approval-completion-webhook-0001"),
            json={
                "url": "https://agent.example.test/hooks/automl",
                "event_types": ["run.completed.v1"],
            },
        )
        assert endpoint.status_code == 201, endpoint.text
        dataset_version_id = _upload_csv(client, content, "approval-0001")
        request = run_request(dataset_version_id)
        request["autonomy"]["production_deploy"] = "REQUIRE_APPROVAL"
        request["budget"]["max_trials"] = 1
        created = client.post(
            "/v1/runs",
            headers=mutation_headers("approval-create-run-0001"),
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
        answered = client.post(
            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer",
            headers=mutation_headers(
                "approval-answer-run-0001",
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

        approval_run = _poll_run(client, run_id, lambda run: run["status"] == "WAITING_APPROVAL")
        assert approval_run["blocking"]["approval_ids"]
        result_before_approval = client.get(f"/v1/runs/{run_id}/result", headers=AUTH)
        assert result_before_approval.status_code == 409

        approvals = client.get(f"/v1/runs/{run_id}/approvals", headers=AUTH)
        assert approvals.status_code == 200, approvals.text
        approval = approvals.json()["items"][0]
        assert approval["status"] == "OPEN"

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}:decide",
            headers=mutation_headers(
                "approval-decide-run-0001",
                **{"If-Match": f'"{approval["evidence_version"]}"'},
            ),
            json={
                "decision": "APPROVE",
                "reason": "Offline evidence accepted for controlled production candidate.",
                "evidence_version": approval["evidence_version"],
            },
        )
        assert decided.status_code == 202, decided.text

        terminal = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
        assert terminal["status"] == "TERMINAL"
        assert terminal["progress"]["percent"] == 100
        stored_terminal = asyncio.run(application.state.store.get_run(run_id))
        assert stored_terminal is not None and stored_terminal["execution_step"] == "COMPLETED"
        events = client.get(f"/v1/runs/{run_id}/events", headers=AUTH).json()["items"]
        assert events[-1]["type"] == "run.completed.v1"
        deliveries = client.get(
            f"/v1/webhook-endpoints/{endpoint.json()['webhook_endpoint_id']}/deliveries",
            headers=AUTH,
        ).json()["items"]
        assert any(item["event_type"] == "run.completed.v1" for item in deliveries)
        result = client.get(f"/v1/runs/{run_id}/result", headers=AUTH)
        assert result.status_code == 200, result.text
        result_body = result.json()
        assert result_body["model_disposition"] == "ELIGIBLE_MODEL_AVAILABLE"
        model_href = result_body["eligible_model"]["href"]
        model = client.get(model_href, headers=AUTH)
        assert model.status_code == 200, model.text
        model_body = model.json()
        assert model_body["status"] == "ELIGIBLE_CANDIDATE"
        assert model_body["signature"]["inputs"]
