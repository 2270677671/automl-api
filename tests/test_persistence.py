from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from automl_api.auth import Principal
from automl_api.durable_workflow import DurableWorkflowService
from automl_api.persistence import JobFenceError, SqliteStore
from automl_api.storage import SyntheticBlobStore
from automl_api.store import IdempotencyState
from automl_api.workflow import WorkflowService, _stages


async def _create_resource_graph(store: SqliteStore) -> dict[str, str]:
    dataset, version = await store.create_dataset_with_version(
        {"tenant_id": "tenant_1", "name": "durable"},
        {"tenant_id": "tenant_1", "status": "READY", "revision": 1},
    )
    run = await store.create_run(
        {
            "tenant_id": "tenant_1",
            "dataset_version_id": version["dataset_version_id"],
            "status": "RUNNING",
            "run_revision": 1,
            "snapshot_seq": 0,
        }
    )
    event = await store.append_event(
        run["run_id"],
        {
            "event_id": store.new_id("event"),
            "type": "run.phase_changed.v1",
            "payload": {"phase": "PROFILE"},
        },
    )
    output = await store.create_output(
        run["run_id"],
        {"tenant_id": "tenant_1", "type": "DATA_QUALITY_REPORT", "created_seq": 1},
    )
    packet = await store.create_decision_packet(
        run["run_id"],
        {
            "tenant_id": "tenant_1",
            "wait_set_id": store.new_id("wait_set"),
            "wait_set_revision": 1,
            "status": "OPEN",
        },
    )
    command = await store.create_command(
        {"tenant_id": "tenant_1", "run_id": run["run_id"], "status": "ACCEPTED"}
    )
    result = await store.set_result(run["run_id"], {"outcome": "SUCCEEDED", "summary": "durable"})
    artifact = await store.create_artifact(
        {"tenant_id": "tenant_1", "run_id": run["run_id"], "state": "COMMITTED"}
    )
    return {
        "dataset_id": dataset["dataset_id"],
        "dataset_version_id": version["dataset_version_id"],
        "run_id": run["run_id"],
        "event_id": event["event_id"],
        "output_id": output["output_id"],
        "decision_packet_id": packet["decision_packet_id"],
        "command_id": command["command_id"],
        "result_manifest_id": result["result_manifest_id"],
        "artifact_id": artifact["artifact_id"],
    }


