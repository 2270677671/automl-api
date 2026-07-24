from __future__ import annotations

import asyncio
from datetime import timedelta
from collections.abc import AsyncIterable
from typing import Any, Iterable

from .auth import Principal
from .backends import (
    BackendMediaTypeUnsupportedError,
    BackendNotFoundError,
    BackendRegistry,
    BackendTaskUnsupportedError,
    BackendUnavailableError,
    default_backend_registry,
)
from .errors import APIProblem
from .limits import RuntimeLimits
from .protocol import iso_now, utcnow
from .storage import BlobInfo, BlobSizeLimitExceeded, BlobStore, BlobStoreError, SyntheticBlobStore
from .store import InMemoryStore, store


RUN_PUBLIC_FIELDS = {
    "api_version",
    "run_id",
    "dataset_version_id",
    "phase",
    "status",
    "outcome",
    "plan_version",
    "run_revision",
    "snapshot_seq",
    "retained_from_seq",
    "contract_versions",
    "progress",
    "stages",
    "blocking",
    "latest_output_refs",
    "available_actions",
    "budget_usage",
    "created_at",
    "updated_at",
    "links",
}


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if key in RUN_PUBLIC_FIELDS}


def _page(items: list[dict[str, Any]], high_watermark: int | None = None) -> dict[str, Any]:
    return {
        "items": items,
        "page": {
            "next_cursor": None,
            "has_more": False,
            "high_watermark": len(items) if high_watermark is None else high_watermark,
        },
    }


def _stages(
    *, active_phase: str, active_status: str, completed: Iterable[str] = ()
) -> list[dict[str, Any]]:
    now = iso_now()
    completed_set = set(completed)
    phases = ["INGEST", "PROFILE", "PLAN", "TRAIN", "EVALUATE", "PACKAGE"]
    result: list[dict[str, Any]] = []
    for phase in phases:
        if phase in completed_set:
            status, percent, started, ended = "COMPLETED", 100.0, now, now
        elif phase == active_phase:
            status = "WAITING" if active_status in {"WAITING_USER", "PAUSED"} else "RUNNING"
            percent, started, ended = 35.0, now, None
        else:
            status, percent, started, ended = "PENDING", 0.0, None, None
        result.append(
            {
                "phase": phase,
                "status": status,
                "progress_percent": percent,
                "started_at": started,
                "completed_at": ended,
                "message": f"{phase.lower()} stage",
                "latest_output_refs": [],
            }
        )
    return result


