from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .store import (
    IdempotencyDecision,
    InMemoryStore,
    ResourceAlreadyExistsError,
    ResourceNotFoundError,
    StoreError,
    StoredResponse,
    _IdempotencyRecord,
)


JsonDict = dict[str, Any]
_SNAPSHOT_VERSION = 1
_JOB_STATUSES = {"READY", "RETRY", "LEASED", "WAITING", "COMPLETED", "DEAD"}


class JobFenceError(StoreError):
    """Raised when a stale worker tries to commit a durable job transition."""

    def __init__(
        self,
        run_id: str,
        *,
        lease_generation: int,
        control_epoch: int,
        current_generation: int,
        current_epoch: int,
        current_status: str,
    ) -> None:
        self.run_id = run_id
        self.lease_generation = lease_generation
        self.control_epoch = control_epoch
        self.current_generation = current_generation
        self.current_epoch = current_epoch
        self.current_status = current_status
        super().__init__(
            "stale execution job fence for "
            f"{run_id!r}: lease {lease_generation}/{control_epoch}, "
            f"current {current_generation}/{current_epoch} ({current_status})"
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


class SqliteStore(InMemoryStore):
    """Single-process durable adapter for the current in-memory Store API.

    Resource state is checkpointed as one SQLite transaction after every mutation. Durable
    execution jobs use normalized rows so their leases can be claimed and fenced atomically.
    SQLite mode intentionally permits only one active job lease at a time. The whole-state
    checkpoint trades write amplification for a zero-migration adapter and is not intended for
    large histories; leases use SQLite's database clock and therefore assume a stable host clock.
    """

    def __init__(self, database_path: str | Path) -> None:
        super().__init__()
        self.database_path = str(database_path)
        self._durability_lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_database()
        self._create_schema()
        snapshot = self._load_checkpoint()
        if snapshot is None:
            self._write_checkpoint(self._serialize_state())
        else:
            self._restore_state(snapshot)

    def _configure_database(self) -> None:
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_checkpoint (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS execution_job (
                run_id TEXT PRIMARY KEY,
                workflow_step TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('READY', 'RETRY', 'LEASED', 'WAITING', 'COMPLETED', 'DEAD')
                ),
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                available_at REAL,
                lease_owner TEXT,
                lease_expires_at REAL,
                lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
                control_epoch INTEGER NOT NULL DEFAULT 0 CHECK (control_epoch >= 0),
                last_error TEXT,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS execution_job_ready_idx
            ON execution_job(status, available_at, lease_expires_at);
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteStore is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        self._ensure_open()
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield cursor
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
        finally:
            cursor.close()

    def _serialize_state(self) -> JsonDict:
        with self._lock:
            idempotency = []
            for (operation, key), record in self._idempotency.items():
                response = record.response
                idempotency.append(
                    {
                        "operation": operation,
                        "key": key,
                        "request_fingerprint": record.request_fingerprint,
                        "response": (
                            None
                            if response is None
                            else {
                                "status_code": response.status_code,
                                "body": deepcopy(response.body),
                                "headers": deepcopy(response.headers),
                            }
                        ),
                    }
                )
            return {
                "snapshot_version": _SNAPSHOT_VERSION,
                "id_counters": deepcopy(self._id_counters),
                "datasets": deepcopy(self._datasets),
                "dataset_versions": deepcopy(self._dataset_versions),
                "runs": deepcopy(self._runs),
                "events": deepcopy(self._events),
                "outputs": deepcopy(self._outputs),
                "decision_packets": deepcopy(self._decision_packets),
                "commands": deepcopy(self._commands),
                "results": deepcopy(self._results),
                "artifacts": deepcopy(self._artifacts),
                "approvals": deepcopy(self._approvals),
                "models": deepcopy(self._models),
                "webhook_endpoints": deepcopy(self._webhook_endpoints),
                "webhook_deliveries": deepcopy(self._webhook_deliveries),
                "deletions": deepcopy(self._deletions),
                "idempotency": idempotency,
            }

    def _restore_state(self, snapshot: Mapping[str, Any]) -> None:
        if snapshot.get("snapshot_version") != _SNAPSHOT_VERSION:
            raise RuntimeError("unsupported SQLite Store checkpoint version")
        with self._lock:
            self._id_counters = deepcopy(dict(snapshot.get("id_counters", {})))
            self._datasets = deepcopy(dict(snapshot.get("datasets", {})))
            self._dataset_versions = deepcopy(dict(snapshot.get("dataset_versions", {})))
            self._runs = deepcopy(dict(snapshot.get("runs", {})))
            self._events = deepcopy(dict(snapshot.get("events", {})))
            self._outputs = deepcopy(dict(snapshot.get("outputs", {})))
            self._decision_packets = deepcopy(dict(snapshot.get("decision_packets", {})))
            self._commands = deepcopy(dict(snapshot.get("commands", {})))
            self._results = deepcopy(dict(snapshot.get("results", {})))
            self._artifacts = deepcopy(dict(snapshot.get("artifacts", {})))
            self._approvals = deepcopy(dict(snapshot.get("approvals", {})))
            self._models = deepcopy(dict(snapshot.get("models", {})))
            self._webhook_endpoints = deepcopy(dict(snapshot.get("webhook_endpoints", {})))
            self._webhook_deliveries = deepcopy(dict(snapshot.get("webhook_deliveries", {})))
            self._deletions = deepcopy(dict(snapshot.get("deletions", {})))
            for run_id in self._runs:
                self._approvals.setdefault(run_id, {})
            self._event_ids = {
                run_id: {event["event_id"]: event for event in events}
                for run_id, events in self._events.items()
            }
            self._idempotency = {}
            for item in snapshot.get("idempotency", []):
                response_value = item.get("response")
                response = (
                    None
                    if response_value is None
                    else StoredResponse(
                        status_code=int(response_value["status_code"]),
                        body=deepcopy(response_value["body"]),
                        headers=deepcopy(dict(response_value["headers"])),
                    )
                )
                self._idempotency[(item["operation"], item["key"])] = _IdempotencyRecord(
                    request_fingerprint=item["request_fingerprint"],
                    response=response,
                )

    def _load_checkpoint(self) -> JsonDict | None:
        row = self._connection.execute(
            "SELECT version, payload FROM store_checkpoint WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        if int(row["version"]) != _SNAPSHOT_VERSION:
            raise RuntimeError("unsupported SQLite Store schema version")
        value = json.loads(row["payload"])
        if not isinstance(value, dict):
            raise RuntimeError("invalid SQLite Store checkpoint")
        return value

    def _write_checkpoint(self, snapshot: Mapping[str, Any], *, clear_jobs: bool = False) -> None:
        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO store_checkpoint(singleton, version, payload, updated_at)
                VALUES (1, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(singleton) DO UPDATE SET
                    version = excluded.version,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (_SNAPSHOT_VERSION, payload),
            )
            if clear_jobs:
                cursor.execute("DELETE FROM execution_job")

    async def _durable_mutation(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        with self._durability_lock:
            self._ensure_open()
            before = self._serialize_state()
            result = await method(*args, **kwargs)
            try:
                self._write_checkpoint(self._serialize_state())
            except Exception:
                self._restore_state(before)
                raise
            return result

    def new_id(self, kind: str) -> str:
        with self._durability_lock:
            self._ensure_open()
            before = self._serialize_state()
            value = super().new_id(kind)
            try:
                self._write_checkpoint(self._serialize_state())
            except Exception:
                self._restore_state(before)
                raise
            return value

    async def create_dataset(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_dataset, value)

    async def update_dataset(self, dataset_id: str, updates: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().update_dataset, dataset_id, updates)

    async def create_dataset_version(self, dataset_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_dataset_version, dataset_id, value)

    async def create_dataset_with_version(
        self,
        dataset: Mapping[str, Any],
        dataset_version: Mapping[str, Any],
    ) -> tuple[JsonDict, JsonDict]:
        return await self._durable_mutation(
            super().create_dataset_with_version,
            dataset,
            dataset_version,
        )

    async def update_dataset_version(
        self, dataset_version_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        return await self._durable_mutation(
            super().update_dataset_version,
            dataset_version_id,
            updates,
        )

    async def create_run(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_run, value)

    async def update_run(
        self,
        run_id: str,
        updates: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        bump_revision: bool = True,
    ) -> JsonDict:
        return await self._durable_mutation(
            super().update_run,
            run_id,
            updates,
            expected_revision=expected_revision,
            bump_revision=bump_revision,
        )

    async def append_event(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().append_event, run_id, value)

    async def mutate_run_with_event(
        self,
        run_id: str,
        updates: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        bump_revision: bool = True,
    ) -> tuple[JsonDict, JsonDict]:
        return await self._durable_mutation(
            super().mutate_run_with_event,
            run_id,
            updates,
            event,
            expected_revision=expected_revision,
            bump_revision=bump_revision,
        )

    async def create_output(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_output, run_id, value)

    async def create_decision_packet(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_decision_packet, run_id, value)

    async def update_decision_packet(
        self,
        run_id: str,
        decision_packet_id: str,
        updates: Mapping[str, Any],
    ) -> JsonDict:
        return await self._durable_mutation(
            super().update_decision_packet,
            run_id,
            decision_packet_id,
            updates,
        )

    async def create_command(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_command, value)

    async def update_command(self, command_id: str, updates: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().update_command, command_id, updates)

    async def set_result(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().set_result, run_id, value)

    async def create_artifact(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_artifact, value)

    async def update_artifact(self, artifact_id: str, updates: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().update_artifact, artifact_id, updates)

    async def create_approval(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_approval, run_id, value)

    async def update_approval(
        self, run_id: str, approval_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        return await self._durable_mutation(super().update_approval, run_id, approval_id, updates)

    async def create_model(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_model, value)

    async def create_webhook_endpoint(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_webhook_endpoint, value)

    async def update_webhook_endpoint(
        self, webhook_endpoint_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        return await self._durable_mutation(
            super().update_webhook_endpoint, webhook_endpoint_id, updates
        )

    async def create_webhook_delivery(
        self, webhook_endpoint_id: str, value: Mapping[str, Any]
    ) -> JsonDict:
        return await self._durable_mutation(
            super().create_webhook_delivery, webhook_endpoint_id, value
        )

    async def update_webhook_delivery(
        self,
        webhook_endpoint_id: str,
        delivery_id: str,
        updates: Mapping[str, Any],
    ) -> JsonDict:
        return await self._durable_mutation(
            super().update_webhook_delivery,
            webhook_endpoint_id,
            delivery_id,
            updates,
        )

    async def create_deletion(self, value: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().create_deletion, value)

    async def update_deletion(self, deletion_id: str, updates: Mapping[str, Any]) -> JsonDict:
        return await self._durable_mutation(super().update_deletion, deletion_id, updates)

    async def begin_idempotent_request(
        self, operation: str, key: str, request_fingerprint: str
    ) -> IdempotencyDecision:
        return await self._durable_mutation(
            super().begin_idempotent_request,
            operation,
            key,
            request_fingerprint,
        )

    async def complete_idempotent_request(
        self,
        operation: str,
        key: str,
        request_fingerprint: str,
        *,
        status_code: int,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> StoredResponse:
        return await self._durable_mutation(
            super().complete_idempotent_request,
            operation,
            key,
            request_fingerprint,
            status_code=status_code,
            body=body,
            headers=headers,
        )

    async def abort_idempotent_request(
        self, operation: str, key: str, request_fingerprint: str
    ) -> bool:
        return await self._durable_mutation(
            super().abort_idempotent_request,
            operation,
            key,
            request_fingerprint,
        )

    async def reset(self) -> None:
        with self._durability_lock:
            self._ensure_open()
            before = self._serialize_state()
            await super().reset()
            try:
                self._write_checkpoint(self._serialize_state(), clear_jobs=True)
            except Exception:
                self._restore_state(before)
                raise

    def _job_from_row(self, row: sqlite3.Row) -> JsonDict:
        value = dict(row)
        value["checkpoint"] = json.loads(value.pop("checkpoint_json"))
        return value

    async def create_execution_job(
        self,
        run_id: str,
        *,
        workflow_step: str,
        checkpoint: Mapping[str, Any] | None = None,
        max_attempts: int = 5,
        control_epoch: int = 0,
    ) -> JsonDict:
        if not workflow_step:
            raise ValueError("workflow_step must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if control_epoch < 0:
            raise ValueError("control_epoch must be non-negative")
        with self._durability_lock:
            with self._lock:
                if run_id not in self._runs:
                    raise ResourceNotFoundError("run", run_id)
            try:
                with self._transaction() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO execution_job(
                            run_id, workflow_step, status, checkpoint_json,
                            max_attempts, available_at, control_epoch
                        )
                        VALUES (?, ?, 'READY', ?, ?, julianday('now'), ?)
                        """,
                        (
                            run_id,
                            workflow_step,
                            json.dumps(
                                dict(checkpoint or {}),
                                sort_keys=True,
                                separators=(",", ":"),
                                default=_json_default,
                            ),
                            max_attempts,
                            control_epoch,
                        ),
                    )
                    row = cursor.execute(
                        "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                    ).fetchone()
            except sqlite3.IntegrityError as exc:
                raise ResourceAlreadyExistsError("execution_job", run_id) from exc
            assert row is not None
            return self._job_from_row(row)

    async def get_execution_job(self, run_id: str) -> JsonDict | None:
        with self._durability_lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if row is None else self._job_from_row(row)

    async def wake_execution_job(self, run_id: str) -> JsonDict:
        with self._durability_lock:
            with self._transaction() as cursor:
                row = cursor.execute(
                    "SELECT status FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ResourceNotFoundError("execution_job", run_id)
                if row["status"] in {"COMPLETED", "DEAD"}:
                    raise StoreError(f"cannot wake terminal execution job {run_id!r}")
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET status = 'READY', available_at = julianday('now'),
                        lease_owner = NULL, lease_expires_at = NULL,
                        control_epoch = control_epoch + 1, attempt = 0,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
                updated = cursor.execute(
                    "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
            assert updated is not None
            return self._job_from_row(updated)

    async def claim_execution_job(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> JsonDict | None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._durability_lock:
            with self._transaction() as cursor:
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET status = 'DEAD', lease_owner = NULL, lease_expires_at = NULL,
                        last_error = COALESCE(last_error, 'lease expired at max attempts'),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE status = 'LEASED'
                      AND lease_expires_at <= julianday('now')
                      AND attempt >= max_attempts
                    """
                )
                active = cursor.execute(
                    """
                    SELECT run_id FROM execution_job
                    WHERE status = 'LEASED' AND lease_expires_at > julianday('now')
                    LIMIT 1
                    """
                ).fetchone()
                if active is not None:
                    return None
                row = cursor.execute(
                    """
                    SELECT run_id FROM execution_job
                    WHERE attempt < max_attempts AND (
                        (status IN ('READY', 'RETRY') AND available_at <= julianday('now'))
                        OR (status = 'LEASED' AND lease_expires_at <= julianday('now'))
                    )
                    ORDER BY COALESCE(available_at, lease_expires_at), created_at, run_id
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                run_id = row["run_id"]
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET status = 'LEASED', attempt = attempt + 1,
                        lease_owner = ?,
                        lease_expires_at = julianday('now') + (? / 86400.0),
                        lease_generation = lease_generation + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE run_id = ?
                    """,
                    (worker_id, lease_seconds, run_id),
                )
                claimed = cursor.execute(
                    "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
            assert claimed is not None
            return self._job_from_row(claimed)

    def _require_job_fence(
        self,
        cursor: sqlite3.Cursor,
        run_id: str,
        lease_generation: int,
        control_epoch: int,
    ) -> sqlite3.Row:
        row = cursor.execute("SELECT * FROM execution_job WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ResourceNotFoundError("execution_job", run_id)
        if (
            row["status"] != "LEASED"
            or int(row["lease_generation"]) != lease_generation
            or int(row["control_epoch"]) != control_epoch
            or row["lease_expires_at"] <= cursor.execute("SELECT julianday('now')").fetchone()[0]
        ):
            raise JobFenceError(
                run_id,
                lease_generation=lease_generation,
                control_epoch=control_epoch,
                current_generation=int(row["lease_generation"]),
                current_epoch=int(row["control_epoch"]),
                current_status=str(row["status"]),
            )
        return row

    async def checkpoint_execution_job(
        self,
        run_id: str,
        *,
        lease_generation: int,
        control_epoch: int,
        workflow_step: str,
        checkpoint: Mapping[str, Any],
        next_status: str = "READY",
    ) -> JsonDict:
        if next_status not in {"READY", "WAITING"}:
            raise ValueError("next_status must be READY or WAITING")
        if not workflow_step:
            raise ValueError("workflow_step must not be empty")
        checkpoint_json = json.dumps(
            dict(checkpoint),
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        with self._durability_lock:
            with self._transaction() as cursor:
                self._require_job_fence(cursor, run_id, lease_generation, control_epoch)
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET workflow_step = ?, checkpoint_json = ?, status = ?,
                        attempt = 0,
                        available_at = CASE WHEN ? = 'READY' THEN julianday('now') ELSE NULL END,
                        lease_owner = NULL, lease_expires_at = NULL, last_error = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE run_id = ?
                    """,
                    (workflow_step, checkpoint_json, next_status, next_status, run_id),
                )
                updated = cursor.execute(
                    "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
            assert updated is not None
            return self._job_from_row(updated)

    async def complete_execution_job(
        self,
        run_id: str,
        *,
        lease_generation: int,
        control_epoch: int,
    ) -> JsonDict:
        with self._durability_lock:
            with self._transaction() as cursor:
                self._require_job_fence(cursor, run_id, lease_generation, control_epoch)
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET status = 'COMPLETED', available_at = NULL,
                        lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
                updated = cursor.execute(
                    "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
            assert updated is not None
            return self._job_from_row(updated)

    async def retry_execution_job(
        self,
        run_id: str,
        *,
        lease_generation: int,
        control_epoch: int,
        delay_seconds: float,
        error: str,
    ) -> JsonDict:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        with self._durability_lock:
            with self._transaction() as cursor:
                row = self._require_job_fence(cursor, run_id, lease_generation, control_epoch)
                terminal = int(row["attempt"]) >= int(row["max_attempts"])
                next_status = "DEAD" if terminal else "RETRY"
                cursor.execute(
                    """
                    UPDATE execution_job
                    SET status = ?,
                        available_at = CASE
                            WHEN ? = 'RETRY'
                            THEN julianday('now') + (? / 86400.0)
                            ELSE NULL
                        END,
                        lease_owner = NULL, lease_expires_at = NULL, last_error = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE run_id = ?
                    """,
                    (next_status, next_status, delay_seconds, error, run_id),
                )
                updated = cursor.execute(
                    "SELECT * FROM execution_job WHERE run_id = ?", (run_id,)
                ).fetchone()
            assert updated is not None
            return self._job_from_row(updated)

    async def close(self) -> None:
        with self._durability_lock:
            if self._closed:
                return
            self._write_checkpoint(self._serialize_state())
            self._connection.close()
            self._closed = True

    async def __aenter__(self) -> SqliteStore:
        self._ensure_open()
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        await self.close()


__all__ = ["JobFenceError", "SqliteStore"]