def test_sqlite_store_restores_resources_idempotency_and_counters(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "automl.db"
        store = SqliteStore(database)
        ids = await _create_resource_graph(store)
        reserved = await store.begin_idempotent_request(
            "tenant_1:createRun", "durable-idempotency-key", "fingerprint"
        )
        assert reserved.state is IdempotencyState.NEW
        original_response = await store.complete_idempotent_request(
            "tenant_1:createRun",
            "durable-idempotency-key",
            "fingerprint",
            status_code=202,
            body={"run_id": ids["run_id"]},
            headers={"ETag": '"snapshot"'},
        )
        last_run_id = store.new_id("run")
        await store.close()

        restored = SqliteStore(database)
        assert (await restored.get_dataset(ids["dataset_id"]))["name"] == "durable"
        assert (await restored.get_dataset_version(ids["dataset_version_id"]))["status"] == "READY"
        assert (await restored.get_run(ids["run_id"]))["snapshot_seq"] == 1
        assert (await restored.get_events(ids["run_id"]))[0]["event_id"] == ids["event_id"]
        assert (await restored.get_output(ids["run_id"], ids["output_id"])) is not None
        assert (
            await restored.get_decision_packet(ids["run_id"], ids["decision_packet_id"])
        ) is not None
        assert (await restored.get_command(ids["command_id"])) is not None
        assert (await restored.get_result(ids["run_id"]))["result_manifest_id"] == ids[
            "result_manifest_id"
        ]
        assert (await restored.get_artifact(ids["artifact_id"])) is not None

        replay = await restored.begin_idempotent_request(
            "tenant_1:createRun", "durable-idempotency-key", "fingerprint"
        )
        assert replay.state is IdempotencyState.REPLAY
        assert replay.response == original_response

        next_run_id = restored.new_id("run")
        assert int(next_run_id.rsplit("_", 1)[1]) == int(last_run_id.rsplit("_", 1)[1]) + 1
        next_event = await restored.append_event(
            ids["run_id"],
            {
                "event_id": restored.new_id("event"),
                "type": "run.progress_updated.v1",
                "payload": {"percent": 50},
            },
        )
        assert next_event["seq"] == 2
        await restored.close()

    asyncio.run(scenario())


def test_execution_job_reclaims_expired_lease_and_fences_stale_workers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "jobs.db"
        store = SqliteStore(database)
        run = await store.create_run(
            {"tenant_id": "tenant_1", "status": "QUEUED", "run_revision": 1}
        )
        run_id = run["run_id"]
        created = await store.create_execution_job(
            run_id,
            workflow_step="PROFILE",
            checkpoint={"dataset_sha256": "a" * 64},
            max_attempts=4,
        )
        assert created["status"] == "READY"

        first = await store.claim_execution_job("worker-a", lease_seconds=0.05)
        assert first is not None
        assert first["lease_generation"] == 1
        assert await store.claim_execution_job("worker-b", lease_seconds=1) is None

        await asyncio.sleep(0.08)
        second = await store.claim_execution_job("worker-b", lease_seconds=1)
        assert second is not None
        assert second["run_id"] == run_id
        assert second["lease_generation"] == 2
        with pytest.raises(JobFenceError):
            await store.complete_execution_job(
                run_id,
                lease_generation=first["lease_generation"],
                control_epoch=first["control_epoch"],
            )

        woken = await store.wake_execution_job(run_id)
        assert woken["status"] == "READY"
        assert woken["control_epoch"] == second["control_epoch"] + 1
        with pytest.raises(JobFenceError):
            await store.checkpoint_execution_job(
                run_id,
                lease_generation=second["lease_generation"],
                control_epoch=second["control_epoch"],
                workflow_step="RESOLVE_TASK",
                checkpoint={},
            )

        third = await store.claim_execution_job("worker-c", lease_seconds=1)
        assert third is not None
        retried = await store.retry_execution_job(
            run_id,
            lease_generation=third["lease_generation"],
            control_epoch=third["control_epoch"],
            delay_seconds=0,
            error="synthetic transient failure",
        )
        assert retried["status"] == "RETRY"
        fourth = await store.claim_execution_job("worker-d", lease_seconds=1)
        assert fourth is not None
        completed = await store.complete_execution_job(
            run_id,
            lease_generation=fourth["lease_generation"],
            control_epoch=fourth["control_epoch"],
        )
        assert completed["status"] == "COMPLETED"
        await store.close()

        reopened = SqliteStore(database)
        assert (await reopened.get_execution_job(run_id))["status"] == "COMPLETED"
        await reopened.close()

    asyncio.run(scenario())


def test_execution_job_renewal_is_fenced_and_cannot_overwrite_new_owner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "job-renewal.db")
        run = await store.create_run(
            {"tenant_id": "tenant_1", "status": "QUEUED", "run_revision": 1}
        )
        run_id = str(run["run_id"])
        await store.create_execution_job(run_id, workflow_step="TRAIN")

        first = await store.claim_execution_job("worker-a", lease_seconds=0.05)
        assert first is not None
        await asyncio.sleep(0.02)
        renewed = await store.renew_execution_job(
            run_id,
            lease_generation=int(first["lease_generation"]),
            control_epoch=int(first["control_epoch"]),
            lease_seconds=0.2,
        )
        assert renewed["status"] == "LEASED"
        assert renewed["lease_owner"] == "worker-a"
        assert renewed["lease_expires_at"] > first["lease_expires_at"]

        await asyncio.sleep(0.06)
        assert await store.claim_execution_job("worker-b", lease_seconds=1) is None

        await store.wake_execution_job(run_id)
        second = await store.claim_execution_job("worker-b", lease_seconds=1)
        assert second is not None
        second_expiry = second["lease_expires_at"]
        with pytest.raises(JobFenceError):
            await store.renew_execution_job(
                run_id,
                lease_generation=int(first["lease_generation"]),
                control_epoch=int(first["control_epoch"]),
                lease_seconds=30,
            )

        current = await store.get_execution_job(run_id)
        assert current is not None
        assert current["lease_owner"] == "worker-b"
        assert current["lease_generation"] == second["lease_generation"]
        assert current["control_epoch"] == second["control_epoch"]
        assert current["lease_expires_at"] == second_expiry
        await store.close()

    asyncio.run(scenario())


