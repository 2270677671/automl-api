from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from .persistence import JobFenceError, SqliteStore


ExecutionJob: TypeAlias = dict[str, Any]
CheckpointStatus: TypeAlias = Literal["READY", "WAITING"]


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    next_step: str
    status: CheckpointStatus = "READY"
    checkpoint: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.next_step:
            raise ValueError("next_step must not be empty")
        if self.status not in {"READY", "WAITING"}:
            raise ValueError("status must be READY or WAITING")
        object.__setattr__(self, "checkpoint", dict(self.checkpoint))


@dataclass(frozen=True, slots=True)
class CompleteResult:
    pass


WorkerResult: TypeAlias = CheckpointResult | CompleteResult
WorkerHandler: TypeAlias = Callable[[ExecutionJob], Awaitable[WorkerResult]]
DeadHandler: TypeAlias = Callable[[ExecutionJob], Awaitable[None]]


def CHECKPOINT(
    next_step: str,
    *,
    status: CheckpointStatus = "READY",
    checkpoint: Mapping[str, Any] | None = None,
) -> CheckpointResult:
    return CheckpointResult(
        next_step=next_step,
        status=status,
        checkpoint={} if checkpoint is None else checkpoint,
    )


COMPLETE = CompleteResult()


class LocalExecutionWorker:
    """Poll and execute durable SQLite jobs in the local deployment profile."""

    def __init__(
        self,
        store: SqliteStore,
        handler: WorkerHandler,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        poll_interval: float = 0.5,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        on_dead: DeadHandler | None = None,
    ) -> None:
        resolved_worker_id = f"local-{uuid4()}" if worker_id is None else worker_id
        if not resolved_worker_id:
            raise ValueError("worker_id must not be empty")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not math.isfinite(poll_interval) or poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if not math.isfinite(retry_base_seconds) or retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be non-negative")
        if not math.isfinite(retry_max_seconds) or retry_max_seconds < 0:
            raise ValueError("retry_max_seconds must be non-negative")

        self.store = store
        self.handler = handler
        self.worker_id = resolved_worker_id
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.on_dead = on_dead
        self._run_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name=f"automl-worker-{self.worker_id}",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._poll_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._poll_task is task:
                self._poll_task = None

    async def run_once(self) -> bool:
        async with self._run_lock:
            job = await self.store.claim_execution_job(
                self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if job is None:
                return False

            run_id = str(job["run_id"])
            attempt = int(job["attempt"])
            lease_generation = int(job["lease_generation"])
            control_epoch = int(job["control_epoch"])
            try:
                result = await self.handler(job)
                if not isinstance(result, (CheckpointResult, CompleteResult)):
                    raise TypeError("worker handler must return CHECKPOINT(...) or COMPLETE")
            except asyncio.CancelledError:
                try:
                    updated = await self.store.retry_execution_job(
                        run_id,
                        lease_generation=lease_generation,
                        control_epoch=control_epoch,
                        delay_seconds=0,
                        error="CancelledError: worker stopped",
                    )
                except JobFenceError:
                    updated = None
                if updated is not None and updated["status"] == "DEAD" and self.on_dead is not None:
                    await self.on_dead(updated)
                raise
            except Exception as exc:
                delay_seconds = self._retry_delay(attempt)
                updated = await self.store.retry_execution_job(
                    run_id,
                    lease_generation=lease_generation,
                    control_epoch=control_epoch,
                    delay_seconds=delay_seconds,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if updated["status"] == "DEAD" and self.on_dead is not None:
                    await self.on_dead(updated)
                return True

            if isinstance(result, CompleteResult):
                await self.store.complete_execution_job(
                    run_id,
                    lease_generation=lease_generation,
                    control_epoch=control_epoch,
                )
            else:
                await self.store.checkpoint_execution_job(
                    run_id,
                    lease_generation=lease_generation,
                    control_epoch=control_epoch,
                    workflow_step=result.next_step,
                    checkpoint=result.checkpoint,
                    next_status=result.status,
                )
            return True

    def _retry_delay(self, attempt: int) -> float:
        if self.retry_base_seconds == 0 or self.retry_max_seconds == 0:
            return 0.0
        try:
            delay = self.retry_base_seconds * (2.0 ** max(0, attempt - 1))
        except OverflowError:
            return self.retry_max_seconds
        return min(self.retry_max_seconds, delay)

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self.run_once():
                continue
            if self.poll_interval == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval,
                )
            except TimeoutError:
                pass


__all__ = [
    "CHECKPOINT",
    "COMPLETE",
    "CheckpointResult",
    "CompleteResult",
    "ExecutionJob",
    "LocalExecutionWorker",
    "WorkerResult",
]
