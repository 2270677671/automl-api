from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from automl_api.persistence import JobFenceError, SqliteStore
from automl_api.store import IdempotencyState


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
