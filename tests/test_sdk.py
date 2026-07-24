from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from automl_sdk import (
    AutoMLClient,
    ConflictError,
    EventCursorExpiredError,
    NotFoundError,
    PreconditionFailedError,
)

from .helpers import create_ready_dataset, create_waiting_run, run_request


def test_sdk_completes_managed_workflow_in_a_small_call_surface(client: TestClient) -> None:
    sdk = AutoMLClient("http://testserver", token="test-tenant-token", http_client=client)
    dataset = sdk.create_dataset(
        name="sdk", filename="sdk.csv", media_type="text/csv", size_bytes=8
    )
    sdk.finalize_dataset(
        dataset["dataset_version_id"],
        upload_id=dataset["upload_id"],
        parts=[{"part_number": 1, "etag": "part"}],
        sha256="b" * 64,
    )
    run = sdk.create_run(run_request(dataset["dataset_version_id"]))
    assert sdk.list_runs()["items"][0]["run_id"] == run["run_id"]
    assert sdk.get_run_stages(run["run_id"])["snapshot_seq"] == run["snapshot_seq"]
    assert sdk.list_run_experiments(run["run_id"])["items"] == []
    with pytest.raises(NotFoundError):
        sdk.get_run_experiment(run["run_id"], "exp_not_registered")
    assert [e["seq"] for e in sdk.iter_run_events(run["run_id"], after_seq=0, limit=2)] == [
        1,
        2,
        3,
        4,
    ]
    question = sdk.wait_for_question(run["run_id"], timeout=1, poll_interval=0)
    command = sdk.answer_and_wait(
        run["run_id"], question, {"q_target": "label"}, timeout=1, poll_interval=0
    )
    result = sdk.wait_for_result(run["run_id"], timeout=1, poll_interval=0)
    report = list(sdk.iter_outputs(run["run_id"], limit=1, types="RUN_REPORT"))[0]
    artifact = sdk.get_artifact(report["artifact_refs"][0]["artifact_id"])
    ticket = sdk.create_artifact_download_ticket(artifact["artifact_id"])
    assert command["status"] == "SUCCEEDED" and result["outcome"] == "SUCCEEDED"
    assert ticket["sha256"] == artifact["sha256"]
    assert [event["seq"] for event in sdk.stream_run_events(run["run_id"], after_seq=0)] == list(
        range(1, 9)
    )

    def fail_on_reconnect(_delay: float) -> None:
        raise AssertionError("a terminal filtered SSE stream must not reconnect")

    guarded_sdk = AutoMLClient(
        "http://testserver",
        token="test-tenant-token",
        http_client=client,
        sleep=fail_on_reconnect,
    )
    filtered = list(
        guarded_sdk.stream_run_events(
            run["run_id"],
            after_seq=0,
            types="output.committed.v1",
        )
    )
    assert len(filtered) == 3
    asyncio.run(client.app.state.store.update_run(run["run_id"], {"retained_from_seq": 3}))
    with pytest.raises(EventCursorExpiredError) as expired:
        list(sdk.iter_run_events(run["run_id"], after_seq=0))
    assert expired.value.lost_event_range == {
        "from_seq": 1,
        "through_seq": 2,
        "historical_events_recoverable": False,
    }
    assert expired.value.recovery["action"] == "GET_RUN_SNAPSHOT"
    with pytest.raises(EventCursorExpiredError) as sse_expired:
        list(guarded_sdk.stream_run_events(run["run_id"], after_seq=0))
    assert sse_expired.value.lost_event_range == expired.value.lost_event_range


def test_sdk_scoped_run_controls(client: TestClient) -> None:
    upload = create_ready_dataset(client, "sdk-control-0001")
    sdk = AutoMLClient("http://testserver", token="test-tenant-token", http_client=client)
    run = sdk.create_run(run_request(upload["dataset_version_id"]))
    try:
        sdk.pause_run(run["run_id"], run_revision=99)
    except PreconditionFailedError:
        pass
    else:
        raise AssertionError("stale run revision should fail")
    sdk.pause_run(run["run_id"], run_revision=run["run_revision"])
    paused = sdk.get_run(run["run_id"])
    assert paused is not None and paused["status"] == "PAUSED"
    sdk.resume_run(run["run_id"], run_revision=paused["run_revision"])
    sdk.cancel_run(run["run_id"])
    try:
        sdk.cancel_run(run["run_id"], idempotency_key="different-cancel-key-0001")
    except ConflictError:
        pass
    else:
        raise AssertionError("a new cancel request against a terminal run should conflict")