def test_terminal_result_events_and_webhook_outbox_commit_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "atomic-finalize.db"
        store = SqliteStore(database)
        endpoint = await store.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["run.stage_completed.v1", "run.completed.v1"],
                "status": "ACTIVE",
                "signature_version": "v1",
                "replay_window_seconds": 300,
                "signing_secret": "A" * 43,
                "created_at": "2026-07-31T00:00:00Z",
            }
        )
        run = await store.create_run(
            {
                "tenant_id": "tenant_1",
                "status": "RUNNING",
                "phase": "PACKAGE",
                "run_revision": 1,
                "snapshot_seq": 0,
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        run_id = run["run_id"]
        events = [
            {
                "schema_version": "1.0",
                "occurred_at": "2026-07-31T00:01:00Z",
                "type": "run.stage_completed.v1",
                "payload": {
                    "phase": "PACKAGE",
                    "status": "COMPLETED",
                    "completed_at": "2026-07-31T00:01:00Z",
                    "progress_percent": 100.0,
                    "output_refs": [],
                    "next_phase": None,
                },
            },
            {
                "schema_version": "1.0",
                "occurred_at": "2026-07-31T00:01:00Z",
                "type": "run.completed.v1",
                "payload": {"outcome": "SUCCEEDED", "result_href": f"/v1/runs/{run_id}/result"},
            },
        ]
        result = {
            "run_id": run_id,
            "outcome": "SUCCEEDED",
            "summary": "atomic",
            "completed_at": "2026-07-31T00:01:00Z",
        }
        updates = {"status": "TERMINAL", "outcome": "SUCCEEDED"}

        original_write = store._write_checkpoint

        def fail_checkpoint(*args, **kwargs):
            raise OSError("synthetic final checkpoint failure")

        store._write_checkpoint = fail_checkpoint
        with pytest.raises(OSError, match="synthetic final checkpoint failure"):
            await store.finalize_run_with_result(
                run_id,
                result=result,
                run_updates=updates,
                events=events,
                expected_revision=1,
            )
        store._write_checkpoint = original_write

        assert (await store.get_run(run_id))["status"] == "RUNNING"
        assert await store.get_result(run_id) is None
        assert await store.get_events(run_id) == []
        assert await store.list_webhook_deliveries(endpoint["webhook_endpoint_id"]) == []

        finalized, stored_result, stored_events = await store.finalize_run_with_result(
            run_id,
            result=result,
            run_updates=updates,
            events=events,
            expected_revision=1,
        )
        assert finalized["status"] == "TERMINAL"
        assert finalized["run_revision"] == 2
        assert stored_result["outcome"] == "SUCCEEDED"
        assert [item["type"] for item in stored_events] == [
            "run.stage_completed.v1",
            "run.completed.v1",
        ]
        deliveries = await store.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
        assert len(deliveries) == 2
        assert {item["event_id"] for item in deliveries} == {
            item["event_id"] for item in stored_events
        }
        await store.close()

        reopened = SqliteStore(database)
        assert (await reopened.get_run(run_id))["status"] == "TERMINAL"
        assert (await reopened.get_result(run_id))["outcome"] == "SUCCEEDED"
        assert len(await reopened.get_events(run_id)) == 2
        assert len(await reopened.list_webhook_deliveries(endpoint["webhook_endpoint_id"])) == 2
        await reopened.close()

    asyncio.run(scenario())


def test_failed_run_finalize_checkpoint_failure_rolls_back_terminal_bundle(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "failed-finalize-rollback.db"
        store = SqliteStore(database)
        endpoint = await store.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["run.failed.v1"],
                "status": "ACTIVE",
                "signature_version": "v1",
                "replay_window_seconds": 300,
                "signing_secret": "B" * 43,
                "created_at": "2026-07-31T00:00:00Z",
            }
        )
        run = await store.create_run(
            {
                "tenant_id": "tenant_1",
                "dataset_version_id": "dsv_1",
                "status": "RUNNING",
                "outcome": None,
                "phase": "TRAIN",
                "run_revision": 1,
                "snapshot_seq": 0,
                "latest_output_refs": [],
                "blocking": {"decision_packet_ids": [], "approval_ids": []},
                "available_actions": ["PAUSE", "CANCEL"],
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        run_id = run["run_id"]
        service = DurableWorkflowService(store, blob_store=SyntheticBlobStore())
        original_write = store._write_checkpoint

        def fail_terminal_checkpoint(snapshot, **kwargs):
            if snapshot["runs"][run_id].get("status") == "TERMINAL":
                raise OSError("synthetic failed-run checkpoint failure")
            return original_write(snapshot, **kwargs)

        store._write_checkpoint = fail_terminal_checkpoint
        with pytest.raises(OSError, match="synthetic failed-run checkpoint failure"):
            await service._fail_run(
                run,
                code="MODEL_TRAINING_FAILED",
                message="Synthetic model failure.",
                retriable=False,
            )
        store._write_checkpoint = original_write

        rolled_back_run = await store.get_run(run_id)
        assert rolled_back_run is not None
        assert rolled_back_run["status"] == "RUNNING"
        assert rolled_back_run["outcome"] is None
        assert await store.get_result(run_id) is None
        assert [event["type"] for event in await store.get_events(run_id)] == [
            "output.committed.v1"
        ]
        assert await store.list_webhook_deliveries(endpoint["webhook_endpoint_id"]) == []
        failure_outputs = await store.list_outputs(run_id=run_id)
        assert [output["type"] for output in failure_outputs] == ["FAILURE_REPORT"]
        await store.close()

        reopened = SqliteStore(database)
        reopened_run = await reopened.get_run(run_id)
        assert reopened_run is not None and reopened_run["status"] == "RUNNING"
        assert await reopened.get_result(run_id) is None
        assert [event["type"] for event in await reopened.get_events(run_id)] == [
            "output.committed.v1"
        ]
        assert await reopened.list_webhook_deliveries(endpoint["webhook_endpoint_id"]) == []
        await reopened.close()

    asyncio.run(scenario())


def test_cancel_finalize_checkpoint_failure_rolls_back_terminal_bundle(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "cancel-finalize-rollback.db"
        store = SqliteStore(database)
        endpoint = await store.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["run.canceled.v1"],
                "status": "ACTIVE",
                "signature_version": "v1",
                "replay_window_seconds": 300,
                "signing_secret": "C" * 43,
                "created_at": "2026-07-31T00:00:00Z",
            }
        )
        run = await store.create_run(
            {
                "tenant_id": "tenant_1",
                "dataset_version_id": "dsv_1",
                "status": "RUNNING",
                "outcome": None,
                "phase": "PLAN",
                "run_revision": 1,
                "snapshot_seq": 0,
                "stages": _stages(
                    active_phase="PLAN", active_status="RUNNING", completed={"INGEST"}
                ),
                "latest_output_refs": [],
                "blocking": {"decision_packet_ids": [], "approval_ids": []},
                "available_actions": ["PAUSE", "CANCEL"],
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        run_id = run["run_id"]
        service = WorkflowService(store, blob_store=SyntheticBlobStore())
        principal = Principal(subject="agent_1", tenant_id="tenant_1")
        original_write = store._write_checkpoint

        def fail_terminal_checkpoint(snapshot, **kwargs):
            if snapshot["runs"][run_id].get("status") == "TERMINAL":
                raise OSError("synthetic cancel checkpoint failure")
            return original_write(snapshot, **kwargs)

        store._write_checkpoint = fail_terminal_checkpoint
        with pytest.raises(OSError, match="synthetic cancel checkpoint failure"):
            await service.cancel(principal, run_id)
        store._write_checkpoint = original_write

        rolled_back = await store.get_run(run_id)
        assert rolled_back is not None and rolled_back["status"] == "RUNNING"
        assert await store.get_result(run_id) is None
        assert await store.get_events(run_id) == []
        assert await store.list_webhook_deliveries(endpoint["webhook_endpoint_id"]) == []

        await service.cancel(principal, run_id)
        terminal = await store.get_run(run_id)
        assert terminal is not None and terminal["status"] == "TERMINAL"
        assert (await store.get_result(run_id))["outcome"] == "CANCELED"
        events = await store.get_events(run_id)
        assert [event["type"] for event in events] == ["run.canceled.v1"]
        assert events[0]["run_revision"] == terminal["run_revision"]
        deliveries = await store.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
        assert len(deliveries) == 1
        assert deliveries[0]["event_id"] == events[0]["event_id"]
        await store.close()

        reopened = SqliteStore(database)
        assert (await reopened.get_run(run_id))["status"] == "TERMINAL"
        assert (await reopened.get_result(run_id))["outcome"] == "CANCELED"
        assert len(await reopened.get_events(run_id)) == 1
        await reopened.close()

    asyncio.run(scenario())
