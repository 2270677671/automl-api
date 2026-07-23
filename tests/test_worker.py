from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from automl_api.persistence import JobFenceError, SqliteStore
from automl_api.worker import CHECKPOINT, COMPLETE, LocalExecutionWorker


async def _create_job(
    store: SqliteStore,
    *,
    workflow_step: str = "PROFILE",
    max_attempts: int = 4,
) -> str:
    run = await store.create_run({"tenant_id": "tenant_1", "status": "QUEUED", "run_revision": 1})
    run_id = str(run["run_id"])
    await store.create_execution_job(
        run_id,
        workflow_step=workflow_step,
        checkpoint={"source": "upload"},
        max_attempts=max_attempts,
    )
    return run_id


def test_worker_checkpoints_then_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-success.db")
        run_id = await _create_job(store)
        handled_steps: list[str] = []

        async def handler(job: dict[str, Any]):
            handled_steps.append(str(job["workflow_step"]))
            if job["workflow_step"] == "PROFILE":
                return CHECKPOINT(
                    "PACKAGE",
                    checkpoint={"profiled": True, "source": job["checkpoint"]["source"]},
                )
            assert job["checkpoint"] == {"profiled": True, "source": "upload"}
            return COMPLETE

        worker = LocalExecutionWorker(store, handler, worker_id="worker-success")
        assert await worker.run_once() is True
        checkpointed = await store.get_execution_job(run_id)
        assert checkpointed is not None
        assert checkpointed["status"] == "READY"
        assert checkpointed["workflow_step"] == "PACKAGE"
        assert checkpointed["attempt"] == 0

        assert await worker.run_once() is True
        completed = await store.get_execution_job(run_id)
        assert completed is not None
        assert completed["status"] == "COMPLETED"
        assert completed["lease_generation"] == 2
        assert handled_steps == ["PROFILE", "PACKAGE"]
        assert await worker.run_once() is False
        await store.close()

    asyncio.run(scenario())


def test_worker_waits_until_job_is_woken(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-waiting.db")
        run_id = await _create_job(store)

        async def handler(job: dict[str, Any]):
            if job["workflow_step"] == "PROFILE":
                return CHECKPOINT(
                    "RESOLVE_TASK",
                    status="WAITING",
                    checkpoint={"decision_packet_id": "packet_1"},
                )
            assert job["checkpoint"] == {"decision_packet_id": "packet_1"}
            return COMPLETE

        worker = LocalExecutionWorker(store, handler, worker_id="worker-waiting")
        assert await worker.run_once() is True
        waiting = await store.get_execution_job(run_id)
        assert waiting is not None
        assert waiting["status"] == "WAITING"
        assert await worker.run_once() is False

        woken = await store.wake_execution_job(run_id)
        assert woken["status"] == "READY"
        assert woken["control_epoch"] == 1
        assert await worker.run_once() is True
        assert (await store.get_execution_job(run_id))["status"] == "COMPLETED"
        await store.close()

    asyncio.run(scenario())


def test_worker_retries_handler_failure_then_succeeds(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-retry.db")
        run_id = await _create_job(store)
        attempts: list[int] = []

        async def handler(job: dict[str, Any]):
            attempts.append(int(job["attempt"]))
            if len(attempts) == 1:
                raise RuntimeError("temporary training failure")
            return COMPLETE

        worker = LocalExecutionWorker(
            store,
            handler,
            worker_id="worker-retry",
            retry_base_seconds=0,
            retry_max_seconds=0,
        )
        assert await worker.run_once() is True
        retried = await store.get_execution_job(run_id)
        assert retried is not None
        assert retried["status"] == "RETRY"
        assert retried["attempt"] == 1
        assert retried["lease_generation"] == 1
        assert retried["last_error"] == "RuntimeError: temporary training failure"

        assert await worker.run_once() is True
        completed = await store.get_execution_job(run_id)
        assert completed is not None
        assert completed["status"] == "COMPLETED"
        assert completed["attempt"] == 2
        assert completed["lease_generation"] == 2
        assert attempts == [1, 2]
        await store.close()

    asyncio.run(scenario())


def test_worker_calls_dead_handler_after_attempts_are_exhausted(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-dead.db")
        run_id = await _create_job(store, max_attempts=1)
        dead_jobs: list[dict[str, Any]] = []

        async def handler(_job: dict[str, Any]):
            raise ValueError("invalid feature matrix")

        async def on_dead(job: dict[str, Any]) -> None:
            dead_jobs.append(job)

        worker = LocalExecutionWorker(
            store,
            handler,
            worker_id="worker-dead",
            retry_base_seconds=0,
            on_dead=on_dead,
        )
        assert await worker.run_once() is True
        dead = await store.get_execution_job(run_id)
        assert dead is not None
        assert dead["status"] == "DEAD"
        assert dead["last_error"] == "ValueError: invalid feature matrix"
        assert len(dead_jobs) == 1
        assert dead_jobs[0]["run_id"] == run_id
        assert dead_jobs[0]["status"] == "DEAD"
        await store.close()

    asyncio.run(scenario())


def test_worker_does_not_retry_after_control_epoch_fence(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-fence.db")
        run_id = await _create_job(store)

        async def handler(_job: dict[str, Any]):
            await store.wake_execution_job(run_id)
            return COMPLETE

        worker = LocalExecutionWorker(
            store,
            handler,
            worker_id="worker-fenced",
            retry_base_seconds=0,
        )
        with pytest.raises(JobFenceError):
            await worker.run_once()

        woken = await store.get_execution_job(run_id)
        assert woken is not None
        assert woken["status"] == "READY"
        assert woken["control_epoch"] == 1
        assert woken["attempt"] == 0
        assert woken["last_error"] is None
        await store.close()

    asyncio.run(scenario())


def test_stopped_worker_releases_lease_for_another_worker_to_reclaim(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "worker-stop.db")
        run_id = await _create_job(store, max_attempts=3)
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def blocking_handler(_job: dict[str, Any]):
            handler_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                handler_cancelled.set()
            return COMPLETE

        worker_a = LocalExecutionWorker(
            store,
            blocking_handler,
            worker_id="worker-a",
            lease_seconds=0.1,
            poll_interval=0.01,
        )
        await worker_a.start()
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        leased = await store.get_execution_job(run_id)
        assert leased is not None
        assert leased["status"] == "LEASED"

        await worker_a.stop()
        assert handler_cancelled.is_set()
        after_stop = await store.get_execution_job(run_id)
        assert after_stop is not None
        assert after_stop["status"] == "RETRY"
        assert after_stop["lease_generation"] == leased["lease_generation"]
        assert after_stop["last_error"] == "CancelledError: worker stopped"

        async def completing_handler(_job: dict[str, Any]):
            return COMPLETE

        worker_b = LocalExecutionWorker(
            store,
            completing_handler,
            worker_id="worker-b",
            lease_seconds=1,
        )
        assert await worker_b.run_once() is True

        completed = await store.get_execution_job(run_id)
        assert completed is not None
        assert completed["status"] == "COMPLETED"
        assert completed["attempt"] == 2
        assert completed["lease_generation"] == leased["lease_generation"] + 1
        await store.close()

    asyncio.run(scenario())