def test_sdk_exposes_production_control_plane_helpers(client: TestClient) -> None:
    sdk = AutoMLClient("http://testserver", token="test-tenant-token", http_client=client)
    endpoint = sdk.create_webhook_endpoint(
        url="https://agent.example.test/hooks/automl",
        event_types=["*"],
        idempotency_key="sdk-webhook-create-0001",
    )
    assert endpoint["status"] == "ACTIVE"
    assert len(endpoint["signing_secret"]) == 43
    assert sdk.get_webhook_endpoint(endpoint["webhook_endpoint_id"])["status"] == "ACTIVE"
    rotated = sdk.rotate_webhook_endpoint_secret(
        endpoint["webhook_endpoint_id"], idempotency_key="sdk-webhook-rotate-0001"
    )
    assert rotated["webhook_endpoint_id"] == endpoint["webhook_endpoint_id"]
    assert sdk.list_webhook_endpoints()["items"]

    create_waiting_run(client, "sdk-webhook-delivery-0001")
    deliveries = sdk.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
    delivery = deliveries["items"][0]
    redelivery = sdk.redeliver_webhook_delivery(
        endpoint["webhook_endpoint_id"],
        delivery["delivery_id"],
        idempotency_key="sdk-webhook-redeliver-0001",
    )
    assert redelivery["delivery_id"] == delivery["delivery_id"]

    dataset = create_ready_dataset(client, "sdk-delete-saga-0001")
    deletion = sdk.delete_dataset(dataset["dataset_id"], idempotency_key="sdk-delete-saga-0001")
    assert sdk.get_deletion_job(deletion["deletion_id"]) == deletion
    sdk.delete_webhook_endpoint(
        endpoint["webhook_endpoint_id"], idempotency_key="sdk-webhook-delete-0001"
    )


def test_sdk_finds_blocking_decision_packet_on_second_cursor_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = AutoMLClient("http://unused.invalid", token="test-tenant-token")
    calls: list[dict[str, object]] = []
    opaque_cursor = "opaque-decision-packet-page-2"

    def fake_list_decision_packets(
        run_id: str,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        calls.append({"run_id": run_id, "status": status, "cursor": cursor, "limit": limit})
        if cursor is None:
            return {
                "items": [
                    {
                        "decision_packet_id": f"dp_nonblocking_{index}",
                        "status": "OPEN",
                        "blocking": False,
                    }
                    for index in range(100)
                ],
                "page": {
                    "next_cursor": opaque_cursor,
                    "has_more": True,
                    "high_watermark": 101,
                },
            }
        assert cursor == opaque_cursor
        return {
            "items": [
                {
                    "decision_packet_id": "dp_blocking",
                    "status": "OPEN",
                    "blocking": True,
                }
            ],
            "page": {"next_cursor": None, "has_more": False, "high_watermark": 101},
        }

    monkeypatch.setattr(sdk, "list_decision_packets", fake_list_decision_packets)
    try:
        packet = sdk._first_open_packet("run_cursor_test")
    finally:
        sdk.close()

    assert packet is not None and packet["decision_packet_id"] == "dp_blocking"
    assert calls == [
        {
            "run_id": "run_cursor_test",
            "status": "OPEN",
            "cursor": None,
            "limit": 100,
        },
        {
            "run_id": "run_cursor_test",
            "status": None,
            "cursor": opaque_cursor,
            "limit": None,
        },
    ]


def test_sdk_token_provider_is_called_for_each_request() -> None:
    tokens = iter(["token-one", "token-two"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"schema_version": "1.0"})

    sdk = AutoMLClient(
        "https://automl.example",
        token=lambda: next(tokens),
        transport=httpx.MockTransport(handler),
    )
    try:
        sdk.get_agent_manifest()
        sdk.get_agent_manifest()
    finally:
        sdk.close()

    assert seen == ["Bearer token-one", "Bearer token-two"]


def test_sdk_lists_available_backends_from_the_agent_manifest() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "default_backend_id": "sklearn",
                "backends": [
                    {"backend_id": "autogluon", "available": False},
                    {"backend_id": "sklearn", "available": True},
                    {"backend_id": "tabpfn", "available": True},
                ],
            },
        )

    sdk = AutoMLClient(
        "https://automl.example",
        token="platform-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert [item["backend_id"] for item in sdk.list_backends()] == [
            "autogluon",
            "sklearn",
            "tabpfn",
        ]
        assert [item["backend_id"] for item in sdk.list_backends(available_only=True)] == [
            "sklearn",
            "tabpfn",
        ]
    finally:
        sdk.close()


def test_official_sdk_version_is_accepted_by_manifest(client: TestClient) -> None:
    sdk = AutoMLClient("http://testserver", token="test-tenant-token", http_client=client)
    manifest = sdk.get_agent_manifest()
    assert manifest["python_sdk_compatible_versions"] == ">=0.7,<0.8"


def test_sdk_retries_retryable_statuses_only_when_write_is_idempotent() -> None:
    calls = 0
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"code": "busy"})
        return httpx.Response(202, json={"run_id": "run_1"})

    sdk = AutoMLClient(
        "https://automl.example",
        token="platform-token",
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
    )
    try:
        result = sdk.create_run(
            {"dataset_version_id": "dsv_1"},
            idempotency_key="retryable-run-key-0001",
        )
    finally:
        sdk.close()

    assert result == {"run_id": "run_1"}
    assert calls == 2
    assert slept == [2.0]
