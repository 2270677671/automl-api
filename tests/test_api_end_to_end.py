from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from automl_api.protocol import iso_now

from .helpers import AUTH, create_ready_dataset, create_waiting_run, mutation_headers, run_request


def test_complete_api_workflow_and_reliable_read_models(client: TestClient) -> None:
    upload = create_ready_dataset(client, "flow-0001")
    dataset_response = client.get(
        f"/v1/dataset-versions/{upload['dataset_version_id']}", headers=AUTH
    )
    assert dataset_response.status_code == 200
    dataset_etag = dataset_response.headers["etag"]
    assert (
        client.get(
            f"/v1/dataset-versions/{upload['dataset_version_id']}",
            headers={**AUTH, "If-None-Match": dataset_etag},
        ).status_code
        == 304
    )

    create_headers = mutation_headers("run-idempotency-flow-0001")
    request_body = run_request(upload["dataset_version_id"])
    first = client.post("/v1/runs", headers=create_headers, json=request_body)
    replay = client.post("/v1/runs", headers=create_headers, json=request_body)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"]
    run = first.json()
    run_id = run["run_id"]

    events: list[dict] = []
    response = client.get(
        f"/v1/runs/{run_id}/events",
        headers={**AUTH, "Accept": "application/json"},
        params={"after_seq": 0, "limit": 2},
    )
    while True:
        assert response.status_code == 200, response.text
        page = response.json()
        events.extend(page["items"])
        if page["next_cursor"] is None:
            break
        response = client.get(
            f"/v1/runs/{run_id}/events",
            headers={**AUTH, "Accept": "application/json"},
            params={"cursor": page["next_cursor"]},
        )
    assert [event["seq"] for event in events] == list(range(1, run["snapshot_seq"] + 1))

    outputs = client.get(f"/v1/runs/{run_id}/outputs", headers=AUTH).json()["items"]
    assert [item["type"] for item in outputs] == ["DATA_QUALITY_REPORT"]
    packet = client.get(
        f"/v1/runs/{run_id}/decision-packets", headers=AUTH, params={"status": "OPEN"}
    ).json()["items"][0]
    assert "evidence" not in packet["questions"][0]
    answer_path = f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer"
    answer_body = {"answers": [{"question_id": "q_target", "value": "label"}]}
    stale = client.post(
        answer_path,
        headers=mutation_headers("stale-answer-key-0001", **{"If-Match": '"99"'}),
        json=answer_body,
    )
    stale_replay = client.post(
        answer_path,
        headers=mutation_headers("stale-answer-key-0001", **{"If-Match": '"99"'}),
        json=answer_body,
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "stale_revision"
    assert stale.json() == stale_replay.json()
    assert stale_replay.headers["content-type"].startswith("application/problem+json")

    refreshed_with_stale_key = client.post(
        answer_path,
        headers=mutation_headers(
            "stale-answer-key-0001",
            **{"If-Match": f'"{packet["wait_set_revision"]}"'},
        ),
        json=answer_body,
    )
    assert refreshed_with_stale_key.status_code == 409
    assert refreshed_with_stale_key.json()["code"] == "idempotency_key_reused"

    answer_headers = mutation_headers(
        "answer-idempotency-0001", **{"If-Match": f'"{packet["wait_set_revision"]}"'}
    )
    answer = client.post(answer_path, headers=answer_headers, json=answer_body)
    answer_replay = client.post(answer_path, headers=answer_headers, json=answer_body)
    assert answer.status_code == answer_replay.status_code == 202
    assert answer.json() == answer_replay.json()
    assert answer.json()["status"] == "SUCCEEDED"

    terminal = client.get(f"/v1/runs/{run_id}", headers=AUTH)
    assert terminal.json()["status"] == "TERMINAL"
    terminal_snapshot = terminal.json()
    all_events = client.get(
        f"/v1/runs/{run_id}/events",
        headers={**AUTH, "Accept": "application/json"},
        params={"after_seq": 0, "limit": 100},
    ).json()["items"]
    assert [item["seq"] for item in all_events] == list(
        range(1, terminal_snapshot["snapshot_seq"] + 1)
    )

    result = client.get(f"/v1/runs/{run_id}/result", headers=AUTH)
    assert result.status_code == 200
    assert result.json()["model_disposition"] == "NO_ELIGIBLE_MODEL"
    report = client.get(
        f"/v1/runs/{run_id}/outputs", headers=AUTH, params={"type": "RUN_REPORT"}
    ).json()["items"][0]
    artifact_id = report["artifact_refs"][0]["artifact_id"]
    artifact = client.get(f"/v1/artifacts/{artifact_id}", headers=AUTH)
    assert artifact.status_code == 200
    ticket_headers = mutation_headers("artifact-ticket-key-0001")
    ticket = client.post(f"/v1/artifacts/{artifact_id}:download", headers=ticket_headers)
    ticket_replay = client.post(f"/v1/artifacts/{artifact_id}:download", headers=ticket_headers)
    assert ticket.status_code == ticket_replay.status_code == 201
    assert ticket.json() == ticket_replay.json()
    assert ticket.json()["expires_in_seconds"] == 900

    sse = client.get(
        f"/v1/runs/{run_id}/events",
        headers={**AUTH, "Accept": "text/event-stream", "Last-Event-ID": "0"},
    )
    assert sse.status_code == 200
    assert "id: 1\n" in sse.text
    assert "event: run.completed.v1\n" in sse.text


def test_idempotency_conflict_and_tenant_isolation(client: TestClient) -> None:
    headers = mutation_headers("same-dataset-key-0001")
    body = {
        "name": "first",
        "filename": "data.csv",
        "media_type": "text/csv",
        "size_bytes": 4,
    }
    created = client.post("/v1/datasets", headers=headers, json=body)
    assert created.status_code == 201
    uploading = client.get(
        f"/v1/dataset-versions/{created.json()['dataset_version_id']}", headers=AUTH
    ).json()
    assert "sha256" not in uploading
    conflict = client.post("/v1/datasets", headers=headers, json={**body, "name": "changed"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"
    hidden = client.get(
        f"/v1/dataset-versions/{created.json()['dataset_version_id']}",
        headers={"Authorization": "Bearer another-tenant"},
    )
    assert hidden.status_code == 404


def test_request_correlation_is_stable_across_idempotent_replay(client: TestClient) -> None:
    headers = mutation_headers("correlated-dataset-key-0001", **{"X-Request-ID": "tool-call-42"})
    body = {
        "name": "correlated",
        "filename": "data.csv",
        "media_type": "text/csv",
        "size_bytes": 4,
    }
    created = client.post("/v1/datasets", headers=headers, json=body)
    replay = client.post(
        "/v1/datasets",
        headers={**headers, "X-Request-ID": "transport-retry-43"},
        json=body,
    )

    assert created.status_code == replay.status_code == 201
    assert created.headers["x-correlation-id"] == "tool-call-42"
    assert replay.headers["x-correlation-id"] == "tool-call-42"

    unauthorized = client.get(
        "/v1/agent/manifest",
        headers={"X-Request-ID": "invalid request id with spaces"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["x-correlation-id"].startswith("corr_")
    assert unauthorized.json()["correlation_id"] == unauthorized.headers["x-correlation-id"]


def test_pause_resume_stale_revision_and_cancel_without_if_match(client: TestClient) -> None:
    run = create_waiting_run(client, "control-0001")
    run_id = run["run_id"]
    assert set(run["available_actions"]) == {"ANSWER", "PAUSE", "CANCEL"}
    stale = client.post(
        f"/v1/runs/{run_id}:pause",
        headers=mutation_headers("pause-stale-key-0001", **{"If-Match": '"99"'}),
    )
    assert stale.status_code == 412

    paused = client.post(
        f"/v1/runs/{run_id}:pause",
        headers=mutation_headers(
            "pause-command-key-0001", **{"If-Match": f'"{run["run_revision"]}"'}
        ),
    )
    assert paused.status_code == 202
    paused_run = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert paused_run["status"] == "PAUSED"
    resumed = client.post(
        f"/v1/runs/{run_id}:resume",
        headers=mutation_headers(
            "resume-command-key-0001",
            **{"If-Match": f'"{paused_run["run_revision"]}"'},
        ),
    )
    assert resumed.status_code == 202
    resumed_run = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert resumed_run["status"] == "WAITING_USER"
    assert set(resumed_run["available_actions"]) == {"ANSWER", "PAUSE", "CANCEL"}

    cancel_headers = mutation_headers("cancel-command-key-0001")
    canceled = client.post(f"/v1/runs/{run_id}:cancel", headers=cancel_headers)
    canceled_replay = client.post(f"/v1/runs/{run_id}:cancel", headers=cancel_headers)
    assert canceled.status_code == canceled_replay.status_code == 202
    assert canceled.json() == canceled_replay.json()
    snapshot = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert snapshot["status"] == "TERMINAL"
    assert snapshot["outcome"] == "CANCELED"
    assert snapshot["blocking"] == {"decision_packet_ids": [], "approval_ids": []}
    assert all(stage["status"] in {"COMPLETED", "CANCELED"} for stage in snapshot["stages"])
    packets = client.get(f"/v1/runs/{run_id}/decision-packets", headers=AUTH).json()["items"]
    assert all(packet["status"] != "OPEN" for packet in packets)
    assert client.get(f"/v1/runs/{run_id}/result", headers=AUTH).json()["outcome"] == "CANCELED"
    new_cancel = client.post(
        f"/v1/runs/{run_id}:cancel", headers=mutation_headers("new-cancel-key-0001")
    )
    assert new_cancel.status_code == 409


def test_problem_documents_and_canonical_contract(client: TestClient) -> None:
    unauthorized = client.get("/v1/runs/run_missing")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["content-type"].startswith("application/problem+json")
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    missing_route = client.get("/v1/not-a-route", headers=AUTH)
    assert missing_route.status_code == 404
    assert missing_route.json()["code"] == "not_found"
    assert missing_route.headers["content-type"].startswith("application/problem+json")
    invalid = client.post(
        "/v1/datasets",
        headers=mutation_headers("validation-key-0001"),
        json={"name": "missing fields"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_failed"
    unsupported = client.get("/v1/webhook-endpoints", headers=AUTH)
    assert unsupported.status_code == 501
    assert unsupported.json()["code"] == "capability_not_implemented"
    contract = client.get("/openapi.yaml")
    assert contract.status_code == 200
    assert contract.text.startswith("openapi: 3.1.0")


def test_run_etag_covers_snapshot_seq_without_changing_control_revision(
    client: TestClient,
) -> None:
    run = create_waiting_run(client, "etag-0001")
    run_id = run["run_id"]
    first = client.get(f"/v1/runs/{run_id}", headers=AUTH)
    first_etag = first.headers["etag"]
    asyncio.run(
        client.app.state.store.append_event(
            run_id,
            {
                "schema_version": "1.0",
                "occurred_at": iso_now(),
                "type": "run.progress_updated.v1",
                "payload": {"progress": run["progress"]},
                "links": {"run": f"/v1/runs/{run_id}"},
            },
        )
    )
    changed = client.get(f"/v1/runs/{run_id}", headers={**AUTH, "If-None-Match": first_etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != first_etag
    assert changed.json()["run_revision"] == run["run_revision"]
    assert changed.json()["snapshot_seq"] == run["snapshot_seq"] + 1


def test_status_filtered_run_cursor_uses_keyset_not_mutable_offset(
    client: TestClient,
) -> None:
    first_run = create_waiting_run(client, "page-0001")
    second_run = create_waiting_run(client, "page-0002")
    first_page = client.get(
        "/v1/runs", headers=AUTH, params={"status": "WAITING_USER", "limit": 1}
    ).json()
    assert first_page["items"][0]["run_id"] == first_run["run_id"]
    client.post(
        f"/v1/runs/{first_run['run_id']}:cancel",
        headers=mutation_headers("page-cancel-key-0001"),
    )
    second_page = client.get(
        "/v1/runs", headers=AUTH, params={"cursor": first_page["page"]["next_cursor"]}
    )
    assert second_page.status_code == 200
    assert [item["run_id"] for item in second_page.json()["items"]] == [second_run["run_id"]]
    invalid_filter = client.get("/v1/runs", headers=AUTH, params={"status": "BOGUS"})
    assert invalid_filter.status_code == 400
