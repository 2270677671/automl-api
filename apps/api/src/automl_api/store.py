from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from time import monotonic
from typing import Any


JsonDict = dict[str, Any]


class StoreError(RuntimeError):
    """Base error for the in-memory store."""


class ResourceNotFoundError(StoreError):
    def __init__(self, resource: str, resource_id: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource} {resource_id!r} was not found")


class ResourceAlreadyExistsError(StoreError):
    def __init__(self, resource: str, resource_id: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource} {resource_id!r} already exists")


class RevisionConflictError(StoreError):
    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"expected run revision {expected}, current revision is {current}")


class IdempotencyConflictError(StoreError):
    def __init__(
        self,
        operation: str,
        key: str,
        request_fingerprint: str,
        existing_fingerprint: str,
    ) -> None:
        self.operation = operation
        self.key = key
        self.request_fingerprint = request_fingerprint
        self.existing_fingerprint = existing_fingerprint
        super().__init__(
            f"idempotency key {key!r} was reused for a different request in operation {operation!r}"
        )


class IdempotencyState(str, Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"
    CONFLICT = "conflict"
    MISS = "miss"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: Any
    headers: dict[str, str]

    def clone(self) -> StoredResponse:
        return StoredResponse(
            status_code=self.status_code,
            body=deepcopy(self.body),
            headers=deepcopy(self.headers),
        )


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    state: IdempotencyState
    request_fingerprint: str
    response: StoredResponse | None = None
    existing_fingerprint: str | None = None

    @property
    def is_new(self) -> bool:
        return self.state is IdempotencyState.NEW

    @property
    def is_replay(self) -> bool:
        return self.state is IdempotencyState.REPLAY

    @property
    def is_conflict(self) -> bool:
        return self.state is IdempotencyState.CONFLICT


@dataclass(slots=True)
class _IdempotencyRecord:
    request_fingerprint: str
    response: StoredResponse | None = None


class InMemoryStore:
    """Thread-safe, process-local storage for the Milestone 1 API shell.

    Public values are copied on ingress and egress so callers cannot mutate state
    outside the lock. Methods are async to fit FastAPI handlers, but critical
    sections contain no awaits and are also protected against access from threads.
    """

    _ID_PREFIXES = {
        "dataset": "ds",
        "datasets": "ds",
        "ds": "ds",
        "dataset_version": "dsv",
        "dataset_versions": "dsv",
        "dsv": "dsv",
        "run": "run",
        "runs": "run",
        "event": "evt",
        "events": "evt",
        "evt": "evt",
        "output": "out",
        "outputs": "out",
        "out": "out",
        "decision_packet": "dp",
        "decision_packets": "dp",
        "dp": "dp",
        "wait_set": "ws",
        "ws": "ws",
        "command": "cmd",
        "commands": "cmd",
        "cmd": "cmd",
        "result": "res",
        "result_manifest": "res",
        "res": "res",
        "artifact": "art",
        "artifacts": "art",
        "art": "art",
        "experiment": "exp",
        "exp": "exp",
        "model": "mdl",
        "mdl": "mdl",
        "approval": "apr",
        "apr": "apr",
        "webhook_endpoint": "wh",
        "webhook_endpoints": "wh",
        "wh": "wh",
        "webhook_delivery": "whd",
        "webhook_deliveries": "whd",
        "whd": "whd",
        "deletion": "del",
        "deletions": "del",
        "del": "del",
        "redelivery": "redel",
        "redeliveries": "redel",
        "redel": "redel",
    }

    def __init__(self) -> None:
        self._lock = RLock()
        self._id_counters: dict[str, int] = {}
        self._datasets: dict[str, JsonDict] = {}
        self._dataset_versions: dict[str, JsonDict] = {}
        self._runs: dict[str, JsonDict] = {}
        self._events: dict[str, list[JsonDict]] = {}
        self._event_ids: dict[str, dict[str, JsonDict]] = {}
        self._outputs: dict[str, dict[str, JsonDict]] = {}
        self._decision_packets: dict[str, dict[str, JsonDict]] = {}
        self._commands: dict[str, JsonDict] = {}
        self._results: dict[str, JsonDict] = {}
        self._artifacts: dict[str, JsonDict] = {}
        self._approvals: dict[str, dict[str, JsonDict]] = {}
        self._models: dict[str, JsonDict] = {}
        self._webhook_endpoints: dict[str, JsonDict] = {}
        self._webhook_deliveries: dict[str, dict[str, JsonDict]] = {}
        self._deletions: dict[str, JsonDict] = {}
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._event_conditions: dict[tuple[str, asyncio.AbstractEventLoop], asyncio.Condition] = {}
        self._idempotency_conditions: dict[
            tuple[str, str, asyncio.AbstractEventLoop], asyncio.Condition
        ] = {}

    def new_id(self, kind: str) -> str:
        normalized = kind.strip().lower().rstrip("_")
        prefix = self._ID_PREFIXES.get(normalized, normalized)
        if not prefix or not prefix.replace("-", "").isalnum():
            raise ValueError(f"invalid resource ID kind: {kind!r}")
        with self._lock:
            return self._new_id_locked(prefix)

    def _new_id_locked(self, prefix: str) -> str:
        value = self._id_counters.get(prefix, 0) + 1
        self._id_counters[prefix] = value
        return f"{prefix}_{value:012d}"

    @staticmethod
    def _mapping(value: Mapping[str, Any], *, name: str) -> JsonDict:
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")
        return deepcopy(dict(value))

    def _create_locked(
        self,
        collection: dict[str, JsonDict],
        value: Mapping[str, Any],
        *,
        id_field: str,
        kind: str,
        resource: str,
        fixed_fields: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        item = self._mapping(value, name=resource)
        for field, fixed_value in (fixed_fields or {}).items():
            supplied = item.get(field)
            if supplied is not None and supplied != fixed_value:
                raise ValueError(f"{resource}.{field} does not match its parent")
            item[field] = fixed_value
        resource_id = item.get(id_field)
        if resource_id is None:
            resource_id = self._new_id_locked(self._ID_PREFIXES[kind])
            item[id_field] = resource_id
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError(f"{resource}.{id_field} must be a non-empty string")
        if resource_id in collection:
            raise ResourceAlreadyExistsError(resource, resource_id)
        collection[resource_id] = item
        return deepcopy(item)

    @staticmethod
    def _get_locked(collection: dict[str, JsonDict], resource_id: str) -> JsonDict | None:
        value = collection.get(resource_id)
        return deepcopy(value) if value is not None else None

    def _update_locked(
        self,
        collection: dict[str, JsonDict],
        resource_id: str,
        updates: Mapping[str, Any],
        *,
        id_field: str,
        resource: str,
    ) -> JsonDict:
        current = collection.get(resource_id)
        if current is None:
            raise ResourceNotFoundError(resource, resource_id)
        patch = self._mapping(updates, name=f"{resource} updates")
        supplied_id = patch.pop(id_field, resource_id)
        if supplied_id != resource_id:
            raise ValueError(f"{resource}.{id_field} cannot be changed")
        current.update(patch)
        return deepcopy(current)

    # Datasets and immutable dataset versions.

    async def create_dataset(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._create_locked(
                self._datasets,
                value,
                id_field="dataset_id",
                kind="dataset",
                resource="dataset",
            )

    async def get_dataset(self, dataset_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._datasets, dataset_id)

    async def update_dataset(self, dataset_id: str, updates: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._update_locked(
                self._datasets,
                dataset_id,
                updates,
                id_field="dataset_id",
                resource="dataset",
            )

    async def list_datasets(self) -> list[JsonDict]:
        with self._lock:
            return deepcopy(list(self._datasets.values()))

    async def create_dataset_version(self, dataset_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            if dataset_id not in self._datasets:
                raise ResourceNotFoundError("dataset", dataset_id)
            return self._create_locked(
                self._dataset_versions,
                value,
                id_field="dataset_version_id",
                kind="dataset_version",
                resource="dataset_version",
                fixed_fields={"dataset_id": dataset_id},
            )

    async def create_dataset_with_version(
        self,
        dataset: Mapping[str, Any],
        dataset_version: Mapping[str, Any],
    ) -> tuple[JsonDict, JsonDict]:
        with self._lock:
            created_dataset = self._create_locked(
                self._datasets,
                dataset,
                id_field="dataset_id",
                kind="dataset",
                resource="dataset",
            )
            try:
                created_version = self._create_locked(
                    self._dataset_versions,
                    dataset_version,
                    id_field="dataset_version_id",
                    kind="dataset_version",
                    resource="dataset_version",
                    fixed_fields={"dataset_id": created_dataset["dataset_id"]},
                )
            except Exception:
                self._datasets.pop(created_dataset["dataset_id"], None)
                raise
            return created_dataset, created_version

    async def get_dataset_version(self, dataset_version_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._dataset_versions, dataset_version_id)

    async def update_dataset_version(
        self, dataset_version_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        with self._lock:
            current = self._dataset_versions.get(dataset_version_id)
            if current is None:
                raise ResourceNotFoundError("dataset_version", dataset_version_id)
            supplied_dataset = updates.get("dataset_id", current["dataset_id"])
            if supplied_dataset != current["dataset_id"]:
                raise ValueError("dataset_version.dataset_id cannot be changed")
            return self._update_locked(
                self._dataset_versions,
                dataset_version_id,
                updates,
                id_field="dataset_version_id",
                resource="dataset_version",
            )

    async def list_dataset_versions(self, dataset_id: str | None = None) -> list[JsonDict]:
        with self._lock:
            values = self._dataset_versions.values()
            if dataset_id is not None:
                values = (value for value in values if value.get("dataset_id") == dataset_id)
            return deepcopy(list(values))

    # Runs and their append-only events.

    async def create_run(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            item = self._mapping(value, name="run")
            item.setdefault("run_revision", 1)
            item.setdefault("snapshot_seq", 0)
            created = self._create_locked(
                self._runs,
                item,
                id_field="run_id",
                kind="run",
                resource="run",
            )
            run_id = created["run_id"]
            self._events[run_id] = []
            self._event_ids[run_id] = {}
            self._outputs[run_id] = {}
            self._decision_packets[run_id] = {}
            self._approvals[run_id] = {}
            return created

    async def get_run(self, run_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._runs, run_id)

    async def update_run(
        self,
        run_id: str,
        updates: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        bump_revision: bool = True,
    ) -> JsonDict:
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise ResourceNotFoundError("run", run_id)
            revision = int(current.get("run_revision", 1))
            if expected_revision is not None and expected_revision != revision:
                raise RevisionConflictError(expected_revision, revision)
            patch = self._mapping(updates, name="run updates")
            supplied_id = patch.pop("run_id", run_id)
            if supplied_id != run_id:
                raise ValueError("run.run_id cannot be changed")
            patch.pop("run_revision", None)
            patch.pop("snapshot_seq", None)
            current.update(patch)
            if bump_revision:
                current["run_revision"] = revision + 1
            return deepcopy(current)

    async def list_runs(self) -> list[JsonDict]:
        with self._lock:
            return deepcopy(list(self._runs.values()))

    def _append_event_locked(self, run_id: str, value: Mapping[str, Any]) -> tuple[JsonDict, bool]:
        run = self._runs.get(run_id)
        if run is None:
            raise ResourceNotFoundError("run", run_id)
        event = self._mapping(value, name="event")
        supplied_run_id = event.get("run_id")
        if supplied_run_id is not None and supplied_run_id != run_id:
            raise ValueError("event.run_id does not match its parent run")
        event["run_id"] = run_id
        event_id = event.get("event_id")
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("event.event_id must be a non-empty string")
            existing = self._event_ids[run_id].get(event_id)
            if existing is not None:
                comparable = deepcopy(event)
                comparable["seq"] = existing["seq"]
                comparable.setdefault("run_revision", existing.get("run_revision"))
                if comparable != existing:
                    raise ResourceAlreadyExistsError("event", event_id)
                return deepcopy(existing), False
        else:
            event_id = self._new_id_locked(self._ID_PREFIXES["event"])
            event["event_id"] = event_id
        next_seq = len(self._events[run_id]) + 1
        event["seq"] = next_seq
        event.setdefault("run_revision", run.get("run_revision", 1))
        self._events[run_id].append(event)
        self._event_ids[run_id][event_id] = event
        run["snapshot_seq"] = next_seq
        return deepcopy(event), True

    def _append_webhook_deliveries_locked(self, event: Mapping[str, Any]) -> None:
        run_id = str(event["run_id"])
        run = self._runs[run_id]
        tenant_id = run.get("tenant_id")
        event_type = event.get("type")
        occurred_at = event.get("occurred_at")
        for endpoint in self._webhook_endpoints.values():
            if endpoint.get("tenant_id") != tenant_id or endpoint.get("status") != "ACTIVE":
                continue
            event_types = set(endpoint.get("event_types") or [])
            if "*" not in event_types and event_type not in event_types:
                continue
            endpoint_id = str(endpoint["webhook_endpoint_id"])
            deliveries = self._webhook_deliveries.setdefault(endpoint_id, {})
            if any(item.get("event_id") == event.get("event_id") for item in deliveries.values()):
                continue
            self._create_locked(
                deliveries,
                {
                    "tenant_id": tenant_id,
                    "event_id": event["event_id"],
                    "event_type": event_type,
                    "run_id": run_id,
                    "status": "PENDING",
                    "attempt_count": 0,
                    "first_attempt_at": None,
                    "next_attempt_at": occurred_at,
                    "last_response_status": None,
                    "last_problem": None,
                    "created_at": occurred_at,
                    "delivered_at": None,
                    "exhausted_at": None,
                    "redeliver_until": None,
                },
                id_field="delivery_id",
                kind="webhook_delivery",
                resource="webhook_delivery",
                fixed_fields={"webhook_endpoint_id": endpoint_id},
            )

    async def append_event(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            event, created = self._append_event_locked(run_id, value)
            if created:
                self._append_webhook_deliveries_locked(event)
            conditions = self._event_conditions_for_run_locked(run_id) if created else []
        if created:
            self._schedule_notifications(conditions)
        return event

    async def mutate_run_with_event(
        self,
        run_id: str,
        updates: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        bump_revision: bool = True,
    ) -> tuple[JsonDict, JsonDict]:
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise ResourceNotFoundError("run", run_id)
            revision = int(current.get("run_revision", 1))
            if expected_revision is not None and expected_revision != revision:
                raise RevisionConflictError(expected_revision, revision)
            patch = self._mapping(updates, name="run updates")
            patch.pop("run_id", None)
            patch.pop("run_revision", None)
            patch.pop("snapshot_seq", None)
            current.update(patch)
            if bump_revision:
                current["run_revision"] = revision + 1
            event_value = self._mapping(event, name="event")
            event_value["run_revision"] = current["run_revision"]
            stored_event, created = self._append_event_locked(run_id, event_value)
            if created:
                self._append_webhook_deliveries_locked(stored_event)
            conditions = self._event_conditions_for_run_locked(run_id) if created else []
            stored_run = deepcopy(current)
        if created:
            self._schedule_notifications(conditions)
        return stored_run, stored_event

    async def get_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        types: Iterable[str] | None = None,
    ) -> list[JsonDict]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        allowed_types = set(types) if types is not None else None
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            selected = [
                event
                for event in self._events[run_id]
                if event["seq"] > after_seq
                and (allowed_types is None or event.get("type") in allowed_types)
            ]
            if limit is not None:
                selected = selected[:limit]
            return deepcopy(selected)

    async def wait_for_events(
        self,
        run_id: str,
        after_seq: int,
        *,
        timeout: float | None = 15.0,
        limit: int | None = None,
        types: Iterable[str] | None = None,
    ) -> list[JsonDict]:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        condition = self._event_condition(run_id)
        deadline = None if timeout is None else monotonic() + timeout
        async with condition:
            while True:
                events = await self.get_events(
                    run_id, after_seq=after_seq, limit=limit, types=types
                )
                if events:
                    return events
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                try:
                    if remaining is None:
                        await condition.wait()
                    else:
                        await asyncio.wait_for(condition.wait(), timeout=remaining)
                except TimeoutError:
                    return []

    def event_condition(self, run_id: str) -> asyncio.Condition:
        """Return the condition used by SSE consumers in the current event loop."""

        return self._event_condition(run_id)

    def _event_condition(self, run_id: str) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        key = (run_id, loop)
        with self._lock:
            condition = self._event_conditions.get(key)
            if condition is None:
                condition = asyncio.Condition()
                self._event_conditions[key] = condition
            return condition

    def _event_conditions_for_run_locked(
        self, run_id: str
    ) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Condition]]:
        return [
            (loop, condition)
            for (condition_run_id, loop), condition in self._event_conditions.items()
            if condition_run_id == run_id
        ]

    # Stable intermediate and final resources.

    async def create_output(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return self._create_locked(
                self._outputs[run_id],
                value,
                id_field="output_id",
                kind="output",
                resource="output",
                fixed_fields={"run_id": run_id},
            )

    async def get_output(self, run_id: str, output_id: str) -> JsonDict | None:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return self._get_locked(self._outputs[run_id], output_id)

    async def list_outputs(self, run_id: str) -> list[JsonDict]:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return deepcopy(list(self._outputs[run_id].values()))

    async def create_decision_packet(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            item = self._mapping(value, name="decision_packet")
            item.setdefault("wait_set_id", self._new_id_locked(self._ID_PREFIXES["wait_set"]))
            return self._create_locked(
                self._decision_packets[run_id],
                item,
                id_field="decision_packet_id",
                kind="decision_packet",
                resource="decision_packet",
                fixed_fields={"run_id": run_id},
            )

    async def get_decision_packet(self, run_id: str, decision_packet_id: str) -> JsonDict | None:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return self._get_locked(self._decision_packets[run_id], decision_packet_id)

    async def get_decision_packet_by_wait_set(
        self, run_id: str, wait_set_id: str
    ) -> JsonDict | None:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            for packet in reversed(list(self._decision_packets[run_id].values())):
                if packet.get("wait_set_id") == wait_set_id:
                    return deepcopy(packet)
            return None

    async def update_decision_packet(
        self,
        run_id: str,
        decision_packet_id: str,
        updates: Mapping[str, Any],
    ) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            current = self._decision_packets[run_id].get(decision_packet_id)
            if current is None:
                raise ResourceNotFoundError("decision_packet", decision_packet_id)
            supplied_run_id = updates.get("run_id", run_id)
            if supplied_run_id != run_id:
                raise ValueError("decision_packet.run_id cannot be changed")
            current_wait_set = current.get("wait_set_id")
            if updates.get("wait_set_id", current_wait_set) != current_wait_set:
                raise ValueError("decision_packet.wait_set_id cannot be changed")
            return self._update_locked(
                self._decision_packets[run_id],
                decision_packet_id,
                updates,
                id_field="decision_packet_id",
                resource="decision_packet",
            )

    async def list_decision_packets(self, run_id: str) -> list[JsonDict]:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return deepcopy(list(self._decision_packets[run_id].values()))

    async def create_command(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            item = self._mapping(value, name="command")
            run_id = item.get("run_id")
            if run_id is not None and run_id not in self._runs:
                raise ResourceNotFoundError("run", str(run_id))
            return self._create_locked(
                self._commands,
                item,
                id_field="command_id",
                kind="command",
                resource="command",
            )

    async def get_command(self, command_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._commands, command_id)

    async def update_command(self, command_id: str, updates: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._update_locked(
                self._commands,
                command_id,
                updates,
                id_field="command_id",
                resource="command",
            )

    async def list_commands(self, run_id: str | None = None) -> list[JsonDict]:
        with self._lock:
            values = self._commands.values()
            if run_id is not None:
                values = (value for value in values if value.get("run_id") == run_id)
            return deepcopy(list(values))

    async def set_result(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            result = self._mapping(value, name="result")
            supplied_run_id = result.get("run_id")
            if supplied_run_id is not None and supplied_run_id != run_id:
                raise ValueError("result.run_id does not match its parent run")
            result["run_id"] = run_id
            result.setdefault(
                "result_manifest_id", self._new_id_locked(self._ID_PREFIXES["result"])
            )
            existing = self._results.get(run_id)
            if existing is not None and existing != result:
                raise ResourceAlreadyExistsError("result", run_id)
            self._results[run_id] = result
            return deepcopy(result)

    async def get_result(self, run_id: str) -> JsonDict | None:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return self._get_locked(self._results, run_id)

    async def create_artifact(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            item = self._mapping(value, name="artifact")
            run_id = item.get("run_id")
            if run_id is not None and run_id not in self._runs:
                raise ResourceNotFoundError("run", str(run_id))
            return self._create_locked(
                self._artifacts,
                item,
                id_field="artifact_id",
                kind="artifact",
                resource="artifact",
            )

    async def get_artifact(self, artifact_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._artifacts, artifact_id)

    async def update_artifact(self, artifact_id: str, updates: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._update_locked(
                self._artifacts,
                artifact_id,
                updates,
                id_field="artifact_id",
                resource="artifact",
            )

    async def list_artifacts(self, run_id: str | None = None) -> list[JsonDict]:
        with self._lock:
            values = self._artifacts.values()
            if run_id is not None:
                values = (value for value in values if value.get("run_id") == run_id)
            return deepcopy(list(values))

    async def create_approval(self, run_id: str, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            self._approvals.setdefault(run_id, {})
            return self._create_locked(
                self._approvals[run_id],
                value,
                id_field="approval_id",
                kind="approval",
                resource="approval",
                fixed_fields={"run_id": run_id},
            )

    async def get_approval(self, run_id: str, approval_id: str) -> JsonDict | None:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return self._get_locked(self._approvals.setdefault(run_id, {}), approval_id)

    async def update_approval(
        self, run_id: str, approval_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            supplied_run_id = updates.get("run_id", run_id)
            if supplied_run_id != run_id:
                raise ValueError("approval.run_id cannot be changed")
            return self._update_locked(
                self._approvals.setdefault(run_id, {}),
                approval_id,
                updates,
                id_field="approval_id",
                resource="approval",
            )

    async def list_approvals(self, run_id: str) -> list[JsonDict]:
        with self._lock:
            if run_id not in self._runs:
                raise ResourceNotFoundError("run", run_id)
            return deepcopy(list(self._approvals.setdefault(run_id, {}).values()))

    async def create_model(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            item = self._mapping(value, name="model")
            run_id = item.get("run_id")
            if run_id is not None and run_id not in self._runs:
                raise ResourceNotFoundError("run", str(run_id))
            return self._create_locked(
                self._models,
                item,
                id_field="model_id",
                kind="model",
                resource="model",
            )

    async def get_model(self, model_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._models, model_id)

    async def list_models(self, run_id: str | None = None) -> list[JsonDict]:
        with self._lock:
            values = self._models.values()
            if run_id is not None:
                values = (value for value in values if value.get("run_id") == run_id)
            return deepcopy(list(values))

    async def create_webhook_endpoint(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._create_locked(
                self._webhook_endpoints,
                value,
                id_field="webhook_endpoint_id",
                kind="webhook_endpoint",
                resource="webhook_endpoint",
            )

    async def get_webhook_endpoint(self, webhook_endpoint_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._webhook_endpoints, webhook_endpoint_id)

    async def update_webhook_endpoint(
        self, webhook_endpoint_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        with self._lock:
            return self._update_locked(
                self._webhook_endpoints,
                webhook_endpoint_id,
                updates,
                id_field="webhook_endpoint_id",
                resource="webhook_endpoint",
            )

    async def list_webhook_endpoints(self, tenant_id: str | None = None) -> list[JsonDict]:
        with self._lock:
            values = self._webhook_endpoints.values()
            if tenant_id is not None:
                values = (value for value in values if value.get("tenant_id") == tenant_id)
            return deepcopy(list(values))

    async def create_webhook_delivery(
        self, webhook_endpoint_id: str, value: Mapping[str, Any]
    ) -> JsonDict:
        with self._lock:
            if webhook_endpoint_id not in self._webhook_endpoints:
                raise ResourceNotFoundError("webhook_endpoint", webhook_endpoint_id)
            self._webhook_deliveries.setdefault(webhook_endpoint_id, {})
            return self._create_locked(
                self._webhook_deliveries[webhook_endpoint_id],
                value,
                id_field="delivery_id",
                kind="webhook_delivery",
                resource="webhook_delivery",
                fixed_fields={"webhook_endpoint_id": webhook_endpoint_id},
            )

    async def get_webhook_delivery(
        self, webhook_endpoint_id: str, delivery_id: str
    ) -> JsonDict | None:
        with self._lock:
            if webhook_endpoint_id not in self._webhook_endpoints:
                raise ResourceNotFoundError("webhook_endpoint", webhook_endpoint_id)
            return self._get_locked(
                self._webhook_deliveries.setdefault(webhook_endpoint_id, {}), delivery_id
            )

    async def update_webhook_delivery(
        self, webhook_endpoint_id: str, delivery_id: str, updates: Mapping[str, Any]
    ) -> JsonDict:
        with self._lock:
            if webhook_endpoint_id not in self._webhook_endpoints:
                raise ResourceNotFoundError("webhook_endpoint", webhook_endpoint_id)
            supplied_endpoint_id = updates.get("webhook_endpoint_id", webhook_endpoint_id)
            if supplied_endpoint_id != webhook_endpoint_id:
                raise ValueError("webhook_delivery.webhook_endpoint_id cannot be changed")
            return self._update_locked(
                self._webhook_deliveries.setdefault(webhook_endpoint_id, {}),
                delivery_id,
                updates,
                id_field="delivery_id",
                resource="webhook_delivery",
            )

    async def list_webhook_deliveries(self, webhook_endpoint_id: str) -> list[JsonDict]:
        with self._lock:
            if webhook_endpoint_id not in self._webhook_endpoints:
                raise ResourceNotFoundError("webhook_endpoint", webhook_endpoint_id)
            return deepcopy(
                list(self._webhook_deliveries.setdefault(webhook_endpoint_id, {}).values())
            )

    async def create_deletion(self, value: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._create_locked(
                self._deletions,
                value,
                id_field="deletion_id",
                kind="deletion",
                resource="deletion",
            )

    async def get_deletion(self, deletion_id: str) -> JsonDict | None:
        with self._lock:
            return self._get_locked(self._deletions, deletion_id)

    async def update_deletion(self, deletion_id: str, updates: Mapping[str, Any]) -> JsonDict:
        with self._lock:
            return self._update_locked(
                self._deletions,
                deletion_id,
                updates,
                id_field="deletion_id",
                resource="deletion",
            )

    # Idempotency reservation and exact response replay.

    async def begin_idempotent_request(
        self, operation: str, key: str, request_fingerprint: str
    ) -> IdempotencyDecision:
        identity = self._idempotency_identity(operation, key, request_fingerprint)
        with self._lock:
            record = self._idempotency.get(identity)
            if record is None:
                self._idempotency[identity] = _IdempotencyRecord(request_fingerprint)
                return IdempotencyDecision(
                    IdempotencyState.NEW, request_fingerprint=request_fingerprint
                )
            return self._idempotency_decision(record, request_fingerprint)

    async def get_idempotency(
        self, operation: str, key: str, request_fingerprint: str
    ) -> IdempotencyDecision:
        identity = self._idempotency_identity(operation, key, request_fingerprint)
        with self._lock:
            record = self._idempotency.get(identity)
            if record is None:
                return IdempotencyDecision(
                    IdempotencyState.MISS, request_fingerprint=request_fingerprint
                )
            return self._idempotency_decision(record, request_fingerprint)

    @staticmethod
    def _idempotency_decision(
        record: _IdempotencyRecord, request_fingerprint: str
    ) -> IdempotencyDecision:
        if record.request_fingerprint != request_fingerprint:
            return IdempotencyDecision(
                IdempotencyState.CONFLICT,
                request_fingerprint=request_fingerprint,
                existing_fingerprint=record.request_fingerprint,
            )
        if record.response is None:
            return IdempotencyDecision(
                IdempotencyState.IN_PROGRESS,
                request_fingerprint=request_fingerprint,
            )
        return IdempotencyDecision(
            IdempotencyState.REPLAY,
            request_fingerprint=request_fingerprint,
            response=record.response.clone(),
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
        identity = self._idempotency_identity(operation, key, request_fingerprint)
        with self._lock:
            record = self._idempotency.get(identity)
            if record is None:
                record = _IdempotencyRecord(request_fingerprint)
                self._idempotency[identity] = record
            if record.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    operation,
                    key,
                    request_fingerprint,
                    record.request_fingerprint,
                )
            if record.response is None:
                record.response = StoredResponse(
                    status_code=status_code,
                    body=deepcopy(body),
                    headers=deepcopy(dict(headers or {})),
                )
            response = record.response.clone()
            conditions = self._idempotency_conditions_for_key_locked(operation, key)
        self._schedule_notifications(conditions)
        return response

    async def wait_for_idempotent_response(
        self,
        operation: str,
        key: str,
        request_fingerprint: str,
        *,
        timeout: float | None = 30.0,
    ) -> IdempotencyDecision:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        condition = self._idempotency_condition(operation, key)
        deadline = None if timeout is None else monotonic() + timeout
        async with condition:
            while True:
                decision = await self.get_idempotency(operation, key, request_fingerprint)
                if decision.state is not IdempotencyState.IN_PROGRESS:
                    return decision
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return decision
                try:
                    if remaining is None:
                        await condition.wait()
                    else:
                        await asyncio.wait_for(condition.wait(), timeout=remaining)
                except TimeoutError:
                    return await self.get_idempotency(operation, key, request_fingerprint)

    async def abort_idempotent_request(
        self, operation: str, key: str, request_fingerprint: str
    ) -> bool:
        identity = self._idempotency_identity(operation, key, request_fingerprint)
        with self._lock:
            record = self._idempotency.get(identity)
            if record is None:
                return False
            if record.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    operation,
                    key,
                    request_fingerprint,
                    record.request_fingerprint,
                )
            if record.response is not None:
                return False
            del self._idempotency[identity]
            conditions = self._idempotency_conditions_for_key_locked(operation, key)
        self._schedule_notifications(conditions)
        return True

    @staticmethod
    def _idempotency_identity(
        operation: str, key: str, request_fingerprint: str
    ) -> tuple[str, str]:
        if not operation:
            raise ValueError("operation must not be empty")
        if not key:
            raise ValueError("idempotency key must not be empty")
        if not request_fingerprint:
            raise ValueError("request fingerprint must not be empty")
        return operation, key

    def _idempotency_condition(self, operation: str, key: str) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        identity = (operation, key, loop)
        with self._lock:
            condition = self._idempotency_conditions.get(identity)
            if condition is None:
                condition = asyncio.Condition()
                self._idempotency_conditions[identity] = condition
            return condition

    def _idempotency_conditions_for_key_locked(
        self, operation: str, key: str
    ) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Condition]]:
        return [
            (loop, condition)
            for (
                condition_operation,
                condition_key,
                loop,
            ), condition in self._idempotency_conditions.items()
            if condition_operation == operation and condition_key == key
        ]

    @staticmethod
    async def _notify(condition: asyncio.Condition) -> None:
        async with condition:
            condition.notify_all()

    @classmethod
    def _schedule_notifications(
        cls,
        conditions: Iterable[tuple[asyncio.AbstractEventLoop, asyncio.Condition]],
    ) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for loop, condition in conditions:
            if loop.is_closed():
                continue

            def schedule(
                target_loop: asyncio.AbstractEventLoop = loop,
                target_condition: asyncio.Condition = condition,
            ) -> None:
                target_loop.create_task(cls._notify(target_condition))

            if loop is current_loop:
                schedule()
            else:
                try:
                    loop.call_soon_threadsafe(schedule)
                except RuntimeError:
                    continue

    async def reset(self) -> None:
        """Clear all process-local state and reset deterministic ID counters."""

        with self._lock:
            conditions = list(self._event_conditions.values())
            idempotency_conditions = list(self._idempotency_conditions.values())
            event_notifications = [
                (loop, condition) for (_run_id, loop), condition in self._event_conditions.items()
            ]
            idempotency_notifications = [
                (loop, condition)
                for (_operation, _key, loop), condition in self._idempotency_conditions.items()
            ]
            self._id_counters.clear()
            self._datasets.clear()
            self._dataset_versions.clear()
            self._runs.clear()
            self._events.clear()
            self._event_ids.clear()
            self._outputs.clear()
            self._decision_packets.clear()
            self._commands.clear()
            self._results.clear()
            self._artifacts.clear()
            self._approvals.clear()
            self._models.clear()
            self._webhook_endpoints.clear()
            self._webhook_deliveries.clear()
            self._deletions.clear()
            self._idempotency.clear()
            self._event_conditions.clear()
            self._idempotency_conditions.clear()
        # Keep local references alive until every waiter has been scheduled.
        _ = conditions, idempotency_conditions
        self._schedule_notifications(event_notifications)
        self._schedule_notifications(idempotency_notifications)


MemoryStore = InMemoryStore
IdempotencyConflict = IdempotencyConflictError

store = InMemoryStore()


async def reset_store() -> None:
    await store.reset()