class WorkflowService:
    """Milestone 1 workflow adapter.

    The critical section emulates the visibility boundary that a Temporal + projection
    implementation will provide later. Store writes remain individually inspectable in tests.
    """

    def __init__(
        self,
        state: InMemoryStore = store,
        *,
        blob_store: BlobStore | None = None,
        limits: RuntimeLimits | None = None,
        backend_registry: BackendRegistry | None = None,
    ) -> None:
        self.store = state
        self.blob_store = blob_store or SyntheticBlobStore()
        self.limits = limits or RuntimeLimits()
        self.backend_registry = backend_registry or default_backend_registry
        self._visibility_lock = asyncio.Lock()

    def _require_requested_backend(
        self,
        request: dict[str, Any],
        *,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        objective = request.get("objective") or {}
        backend_id = objective.get("backend_id") or self.backend_registry.default_backend_id
        try:
            backend = self.backend_registry.validate_request(
                backend_id,
                task_type=objective.get("task_type"),
                media_type=media_type,
            )
        except BackendNotFoundError as error:
            raise APIProblem(
                status=422,
                code="backend_not_found",
                title="AutoML backend not found",
                detail=str(error),
                extras=error.context,
            ) from error
        except BackendUnavailableError as error:
            raise APIProblem(
                status=409,
                code="backend_unavailable",
                title="AutoML backend unavailable",
                detail=str(error),
                extras=error.context,
            ) from error
        except (BackendTaskUnsupportedError, BackendMediaTypeUnsupportedError) as error:
            raise APIProblem(
                status=422,
                code=error.code.lower(),
                title="AutoML backend capability unsupported",
                detail=str(error),
                extras=error.context,
            ) from error
        return backend.descriptor.as_dict()

    @staticmethod
    def _owned(resource: dict[str, Any] | None, principal: Principal) -> dict[str, Any]:
        if resource is None or resource.get("tenant_id") != principal.tenant_id:
            raise APIProblem(
                status=404,
                code="not_found",
                title="Resource not found",
                detail="The resource does not exist or is not visible to this principal.",
            )
        return resource

    async def create_dataset(
        self,
        principal: Principal,
        request: dict[str, Any],
        *,
        public_base_url: str = "http://localhost",
    ) -> dict[str, Any]:
        async with self._visibility_lock:
            await self._enforce_dataset_limits(principal, request)
            now = iso_now()
            upload_id = self.store.new_id("upload")
            expires_at = (utcnow() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            dataset, version = await self.store.create_dataset_with_version(
                {
                    "tenant_id": principal.tenant_id,
                    "name": request["name"],
                    "retention_days": request.get("retention_days") or 30,
                    "created_at": now,
                },
                {
                    "tenant_id": principal.tenant_id,
                    "status": "UPLOADING",
                    "revision": 1,
                    "media_type": request["media_type"],
                    "size_bytes": request["size_bytes"],
                    "filename": request["filename"],
                    "upload_id": upload_id,
                    "upload_expires_at": expires_at,
                    "sha256": None,
                    "validation_issues": [],
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return {
                "dataset_id": dataset["dataset_id"],
                "dataset_version_id": version["dataset_version_id"],
                "status": version["status"],
                "upload_id": upload_id,
                "expires_at": expires_at,
                "parts": [
                    self._upload_part(
                        public_base_url,
                        version["dataset_version_id"],
                        upload_id,
                        1,
                        expires_at,
                    )
                ],
            }

    def _upload_part(
        self,
        public_base_url: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
        expires_at: str,
    ) -> dict[str, Any]:
        return {
            "part_number": part_number,
            "url": self.blob_store.upload_url(
                public_base_url, dataset_version_id, upload_id, part_number
            ),
            "expires_at": expires_at,
            "required_headers": {"x-automl-upload-part": str(part_number)},
        }

    async def sign_upload_parts(
        self,
        principal: Principal,
        dataset_version_id: str,
        request: dict[str, Any],
        *,
        public_base_url: str = "http://localhost",
    ) -> dict[str, Any]:
        version = self._owned(await self.store.get_dataset_version(dataset_version_id), principal)
        if version["status"] != "UPLOADING" or request["upload_id"] != version["upload_id"]:
            raise APIProblem(
                status=409,
                code="upload_not_active",
                title="Upload is not active",
                detail="The upload session is no longer active for this dataset version.",
            )
        expires_at = (utcnow() + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        return {
            "upload_id": version["upload_id"],
            "expires_at": expires_at,
            "parts": [
                self._upload_part(
                    public_base_url,
                    dataset_version_id,
                    version["upload_id"],
                    int(part),
                    expires_at,
                )
                for part in sorted(request["part_numbers"])
            ],
        }

    async def put_upload_part(
        self,
        principal: Principal,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterable[bytes],
    ) -> BlobInfo:
        version = self._owned(await self.store.get_dataset_version(dataset_version_id), principal)
        if version["status"] != "UPLOADING" or upload_id != version["upload_id"]:
            raise APIProblem(
                status=409,
                code="upload_not_active",
                title="Upload is not active",
                detail="The upload session is no longer active for this dataset version.",
            )
        expires_at = version.get("upload_expires_at")
        if isinstance(expires_at, str) and expires_at < iso_now():
            raise APIProblem(
                status=410,
                code="upload_expired",
                title="Upload session expired",
                detail="Create a new dataset upload session.",
            )
        try:
            return await self.blob_store.put_upload_part(
                tenant_id=principal.tenant_id,
                dataset_version_id=dataset_version_id,
                upload_id=upload_id,
                part_number=part_number,
                chunks=chunks,
            )
        except BlobSizeLimitExceeded as error:
            raise APIProblem(
                status=413,
                code="upload_part_too_large",
                title="Upload part is too large",
                detail="The uploaded part exceeds this service profile's byte limit.",
                extras={"runtime_limits": self.limits.manifest()},
            ) from error
        except BlobStoreError as error:
            raise APIProblem(
                status=422,
                code="upload_failed",
                title="Upload failed",
                detail=str(error),
            ) from error

    async def finalize_dataset(
        self, principal: Principal, dataset_version_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._visibility_lock:
            version = self._owned(
                await self.store.get_dataset_version(dataset_version_id), principal
            )
            if version["status"] == "READY" and version.get("sha256") == request["sha256"]:
                return self.public_dataset_version(version)
            if version["status"] != "UPLOADING" or request["upload_id"] != version["upload_id"]:
                raise APIProblem(
                    status=409,
                    code="upload_not_active",
                    title="Upload is not active",
                    detail="The dataset version cannot be finalized from its current state.",
                )
            try:
                blob = await self.blob_store.finalize_upload(
                    tenant_id=principal.tenant_id,
                    dataset_version_id=dataset_version_id,
                    upload_id=request["upload_id"],
                    parts=request["parts"],
                    expected_size=int(version["size_bytes"]),
                    expected_sha256=request["sha256"],
                )
            except BlobStoreError as error:
                raise APIProblem(
                    status=422,
                    code="upload_integrity_failed",
                    title="Upload integrity validation failed",
                    detail=str(error),
                ) from error
            updated = await self.store.update_dataset_version(
                dataset_version_id,
                {
                    "status": "READY",
                    "revision": int(version["revision"]) + 1,
                    "sha256": request["sha256"],
                    "blob_key": blob.key,
                    "updated_at": iso_now(),
                },
            )
            return self.public_dataset_version(updated)

    @staticmethod
    def public_dataset_version(version: dict[str, Any]) -> dict[str, Any]:
        keys = {
            "dataset_id",
            "dataset_version_id",
            "status",
            "revision",
            "media_type",
            "size_bytes",
            "sha256",
            "validation_issues",
            "created_at",
            "updated_at",
        }
        return {key: value for key, value in version.items() if key in keys and value is not None}

    async def get_dataset_version(
        self, principal: Principal, dataset_version_id: str
    ) -> dict[str, Any]:
        version = self._owned(await self.store.get_dataset_version(dataset_version_id), principal)
        if version.get("status") == "DELETED":
            raise APIProblem(
                404,
                "not_found",
                "Dataset version not found",
                "The dataset version has been deleted or is not visible to this principal.",
            )
        return self.public_dataset_version(version)

    async def create_run(self, principal: Principal, request: dict[str, Any]) -> dict[str, Any]:
        async with self._visibility_lock:
            await self._enforce_run_limits(principal, request)
            version = self._owned(
                await self.store.get_dataset_version(request["dataset_version_id"]), principal
            )
            if version["status"] != "READY":
                raise APIProblem(
                    status=409,
                    code="dataset_not_ready",
                    title="Dataset is not ready",
                    detail="Finalize the dataset and wait for READY before creating a Run.",
                )
            backend = self._require_requested_backend(
                request,
                media_type=version.get("media_type"),
            )

            run_id = self.store.new_id("run")
            now = iso_now()
            budget = request["budget"]
            run = await self.store.create_run(
                {
                    "run_id": run_id,
                    "tenant_id": principal.tenant_id,
                    "api_version": "v1",
                    "dataset_version_id": version["dataset_version_id"],
                    "phase": "INGEST",
                    "status": "QUEUED",
                    "outcome": None,
                    "plan_version": 1,
                    "run_revision": 1,
                    "snapshot_seq": 0,
                    "retained_from_seq": 1,
                    "contract_versions": {
                        "event_schema": "1.0",
                        "output_schema": "1.0",
                        "policy_version": "policy.m1",
                        "tool_versions": {"synthetic_profiler": "1.0"},
                    },
                    "progress": self._progress(0, "QUEUED", "Run accepted"),
                    "stages": _stages(active_phase="INGEST", active_status="RUNNING"),
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                    "latest_output_refs": [],
                    "available_actions": ["CANCEL"],
                    "budget_usage": {
                        "compute_credits": {"used": 0, "limit": budget["max_compute_credits"]},
                        "trials": {"used": 0, "limit": budget["max_trials"]},
                        "wall_time_seconds": {"used": 0, "limit": budget["max_wall_time_seconds"]},
                        "llm_tokens": {"used": 0, "limit": budget["max_llm_tokens"]},
                    },
                    "created_at": now,
                    "updated_at": now,
                    "links": self._run_links(run_id),
                    "autonomy": request["autonomy"],
                    "objective": request["objective"],
                    "backend_id": backend["backend_id"],
                    "policy": request["policy"],
                }
            )

            run = await self.store.update_run(
                run_id,
                {
                    "phase": "PROFILE",
                    "status": "RUNNING",
                    "progress": self._progress(15, "PROFILE", "Synthetic profile committed"),
                    "stages": _stages(
                        active_phase="PROFILE", active_status="RUNNING", completed={"INGEST"}
                    ),
                    "updated_at": iso_now(),
                },
                bump_revision=False,
            )
            run = await self._emit(
                run,
                "run.phase_changed.v1",
                {"previous_phase": "INGEST", "phase": "PROFILE", "status": "RUNNING"},
            )
            quality_output, run = await self._commit_output(
                run,
                output_type="DATA_QUALITY_REPORT",
                phase="PROFILE",
                summary={
                    "code": "SYNTHETIC_PROFILE_READY",
                    "message": "Milestone 1 metadata profile is ready.",
                    "severity": "INFO",
                },
                payload={
                    "kind": "DATA_QUALITY_REPORT",
                    "row_count": 0,
                    "column_count": 0,
                    "quality_score": 100,
                    "issues": [],
                },
            )

            decision_packet_id = self.store.new_id("decision_packet")
            wait_set_id = self.store.new_id("wait_set")
            run = await self.store.update_run(
                run_id,
                {
                    "phase": "PLAN",
                    "status": "WAITING_USER",
                    "progress": self._progress(35, "WAITING_USER", "Target column required"),
                    "stages": _stages(
                        active_phase="PLAN",
                        active_status="WAITING_USER",
                        completed={"INGEST", "PROFILE"},
                    ),
                    "blocking": {
                        "decision_packet_ids": [decision_packet_id],
                        "approval_ids": [],
                    },
                    "latest_output_refs": [self._output_ref(quality_output)],
                    "available_actions": ["ANSWER", "PAUSE", "CANCEL"],
                    "updated_at": iso_now(),
                },
                expected_revision=run["run_revision"],
                bump_revision=True,
            )
            packet = await self.store.create_decision_packet(
                run_id,
                {
                    "decision_packet_id": decision_packet_id,
                    "wait_set_id": wait_set_id,
                    "wait_set_revision": 1,
                    "run_id": run_id,
                    "tenant_id": principal.tenant_id,
                    "run_revision": run["run_revision"],
                    "status": "OPEN",
                    "kind": "CLARIFICATION",
                    "reason": "A target column cannot be inferred safely from metadata alone.",
                    "blocking": True,
                    "resolution_policy": "HUMAN_REQUIRED",
                    "questions": [
                        {
                            "question_id": "q_target",
                            "prompt": "Which column should the synthetic workflow treat as the target?",
                            "answer_schema": {"type": "string", "minLength": 1},
                            "selection_mode": "FREEFORM",
                            "min_selections": 1,
                            "max_selections": 1,
                            "options": [],
                            "recommendation": None,
                            "recommendation_reason": "Milestone 1 never guesses a target column.",
                        }
                    ],
                    "evidence_refs": [quality_output["output_id"]],
                    "created_at": iso_now(),
                    "expires_at": (utcnow() + timedelta(hours=24))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            run = await self._emit(
                run,
                "run.phase_changed.v1",
                {"previous_phase": "PROFILE", "phase": "PLAN", "status": "WAITING_USER"},
            )
            run = await self._emit(
                run,
                "decision_packet.requested.v1",
                {
                    "decision_packet_id": packet["decision_packet_id"],
                    "wait_set_id": packet["wait_set_id"],
                    "href": f"/v1/runs/{run_id}/decision-packets",
                },
            )
            return _public_run(run)

    async def _emit(
        self, run: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = await self.store.append_event(
            run["run_id"],
            {
                "event_id": self.store.new_id("event"),
                "run_id": run["run_id"],
                "seq": int(run["snapshot_seq"]) + 1,
                "run_revision": run["run_revision"],
                "schema_version": "1.0",
                "occurred_at": iso_now(),
                "type": event_type,
                "payload": payload,
                "links": {"run": f"/v1/runs/{run['run_id']}"},
            },
        )
        return await self.store.update_run(
            run["run_id"],
            {"snapshot_seq": event["seq"], "updated_at": iso_now()},
            bump_revision=False,
        )

    async def _commit_output(
        self,
        run: dict[str, Any],
        *,
        output_type: str,
        phase: str,
        summary: dict[str, Any],
        payload: dict[str, Any],
        artifact_refs: list[dict[str, Any]] | None = None,
        supersedes: str | None = None,
        lineage: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        output_id = self.store.new_id("output")
        output_lineage = {
            "dataset_version_id": run["dataset_version_id"],
            "task_spec_output_id": run.get("task_spec_output_id"),
            "split_manifest_output_id": run.get("split_manifest_output_id"),
            "policy_version": run.get("contract_versions", {}).get("policy_version", "policy.m1"),
            "method_version": run.get("method_version", "synthetic.v1"),
            "parent_refs": [],
            "evidence_refs": [],
        }
        output_lineage.update(lineage or {})
        output = await self.store.create_output(
            run["run_id"],
            {
                "output_id": output_id,
                "tenant_id": run["tenant_id"],
                "schema_version": "1.0",
                "run_id": run["run_id"],
                "run_revision": run["run_revision"],
                "created_seq": int(run["snapshot_seq"]) + 1,
                "phase": phase,
                "state": "FINAL",
                "type": output_type,
                "summary": summary,
                "payload": payload,
                "lineage": output_lineage,
                "artifact_refs": artifact_refs or [],
                "supersedes": supersedes,
                "created_at": iso_now(),
            },
        )
        run = await self._emit(
            run,
            "output.committed.v1",
            {"output": self._output_ref(output)},
        )
        refs = [*run.get("latest_output_refs", [])]
        refs = [ref for ref in refs if ref["type"] != output_type]
        refs.append(self._output_ref(output))
        run = await self.store.update_run(
            run["run_id"], {"latest_output_refs": refs}, bump_revision=False
        )
        return output, run

    @staticmethod
    def _output_ref(output: dict[str, Any]) -> dict[str, Any]:
        return {
            "output_id": output["output_id"],
            "type": output["type"],
            "state": output["state"],
            "href": f"/v1/runs/{output['run_id']}/outputs/{output['output_id']}",
        }

    @staticmethod
    def _progress(percent: float, code: str, title: str) -> dict[str, Any]:
        return {
            "plan_version": 1,
            "percent": percent,
            "estimate_revision": 1,
            "completed_steps": int(percent // 20),
            "total_steps": 5,
            "current_step": {"code": code, "title": title, "message": title},
            "eta_seconds": None,
        }

    @staticmethod
    def _run_links(run_id: str) -> dict[str, str]:
        return {
            "self": f"/v1/runs/{run_id}",
            "events": f"/v1/runs/{run_id}/events",
            "outputs": f"/v1/runs/{run_id}/outputs",
            "result": f"/v1/runs/{run_id}/result",
        }

    async def get_run(self, principal: Principal, run_id: str) -> dict[str, Any]:
        async with self._visibility_lock:
            return _public_run(self._owned(await self.store.get_run(run_id), principal))

    async def list_runs(self, principal: Principal) -> list[dict[str, Any]]:
        async with self._visibility_lock:
            runs = await self.store.list_runs()
            return [_public_run(run) for run in runs if run.get("tenant_id") == principal.tenant_id]

    async def list_decision_packets(
        self, principal: Principal, run_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        self._owned(await self.store.get_run(run_id), principal)
        packets = await self.store.list_decision_packets(run_id=run_id)
        return [
            {key: value for key, value in packet.items() if key != "tenant_id"}
            for packet in packets
            if packet.get("tenant_id") == principal.tenant_id
            and (status is None or packet["status"] == status)
        ]

    async def _enforce_dataset_limits(self, principal: Principal, request: dict[str, Any]) -> None:
        size_bytes = int(request["size_bytes"])
        if size_bytes > self.limits.max_dataset_bytes:
            raise APIProblem(
                413,
                "dataset_too_large",
                "Dataset is too large",
                "The declared dataset size exceeds this service profile.",
                extras={"runtime_limits": self.limits.manifest()},
            )
        storage_used = await self._tenant_storage_bytes(principal.tenant_id)
        if storage_used + size_bytes > self.limits.max_storage_bytes_per_tenant:
            raise APIProblem(
                429,
                "tenant_storage_limit_exceeded",
                "Tenant storage limit exceeded",
                "Delete or expire existing resources before uploading another dataset.",
                retriable=True,
                extras={
                    "storage_bytes_used": storage_used,
                    "requested_bytes": size_bytes,
                    "runtime_limits": self.limits.manifest(),
                },
                headers={"Retry-After": "3600"},
            )

    async def _enforce_run_limits(self, principal: Principal, request: dict[str, Any]) -> None:
        self.limits.validate_budget(request["budget"])
        active = await self._tenant_active_run_count(principal.tenant_id)
        if active >= self.limits.max_active_runs_per_tenant:
            raise APIProblem(
                429,
                "active_run_limit_exceeded",
                "Active Run limit exceeded",
                "Wait for an active Run to finish before creating another one.",
                retriable=True,
                extras={
                    "active_runs": active,
                    "runtime_limits": self.limits.manifest(),
                },
                headers={"Retry-After": "30"},
            )

    async def _tenant_storage_bytes(self, tenant_id: str) -> int:
        versions = await self.store.list_dataset_versions()
        artifacts = await self.store.list_artifacts()
        dataset_bytes = sum(
            int(version.get("size_bytes") or 0)
            for version in versions
            if version.get("tenant_id") == tenant_id
        )
        artifact_bytes = sum(
            int(artifact.get("size_bytes") or 0)
            for artifact in artifacts
            if artifact.get("tenant_id") == tenant_id
        )
        return dataset_bytes + artifact_bytes

    async def _tenant_active_run_count(self, tenant_id: str) -> int:
        runs = await self.store.list_runs()
        return sum(
            1
            for run in runs
            if run.get("tenant_id") == tenant_id and run.get("status") != "TERMINAL"
        )

    @staticmethod
    def _require_external_agent_policy(run: dict[str, Any]) -> None:
        if run.get("policy", {}).get("allow_external_llm") is not True:
            raise APIProblem(
                403,
                "external_agent_access_denied",
                "External agent access is disabled",
                "Create the Run with policy.allow_external_llm=true before exposing its "
                "structured context to an external agent platform.",
            )

    @staticmethod
    def _enforce_decision_actor(packet: dict[str, Any], principal: Principal) -> None:
        policy = packet.get("resolution_policy", "HUMAN_REQUIRED")
        actor_type = principal.actor_type
        if actor_type == "development":
            return
        if policy == "HUMAN_REQUIRED" and actor_type != "human":
            raise APIProblem(
                403,
                "human_decision_required",
                "Human decision required",
                "This wait-set must be answered with a delegated human token.",
            )
        if policy == "APPROVAL_REQUIRED":
            raise APIProblem(
                403,
                "approval_required",
                "Approval required",
                "This wait-set requires an approval workflow instead of a direct answer.",
            )

    @staticmethod
    def _enforce_agent_recommendations(
        packet: dict[str, Any],
        principal: Principal,
        answers: dict[str, Any],
    ) -> None:
        if principal.actor_type not in {"agent", "service"}:
            return
        if packet.get("resolution_policy", "HUMAN_REQUIRED") != "AGENT_ALLOWED":
            return
        for question in packet.get("questions", []):
            question_id = question["question_id"]
            recommendation = question.get("recommendation")
            supplied = answers.get(question_id)
            if question.get("selection_mode") == "MULTIPLE":
                if not (
                    isinstance(supplied, list)
                    and isinstance(recommendation, list)
                    and WorkflowService._same_value_multiset(supplied, recommendation)
                ):
                    raise APIProblem(
                        422,
                        "agent_answer_must_use_recommendation",
                        "Agent answer must use the recommendation",
                        "Agent-scoped answers may only submit the packet recommendation.",
                    )
            elif not (type(supplied) is type(recommendation) and supplied == recommendation):
                raise APIProblem(
                    422,
                    "agent_answer_must_use_recommendation",
                    "Agent answer must use the recommendation",
                    "Agent-scoped answers may only submit the packet recommendation.",
                )

    @staticmethod
    def _same_value_multiset(left: list[Any], right: list[Any]) -> bool:
        if len(left) != len(right):
            return False
        remaining = list(right)
        for value in left:
            for index, candidate in enumerate(remaining):
                if type(value) is type(candidate) and value == candidate:
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining

    @staticmethod
    def _validate_packet_answers(packet: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """Validate cardinality and option membership before domain-specific handling."""

        answer_by_id = {item["question_id"]: item["value"] for item in request["answers"]}
        questions = packet.get("questions", [])
        required = {question["question_id"] for question in questions}
        if (
            len(request["answers"]) != len(answer_by_id)
            or len(answer_by_id) != len(required)
            or set(answer_by_id) != required
        ):
            raise APIProblem(
                422,
                "incomplete_wait_set",
                "Answer the complete wait-set",
                "Submit exactly one answer for every question in the current wait-set.",
            )

        for question in questions:
            question_id = question["question_id"]
            value = answer_by_id[question_id]
            mode = question.get("selection_mode") or (
                "SINGLE" if question.get("options") else "FREEFORM"
            )
            min_selections = int(question.get("min_selections", 1))
            max_selections = int(question.get("max_selections", 1))
            if mode == "MULTIPLE":
                if not isinstance(value, list):
                    raise APIProblem(
                        422,
                        "invalid_answer",
                        "Invalid selection format",
                        f"{question_id} requires a list of selections.",
                    )
                values = value
            else:
                if isinstance(value, list):
                    raise APIProblem(
                        422,
                        "invalid_answer",
                        "Invalid selection format",
                        f"{question_id} accepts exactly one scalar value.",
                    )
                values = [value]

            if not min_selections <= len(values) <= max_selections:
                raise APIProblem(
                    422,
                    "invalid_answer",
                    "Invalid selection count",
                    f"{question_id} requires between {min_selections} and "
                    f"{max_selections} selection(s).",
                )

            options = question.get("options") or []
            if options:
                for selected in values:
                    if not any(
                        type(selected) is type(option.get("value"))
                        and selected == option.get("value")
                        for option in options
                    ):
                        raise APIProblem(
                            422,
                            "invalid_answer",
                            "Selection is not available",
                            f"{question_id} must use one of the declared option values.",
                        )
        return answer_by_id

    @staticmethod
    def _agent_action_items(
        run: dict[str, Any], open_packets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        run_id = str(run["run_id"])
        available = set(run.get("available_actions", []))
        items: list[dict[str, Any]] = []
        if "ANSWER" in available:
            for packet in open_packets:
                # Human and approval gates stay visible in context, but are never
                # presented as executable Agent actions.
                if packet.get("resolution_policy", "HUMAN_REQUIRED") != "AGENT_ALLOWED":
                    continue
                items.append(
                    {
                        "action": "ANSWER",
                        "operation_id": "answerDecisionPacket",
                        "method": "POST",
                        "href": (
                            f"/v1/runs/{run_id}/decision-packets/{packet['wait_set_id']}:answer"
                        ),
                        "idempotency_key_required": True,
                        "if_match": {
                            "header": "If-Match",
                            "scope": "WAIT_SET_REVISION",
                            "value": f'"{packet["wait_set_revision"]}"',
                        },
                        "request_schema_ref": ("#/components/schemas/AnswerDecisionPacketRequest"),
                    }
                )
        if "PAUSE" in available:
            items.append(
                {
                    "action": "PAUSE",
                    "operation_id": "pauseRun",
                    "method": "POST",
                    "href": f"/v1/runs/{run_id}:pause",
                    "idempotency_key_required": True,
                    "if_match": {
                        "header": "If-Match",
                        "scope": "RUN_REVISION",
                        "value": f'"{run["run_revision"]}"',
                    },
                    "request_schema_ref": None,
                }
            )
        if "RESUME" in available:
            items.append(
                {
                    "action": "RESUME",
                    "operation_id": "resumeRun",
                    "method": "POST",
                    "href": f"/v1/runs/{run_id}:resume",
                    "idempotency_key_required": True,
                    "if_match": {
                        "header": "If-Match",
                        "scope": "RUN_REVISION",
                        "value": f'"{run["run_revision"]}"',
                    },
                    "request_schema_ref": None,
                }
            )
        if "CANCEL" in available:
            items.append(
                {
                    "action": "CANCEL",
                    "operation_id": "cancelRun",
                    "method": "POST",
                    "href": f"/v1/runs/{run_id}:cancel",
                    "idempotency_key_required": True,
                    "if_match": None,
                    "request_schema_ref": None,
                }
            )
        return items

    async def get_agent_context(
        self, principal: Principal, run_id: str, *, output_limit: int
    ) -> dict[str, Any]:
        async with self._visibility_lock:
            run = self._owned(await self.store.get_run(run_id), principal)
            self._require_external_agent_policy(run)
            packets = await self.store.list_decision_packets(run_id=run_id)
            open_packets = [
                {key: value for key, value in packet.items() if key != "tenant_id"}
                for packet in packets
                if packet.get("tenant_id") == principal.tenant_id and packet.get("status") == "OPEN"
            ]
            outputs = await self.store.list_outputs(run_id=run_id)
            visible_outputs = [
                output for output in outputs if output.get("tenant_id") == principal.tenant_id
            ]
            visible_outputs.sort(
                key=lambda output: (int(output.get("created_seq", 0)), str(output["output_id"]))
            )
            selected_outputs = visible_outputs[-output_limit:]
            result = await self.store.get_result(run_id)
            resolved_objective = run.get("resolved_inputs") or run.get("objective") or {}
            return {
                "schema_version": "1.0",
                "run": _public_run(run),
                "objective": dict(resolved_objective),
                "open_decision_packets": open_packets,
                "recent_output_refs": [self._output_ref(output) for output in selected_outputs],
                "output_refs_truncated": len(visible_outputs) > len(selected_outputs),
                "event_checkpoint": {
                    "after_seq": int(run["snapshot_seq"]),
                    "events_href": f"/v1/runs/{run_id}/events",
                },
                "result_available": run.get("status") == "TERMINAL" and result is not None,
                "contains_raw_dataset_rows": False,
                "may_include_dataset_derived_values": True,
                "dataset_derived_text_trust": "UNTRUSTED",
                "actions_href": f"/v1/runs/{run_id}/agent-actions",
                "links": {
                    "run": f"/v1/runs/{run_id}",
                    "events": f"/v1/runs/{run_id}/events",
                    "outputs": f"/v1/runs/{run_id}/outputs",
                    "result": f"/v1/runs/{run_id}/result",
                },
            }

    async def get_agent_actions(self, principal: Principal, run_id: str) -> dict[str, Any]:
        async with self._visibility_lock:
            run = self._owned(await self.store.get_run(run_id), principal)
            self._require_external_agent_policy(run)
            packets = await self.store.list_decision_packets(run_id=run_id)
            open_packets = [
                packet
                for packet in packets
                if packet.get("tenant_id") == principal.tenant_id and packet.get("status") == "OPEN"
            ]
            return {
                "schema_version": "1.0",
                "run_id": run_id,
                "run_revision": run["run_revision"],
                "items": self._agent_action_items(run, open_packets),
            }

    async def answer(
        self,
        principal: Principal,
        run_id: str,
        wait_set_id: str,
        wait_set_revision: int,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._visibility_lock:
            run = self._owned(await self.store.get_run(run_id), principal)
            packet = self._owned(
                await self.store.get_decision_packet_by_wait_set(run_id, wait_set_id), principal
            )
            if packet["run_id"] != run_id:
                raise APIProblem(404, "not_found", "Resource not found", "Wait-set not found.")
            if packet["wait_set_revision"] != wait_set_revision:
                raise APIProblem(
                    412,
                    "stale_revision",
                    "Decision packet changed",
                    "Refresh the DecisionPacket and retry against its wait_set_revision.",
                    extras={
                        "current_wait_set_revision": packet["wait_set_revision"],
                        "recovery": {
                            "action": "LIST_DECISION_PACKETS",
                            "href": f"/v1/runs/{run_id}/decision-packets?status=OPEN",
                        },
                    },
                )
            if packet["status"] != "OPEN" or run["status"] != "WAITING_USER":
                raise APIProblem(
                    409,
                    "decision_packet_not_open",
                    "Decision packet is not open",
                    "Only the current OPEN wait-set can be answered.",
                )
            self._enforce_decision_actor(packet, principal)
            answer_by_id = self._validate_packet_answers(packet, request)
            self._enforce_agent_recommendations(packet, principal, answer_by_id)
            target_column = answer_by_id["q_target"]
            if not isinstance(target_column, str) or not target_column.strip():
                raise APIProblem(
                    422,
                    "invalid_answer",
                    "Invalid target column",
                    "q_target must be a non-empty string.",
                )

            command = await self.store.create_command(
                {
                    "tenant_id": principal.tenant_id,
                    "run_id": run_id,
                    "type": "ANSWER",
                    "status": "RUNNING",
                    "submitted_at": iso_now(),
                    "completed_at": None,
                    "resulting_run_revision": None,
                    "problem": None,
                }
            )
            await self.store.update_decision_packet(
                run_id,
                packet["decision_packet_id"],
                {"status": "ANSWERED", "wait_set_revision": wait_set_revision + 1},
            )
            run = await self.store.update_run(
                run_id,
                {
                    "status": "RUNNING",
                    "phase": "PLAN",
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                    "available_actions": ["PAUSE", "CANCEL"],
                    "progress": self._progress(50, "PLAN", "Task specification confirmed"),
                    "updated_at": iso_now(),
                },
                expected_revision=run["run_revision"],
                bump_revision=True,
            )
            run = await self._emit(
                run,
                "run.phase_changed.v1",
                {"previous_phase": "PLAN", "phase": "PLAN", "status": "RUNNING"},
            )

            objective = run.get("objective", {})
            task_type = objective.get("task_type") or "BINARY_CLASSIFICATION"
            primary_metric = objective.get("primary_metric") or (
                "average_precision" if task_type == "BINARY_CLASSIFICATION" else "rmse"
            )
            task_output, run = await self._commit_output(
                run,
                output_type="TASK_SPEC",
                phase="PLAN",
                summary={
                    "code": "TASK_SPEC_CONFIRMED",
                    "message": "The user confirmed the target column.",
                    "severity": "INFO",
                },
                payload={
                    "kind": "TASK_SPEC",
                    "task_type": task_type,
                    "target_column_id": target_column.strip(),
                    "positive_class": None,
                    "primary_metric": primary_metric,
                    "guardrail_metrics": [],
                    "split_strategy": "STRATIFIED_HOLDOUT"
                    if task_type == "BINARY_CLASSIFICATION"
                    else "RANDOM_HOLDOUT",
                    "confidence": 1.0,
                    "assumptions": ["Milestone 1 uses synthetic execution only."],
                    "confirmed_by": "USER",
                },
            )

            report_bytes = f"synthetic report for {run_id}\n".encode("utf-8")
            artifact_id = self.store.new_id("artifact")
            blob = await self.blob_store.put_artifact(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
                content=report_bytes,
            )
            artifact = await self.store.create_artifact(
                {
                    "artifact_id": artifact_id,
                    "tenant_id": principal.tenant_id,
                    "run_id": run_id,
                    "output_id": None,
                    "kind": "RUN_REPORT_JSON",
                    "media_type": "application/json",
                    "size_bytes": blob.size_bytes,
                    "sha256": blob.sha256,
                    "href": f"/v1/artifacts/{artifact_id}",
                    "state": "COMMITTED",
                    "etag": blob.etag,
                    "supports_range": True,
                    "blob_key": blob.key,
                    "created_at": iso_now(),
                    "lineage": {
                        "dataset_version_id": run["dataset_version_id"],
                        "task_spec_output_id": task_output["output_id"],
                        "split_manifest_output_id": None,
                        "policy_version": "policy.m1",
                        "method_version": "synthetic.v1",
                        "parent_refs": [],
                        "evidence_refs": [task_output["output_id"]],
                    },
                }
            )
            artifact_ref = {
                key: artifact[key]
                for key in ["artifact_id", "kind", "media_type", "size_bytes", "sha256", "href"]
            }
            report_output, run = await self._commit_output(
                run,
                output_type="RUN_REPORT",
                phase="PACKAGE",
                summary={
                    "code": "SYNTHETIC_RUN_COMPLETE",
                    "message": "The Milestone 1 workflow completed without training a model.",
                    "severity": "INFO",
                },
                payload={
                    "kind": "RUN_REPORT",
                    "summary": "Synthetic workflow completed.",
                    "recommendation": "Proceed to the deterministic ML baseline milestone.",
                    "evidence_refs": [task_output["output_id"]],
                },
                artifact_refs=[artifact_ref],
            )
            await self.store.update_artifact(artifact_id, {"output_id": report_output["output_id"]})
            all_outputs = await self.store.list_outputs(run_id=run_id)
            result = {
                "result_manifest_id": self.store.new_id("result"),
                "run_id": run_id,
                "outcome": "SUCCEEDED",
                "model_disposition": "NO_ELIGIBLE_MODEL",
                "summary": "Milestone 1 completed; model training is intentionally disabled.",
                "output_refs": [self._output_ref(output) for output in all_outputs],
                "partial": False,
                "eligible_model": None,
                "reason": {
                    "code": "MILESTONE_1_NO_TRAINING",
                    "message": "This skeleton validates orchestration and API contracts only.",
                    "retriable": False,
                    "failed_gates": [],
                    "evidence_refs": [report_output["output_id"]],
                    "remediation": ["Implement the deterministic baseline milestone."],
                },
                "completed_at": iso_now(),
            }
            await self.store.set_result(run_id, result)
            run = await self.store.update_run(
                run_id,
                {
                    "phase": "PACKAGE",
                    "status": "TERMINAL",
                    "outcome": "SUCCEEDED",
                    "progress": self._progress(100, "COMPLETED", "Run completed"),
                    "stages": _stages(
                        active_phase="PACKAGE",
                        active_status="RUNNING",
                        completed={"INGEST", "PROFILE", "PLAN", "TRAIN", "EVALUATE", "PACKAGE"},
                    ),
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                    "available_actions": [],
                    "latest_output_refs": [self._output_ref(report_output)],
                    "updated_at": iso_now(),
                },
                expected_revision=run["run_revision"],
                bump_revision=True,
            )
            run = await self._emit(
                run,
                "run.completed.v1",
                {"outcome": "SUCCEEDED", "result_href": f"/v1/runs/{run_id}/result"},
            )
            command = await self.store.update_command(
                command["command_id"],
                {
                    "status": "SUCCEEDED",
                    "completed_at": iso_now(),
                    "resulting_run_revision": run["run_revision"],
                    "links": {
                        "self": f"/v1/commands/{command['command_id']}",
                        "run": f"/v1/runs/{run_id}",
                    },
                },
            )
            return self.public_command(command)

    async def pause(self, principal: Principal, run_id: str, revision: int) -> dict[str, Any]:
        return await self._control(principal, run_id, "PAUSE", revision=revision)

    async def resume(self, principal: Principal, run_id: str, revision: int) -> dict[str, Any]:
        return await self._control(principal, run_id, "RESUME", revision=revision)

    async def cancel(self, principal: Principal, run_id: str) -> dict[str, Any]:
        return await self._control(principal, run_id, "CANCEL", revision=None)

    async def _control(
        self, principal: Principal, run_id: str, command_type: str, revision: int | None
    ) -> dict[str, Any]:
        async with self._visibility_lock:
            run = self._owned(await self.store.get_run(run_id), principal)
            if revision is not None and run["run_revision"] != revision:
                raise APIProblem(
                    412,
                    "stale_revision",
                    "Run changed",
                    "Refresh the RunSnapshot and retry against its run_revision.",
                    extras={"current_run_revision": run["run_revision"]},
                )
            if run["status"] == "TERMINAL":
                raise APIProblem(
                    409,
                    "run_terminal",
                    "Run is terminal",
                    "A terminal Run cannot accept this control command.",
                )
            if command_type not in run["available_actions"]:
                raise APIProblem(
                    409,
                    "command_not_available",
                    "Command is not available",
                    f"{command_type} is not valid in the current Run state.",
                )
            if command_type == "RESUME" and run["status"] != "PAUSED":
                raise APIProblem(
                    409, "run_not_paused", "Run is not paused", "Only PAUSED can resume."
                )
            if command_type == "PAUSE" and run["status"] == "PAUSED":
                raise APIProblem(
                    409, "run_already_paused", "Run is paused", "The Run is already paused."
                )

            command = await self.store.create_command(
                {
                    "tenant_id": principal.tenant_id,
                    "run_id": run_id,
                    "type": command_type,
                    "status": "RUNNING",
                    "submitted_at": iso_now(),
                    "completed_at": None,
                    "resulting_run_revision": None,
                    "problem": None,
                }
            )
            if command_type == "PAUSE":
                new_status, outcome, actions = "PAUSED", None, ["RESUME", "CANCEL"]
            elif command_type == "RESUME":
                packets = await self.store.list_decision_packets(run_id=run_id)
                new_status = (
                    "WAITING_USER" if any(p["status"] == "OPEN" for p in packets) else "RUNNING"
                )
                outcome, actions = (
                    None,
                    ["ANSWER", "PAUSE", "CANCEL"]
                    if new_status == "WAITING_USER"
                    else ["PAUSE", "CANCEL"],
                )
            else:
                new_status, outcome, actions = "TERMINAL", "CANCELED", []
                packets = await self.store.list_decision_packets(run_id=run_id)
                for packet in packets:
                    if packet["status"] == "OPEN":
                        await self.store.update_decision_packet(
                            run_id,
                            packet["decision_packet_id"],
                            {
                                "status": "SUPERSEDED",
                                "wait_set_revision": int(packet["wait_set_revision"]) + 1,
                            },
                        )
                outputs = await self.store.list_outputs(run_id=run_id)
                await self.store.set_result(
                    run_id,
                    {
                        "result_manifest_id": self.store.new_id("result"),
                        "run_id": run_id,
                        "outcome": "CANCELED",
                        "model_disposition": "INCOMPLETE",
                        "summary": "The Run was canceled by the user.",
                        "output_refs": [self._output_ref(output) for output in outputs],
                        "partial": True,
                        "eligible_model": None,
                        "reason": {
                            "code": "USER_CANCELED",
                            "message": "Cancellation was requested through the public API.",
                            "retriable": False,
                            "failed_gates": [],
                            "evidence_refs": [],
                            "remediation": ["Create a new Run to start over."],
                        },
                        "completed_at": iso_now(),
                    },
                )
            run = await self.store.update_run(
                run_id,
                {
                    "status": new_status,
                    "outcome": outcome,
                    "available_actions": actions,
                    "blocking": (
                        {"decision_packet_ids": [], "approval_ids": []}
                        if command_type == "CANCEL"
                        else run["blocking"]
                    ),
                    "stages": (
                        self._canceled_stages(run["stages"])
                        if command_type == "CANCEL"
                        else run["stages"]
                    ),
                    "updated_at": iso_now(),
                },
                expected_revision=run["run_revision"],
                bump_revision=True,
            )
            event_type = "run.canceled.v1" if command_type == "CANCEL" else "run.phase_changed.v1"
            payload = (
                {"outcome": "CANCELED", "result_href": f"/v1/runs/{run_id}/result"}
                if command_type == "CANCEL"
                else {"previous_phase": run["phase"], "phase": run["phase"], "status": new_status}
            )
            run = await self._emit(run, event_type, payload)
            command = await self.store.update_command(
                command["command_id"],
                {
                    "status": "SUCCEEDED",
                    "completed_at": iso_now(),
                    "resulting_run_revision": run["run_revision"],
                    "links": {
                        "self": f"/v1/commands/{command['command_id']}",
                        "run": f"/v1/runs/{run_id}",
                    },
                },
            )
            return self.public_command(command)

    @staticmethod
    def _canceled_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        canceled_at = iso_now()
        result: list[dict[str, Any]] = []
        for stage in stages:
            item = dict(stage)
            if item["status"] not in {"COMPLETED", "SKIPPED"}:
                item.update(
                    {
                        "status": "CANCELED",
                        "completed_at": canceled_at,
                        "message": "Canceled by user request",
                    }
                )
            result.append(item)
        return result

    @staticmethod
    def public_command(command: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in command.items() if key != "tenant_id"}

    async def get_command(self, principal: Principal, command_id: str) -> dict[str, Any]:
        return self.public_command(self._owned(await self.store.get_command(command_id), principal))

    async def get_result(self, principal: Principal, run_id: str) -> dict[str, Any]:
        async with self._visibility_lock:
            run = self._owned(await self.store.get_run(run_id), principal)
            result = await self.store.get_result(run_id)
            if run["status"] != "TERMINAL" or result is None:
                raise APIProblem(
                    409,
                    "run_not_terminal",
                    "Run is not terminal",
                    "The result manifest becomes available atomically with terminal state.",
                )
            return result

    async def list_outputs(self, principal: Principal, run_id: str) -> list[dict[str, Any]]:
        self._owned(await self.store.get_run(run_id), principal)
        return [
            {key: value for key, value in output.items() if key != "tenant_id"}
            for output in await self.store.list_outputs(run_id=run_id)
        ]

    async def get_output(self, principal: Principal, run_id: str, output_id: str) -> dict[str, Any]:
        self._owned(await self.store.get_run(run_id), principal)
        output = self._owned(await self.store.get_output(run_id, output_id), principal)
        return {key: value for key, value in output.items() if key != "tenant_id"}

    async def get_artifact(self, principal: Principal, artifact_id: str) -> dict[str, Any]:
        artifact = self._owned(await self.store.get_artifact(artifact_id), principal)
        if artifact.get("state") != "COMMITTED":
            raise APIProblem(410, "artifact_gone", "Artifact is unavailable", "Artifact is gone.")
        return {key: value for key, value in artifact.items() if key != "tenant_id"}

    async def create_download_ticket(
        self,
        principal: Principal,
        artifact_id: str,
        *,
        public_base_url: str = "http://localhost",
    ) -> dict[str, Any]:
        artifact = self._owned(await self.store.get_artifact(artifact_id), principal)
        if artifact["state"] != "COMMITTED":
            raise APIProblem(410, "artifact_gone", "Artifact is unavailable", "Artifact is gone.")
        ticket_id = self.store.new_id("ticket")
        expires_at_epoch = int(utcnow().timestamp()) + 900
        token = self.blob_store.create_download_token(
            artifact_id=artifact_id,
            tenant_id=principal.tenant_id,
            etag=artifact["etag"],
            expires_at=expires_at_epoch,
        )
        return {
            "ticket_id": ticket_id,
            "artifact_id": artifact_id,
            "url": (
                f"{public_base_url.rstrip('/')}/v1/artifact-downloads/{token}"
                if self.blob_store.durable
                else f"https://downloads.invalid/tickets/{ticket_id}"
            ),
            "expires_in_seconds": 900,
            "expires_at": (utcnow() + timedelta(seconds=900)).isoformat().replace("+00:00", "Z"),
            "required_headers": {"If-Match": artifact["etag"]},
            "etag": artifact["etag"],
            "sha256": artifact["sha256"],
            "size_bytes": artifact["size_bytes"],
            "supports_range": artifact["supports_range"],
        }


workflow = WorkflowService()
