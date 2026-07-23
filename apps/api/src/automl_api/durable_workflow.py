from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from .auth import Principal
from .backends import BackendRegistry
from .errors import APIProblem
from .limits import RuntimeLimits
from .ml_engine import (
    MLEngineError,
    PositiveClassRequiredError,
    TabularAutoMLResult,
    inspect_tabular_dataset,
)
from .persistence import SqliteStore
from .protocol import iso_now, utcnow
from .storage import BlobStore
from .worker import CHECKPOINT, COMPLETE, ExecutionJob, WorkerResult
from .workflow import WorkflowService, _public_run, _stages


def _metric(name: str, value: float) -> dict[str, Any]:
    direction = "MINIMIZE" if name in {"rmse", "mae", "log_loss"} else "MAXIMIZE"
    return {"name": name, "value": float(value), "direction": direction}


class DurableWorkflowService(WorkflowService):
    """Local durable workflow that executes a bounded real tabular ML pipeline."""

    def __init__(
        self,
        state: SqliteStore,
        *,
        blob_store: BlobStore,
        limits: RuntimeLimits | None = None,
        backend_registry: BackendRegistry | None = None,
    ) -> None:
        super().__init__(
            state,
            blob_store=blob_store,
            limits=limits,
            backend_registry=backend_registry,
        )
        self.store: SqliteStore = state

    async def ensure_execution_jobs(self) -> None:
        """Repair a crash between Run creation and its one durable job insert."""

        for run in await self.store.list_runs():
            if run.get("status") == "TERMINAL":
                continue
            if await self.store.get_execution_job(run["run_id"]) is not None:
                continue
            await self.store.create_execution_job(
                run["run_id"],
                workflow_step=str(run.get("execution_step", "PROFILE")),
                checkpoint={},
            )

    async def create_run(self, principal: Principal, request: dict[str, Any]) -> dict[str, Any]:
        async with self._visibility_lock:
            await self._enforce_run_limits(principal, request)
            version = self._owned(
                await self.store.get_dataset_version(request["dataset_version_id"]), principal
            )
            if version["status"] != "READY" or not version.get("blob_key"):
                raise APIProblem(
                    409,
                    "dataset_not_ready",
                    "Dataset is not ready",
                    "Upload and finalize the dataset bytes before creating a Run.",
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
                        "policy_version": "policy.m2-local",
                        "tool_versions": {
                            backend["backend_id"]: backend["backend_version"]
                            or backend["engine_version"]
                        },
                    },
                    "method_version": backend["engine_version"],
                    "progress": self._progress(0, "QUEUED", "Run queued for profiling"),
                    "stages": _stages(active_phase="INGEST", active_status="RUNNING"),
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                    "latest_output_refs": [],
                    "available_actions": ["PAUSE", "CANCEL"],
                    "budget_usage": {
                        "compute_credits": {"used": 0, "limit": budget["max_compute_credits"]},
                        "trials": {"used": 0, "limit": budget["max_trials"]},
                        "wall_time_seconds": {"used": 0, "limit": budget["max_wall_time_seconds"]},
                        "llm_tokens": {"used": 0, "limit": budget["max_llm_tokens"]},
                    },
                    "created_at": now,
                    "updated_at": now,
                    "links": self._run_links(run_id),
                    "objective": request["objective"],
                    "resolved_inputs": dict(request["objective"]),
                    "backend_id": backend["backend_id"],
                    "policy": request["policy"],
                    "budget": budget,
                    "execution_step": "PROFILE",
                }
            )
            await self.store.create_execution_job(
                run_id,
                workflow_step="PROFILE",
                checkpoint={"dataset_version_id": version["dataset_version_id"]},
            )
            return _public_run(run)

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

            resolved = dict(run.get("resolved_inputs", {}))
            profile = run.get("pre_split_profile", {})
            column_names = {
                item["name"] for item in profile.get("columns", []) if isinstance(item, dict)
            }
            if "q_target" in answer_by_id:
                target = answer_by_id["q_target"]
                if not isinstance(target, str) or target not in column_names:
                    raise APIProblem(
                        422,
                        "invalid_answer",
                        "Invalid target column",
                        "Choose one of the columns in the current DecisionPacket.",
                    )
                resolved["target_column"] = target
            if "q_iid" in answer_by_id:
                if answer_by_id["q_iid"] is not True:
                    raise APIProblem(
                        422,
                        "unsupported_split_requirement",
                        "A grouped or temporal split is required",
                        "This local slice only runs after i.i.d. rows are explicitly confirmed.",
                    )
                resolved["iid_confirmed"] = True
            if "q_positive_class" in answer_by_id:
                resolved["positive_class"] = answer_by_id["q_positive_class"]

            command = await self.store.create_command(
                {
                    "tenant_id": principal.tenant_id,
                    "run_id": run_id,
                    "type": "ANSWER",
                    "status": "ACCEPTED",
                    "submitted_at": iso_now(),
                    "completed_at": None,
                    "resulting_run_revision": None,
                    "problem": None,
                    "links": {
                        "self": "",
                        "run": f"/v1/runs/{run_id}",
                    },
                }
            )
            await self.store.update_command(
                command["command_id"],
                {
                    "links": {
                        "self": f"/v1/commands/{command['command_id']}",
                        "run": f"/v1/runs/{run_id}",
                    }
                },
            )
            command = await self.store.get_command(command["command_id"])
            assert command is not None
            await self.store.update_decision_packet(
                run_id,
                packet["decision_packet_id"],
                {"status": "ANSWERED", "wait_set_revision": wait_set_revision + 1},
            )
            await self.store.update_run(
                run_id,
                {
                    "status": "QUEUED",
                    "blocking": {"decision_packet_ids": [], "approval_ids": []},
                    "available_actions": ["PAUSE", "CANCEL"],
                    "resolved_inputs": resolved,
                    "pending_command_id": command["command_id"],
                    "updated_at": iso_now(),
                },
                expected_revision=run["run_revision"],
                bump_revision=True,
            )
            await self.store.wake_execution_job(run_id)
            return self.public_command(command)

    async def resume(self, principal: Principal, run_id: str, revision: int) -> dict[str, Any]:
        command = await super().resume(principal, run_id, revision)
        job = await self.store.get_execution_job(run_id)
        if job is not None and job["status"] not in {"COMPLETED", "DEAD"}:
            await self.store.wake_execution_job(run_id)
        return command

    async def cancel(self, principal: Principal, run_id: str) -> dict[str, Any]:
        command = await super().cancel(principal, run_id)
        job = await self.store.get_execution_job(run_id)
        if job is not None and job["status"] not in {"COMPLETED", "DEAD"}:
            await self.store.wake_execution_job(run_id)
        return command

    async def handle_execution_job(self, job: ExecutionJob) -> WorkerResult:
        run_id = str(job["run_id"])
        run = await self.store.get_run(run_id)
        if run is None or run.get("status") == "TERMINAL":
            return COMPLETE
        if run.get("status") in {"PAUSED", "WAITING_USER"}:
            return CHECKPOINT(
                str(job["workflow_step"]), status="WAITING", checkpoint=job["checkpoint"]
            )
        step = str(job["workflow_step"])
        if step == "PROFILE":
            return await self._execute_profile(run)
        if step == "RESOLVE_TASK":
            return await self._execute_resolved_task(run)
        if step == "TRAIN":
            return await self._execute_training(run)
        raise RuntimeError(f"unknown durable workflow step: {step}")

    async def handle_dead_job(self, job: ExecutionJob) -> None:
        run = await self.store.get_run(str(job["run_id"]))
        if run is not None and run.get("status") != "TERMINAL":
            await self._fail_run(
                run,
                code="WORKER_RETRIES_EXHAUSTED",
                message=str(job.get("last_error") or "The local worker exhausted its retries."),
                retriable=True,
            )

    def _dataset_path(self, run: dict[str, Any]) -> tuple[Any, str]:
        version_id = run["dataset_version_id"]
        version = self.store._dataset_versions.get(version_id)
        if version is None or not version.get("blob_key"):
            raise RuntimeError("the Run dataset bytes are unavailable")
        return self.blob_store.path_for_key(version["blob_key"]), str(version["media_type"])

    async def _execute_profile(self, run: dict[str, Any]) -> WorkerResult:
        path, media_type = self._dataset_path(run)
        profile = await asyncio.to_thread(inspect_tabular_dataset, path, media_type)
        async with self._visibility_lock:
            current = await self.store.get_run(run["run_id"])
            if current is None or current["status"] == "TERMINAL":
                return COMPLETE
            current = await self.store.update_run(
                run["run_id"],
                {
                    "phase": "PROFILE",
                    "status": "RUNNING",
                    "execution_step": "PROFILE",
                    "progress": self._progress(15, "PROFILE", "Dataset profile committed"),
                    "stages": _stages(
                        active_phase="PROFILE", active_status="RUNNING", completed={"INGEST"}
                    ),
                    "updated_at": iso_now(),
                },
                bump_revision=False,
            )
            current = await self._emit(
                current,
                "run.phase_changed.v1",
                {"previous_phase": "INGEST", "phase": "PROFILE", "status": "RUNNING"},
            )
            quality_output, current = await self._commit_output(
                current,
                output_type="DATA_QUALITY_REPORT",
                phase="PROFILE",
                summary={
                    "code": "PRE_SPLIT_PROFILE_READY",
                    "message": "Dataset structure was parsed without exposing row values.",
                    "severity": "INFO",
                },
                payload={
                    "kind": "DATA_QUALITY_REPORT",
                    "row_count": profile["row_count"],
                    "column_count": profile["column_count"],
                    "quality_score": 100,
                    "issues": [],
                },
            )
            current = await self.store.update_run(
                current["run_id"],
                {"pre_split_profile": profile, "execution_step": "RESOLVE_TASK"},
                bump_revision=False,
            )
            questions = self._initial_questions(current, profile)
            if questions:
                await self._request_decision(current, questions, [quality_output["output_id"]])
                return CHECKPOINT(
                    "RESOLVE_TASK",
                    status="WAITING",
                    checkpoint={"profile_output_id": quality_output["output_id"]},
                )
            await self.store.update_run(
                current["run_id"],
                {
                    "phase": "TRAIN",
                    "status": "QUEUED",
                    "execution_step": "TRAIN",
                    "progress": self._progress(40, "QUEUED", "Training step queued"),
                    "available_actions": ["PAUSE", "CANCEL"],
                    "updated_at": iso_now(),
                },
                bump_revision=True,
            )
            return CHECKPOINT(
                "TRAIN", checkpoint={"profile_output_id": quality_output["output_id"]}
            )

    @staticmethod
    def _initial_questions(run: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
        objective = run.get("resolved_inputs", {})
        columns = [item["name"] for item in profile["columns"]]
        questions: list[dict[str, Any]] = []
        if objective.get("target_column") not in columns:
            questions.append(
                {
                    "question_id": "q_target",
                    "prompt": "Which column is the prediction target?",
                    "answer_schema": {"type": "string", "enum": columns},
                    "selection_mode": "SINGLE",
                    "min_selections": 1,
                    "max_selections": 1,
                    "options": [
                        {
                            "value": column,
                            "label": column,
                            "consequence": f"Train a model that predicts {column}.",
                            "risk": "MEDIUM",
                            "risk_reason": "Choosing a different target changes the task definition and all reported metrics.",
                        }
                        for column in columns
                    ],
                    "recommendation": None,
                    "recommendation_reason": "A target cannot be inferred safely from structure alone.",
                }
            )
        if objective.get("iid_confirmed") is not True:
            questions.append(
                {
                    "question_id": "q_iid",
                    "prompt": "Can rows be treated as independent and identically distributed?",
                    "answer_schema": {"type": "boolean"},
                    "selection_mode": "SINGLE",
                    "min_selections": 1,
                    "max_selections": 1,
                    "options": [
                        {
                            "value": True,
                            "label": "Yes",
                            "consequence": "Use grouped duplicate protection with a random holdout.",
                            "risk": "HIGH",
                            "risk_reason": "Treating dependent rows as IID can make evaluation optimistic.",
                        },
                        {
                            "value": False,
                            "label": "No",
                            "consequence": "Pause because group/time splitting is not in this slice.",
                            "risk": "LOW",
                            "risk_reason": "The current execution backend will stop instead of using an unsafe split.",
                        },
                    ],
                    "recommendation": None,
                    "recommendation_reason": "The dataset does not prove row independence.",
                }
            )
        return questions

    @staticmethod
    def _positive_class_question(classes: list[Any]) -> dict[str, Any]:
        recommendation: Any = None
        recommendation_reason = "Class meaning is a business decision."
        if len(classes) == 2 and {str(value).strip() for value in classes} == {"0", "1"}:
            recommendation = next(value for value in classes if str(value).strip() == "1")
            recommendation_reason = (
                "The target labels are the conventional 0/1 encoding; 1 is the positive class."
            )
        return {
            "question_id": "q_positive_class",
            "prompt": "Which target value is the positive class?",
            "answer_schema": {
                "type": ["string", "number", "boolean"],
                "enum": classes,
            },
            "selection_mode": "SINGLE",
            "min_selections": 1,
            "max_selections": 1,
            "options": [
                {
                    "value": value,
                    "label": str(value),
                    "consequence": f"Treat {value!r} as the positive outcome.",
                    "risk": (
                        "LOW"
                        if recommendation is not None
                        and type(value) is type(recommendation)
                        and value == recommendation
                        else "MEDIUM"
                    ),
                    "risk_reason": (
                        "This matches the conventional positive label for 0/1 targets."
                        if recommendation is not None
                        and type(value) is type(recommendation)
                        and value == recommendation
                        else "Class orientation changes precision/recall interpretation and threshold metrics."
                    ),
                }
                for value in classes
            ],
            "recommendation": recommendation,
            "recommendation_reason": recommendation_reason,
        }

    @staticmethod
    def _decision_resolution_policy(run: dict[str, Any], questions: list[dict[str, Any]]) -> str:
        run_policy = run.get("policy", {})
        if (
            run_policy.get("allow_external_llm") is not True
            or run_policy.get("risk_tier", "STANDARD") != "STANDARD"
        ):
            return "HUMAN_REQUIRED"
        for question in questions:
            recommendation = question.get("recommendation")
            if recommendation is None:
                return "HUMAN_REQUIRED"
            recommended_values = (
                recommendation if question.get("selection_mode") == "MULTIPLE" else [recommendation]
            )
            if not isinstance(recommended_values, list):
                return "HUMAN_REQUIRED"
            options = question.get("options") or []
            if not options:
                return "HUMAN_REQUIRED"
            for value in recommended_values:
                option = next(
                    (
                        option
                        for option in options
                        if type(option.get("value")) is type(value) and option.get("value") == value
                    ),
                    None,
                )
                if option is None or option.get("risk") != "LOW":
                    return "HUMAN_REQUIRED"
        return "AGENT_ALLOWED"

    async def _request_decision(
        self,
        run: dict[str, Any],
        questions: list[dict[str, Any]],
        evidence_refs: list[str],
    ) -> None:
        packet_id = self.store.new_id("decision_packet")
        wait_set_id = self.store.new_id("wait_set")
        updated = await self.store.update_run(
            run["run_id"],
            {
                "phase": "PLAN",
                "status": "WAITING_USER",
                "progress": self._progress(35, "WAITING_USER", "Task information required"),
                "stages": _stages(
                    active_phase="PLAN",
                    active_status="WAITING_USER",
                    completed={"INGEST", "PROFILE"},
                ),
                "blocking": {"decision_packet_ids": [packet_id], "approval_ids": []},
                "available_actions": ["ANSWER", "PAUSE", "CANCEL"],
                "updated_at": iso_now(),
            },
            expected_revision=run["run_revision"],
            bump_revision=True,
        )
        packet = await self.store.create_decision_packet(
            run["run_id"],
            {
                "decision_packet_id": packet_id,
                "wait_set_id": wait_set_id,
                "wait_set_revision": 1,
                "run_id": run["run_id"],
                "tenant_id": run["tenant_id"],
                "run_revision": updated["run_revision"],
                "status": "OPEN",
                "kind": "CLARIFICATION",
                "reason": "Required task semantics cannot be derived safely from the uploaded table.",
                "blocking": True,
                "resolution_policy": self._decision_resolution_policy(run, questions),
                "questions": questions,
                "evidence_refs": evidence_refs,
                "created_at": iso_now(),
                "expires_at": (utcnow() + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            },
        )
        updated = await self._emit(
            updated,
            "run.phase_changed.v1",
            {"previous_phase": run["phase"], "phase": "PLAN", "status": "WAITING_USER"},
        )
        await self._emit(
            updated,
            "decision_packet.requested.v1",
            {
                "decision_packet_id": packet["decision_packet_id"],
                "wait_set_id": packet["wait_set_id"],
                "href": f"/v1/runs/{run['run_id']}/decision-packets",
            },
        )

    async def _execute_resolved_task(self, run: dict[str, Any]) -> WorkerResult:
        async with self._visibility_lock:
            current = await self.store.get_run(run["run_id"])
            if current is None or current["status"] == "TERMINAL":
                return COMPLETE
            pending_command_id = current.get("pending_command_id")
            current = await self.store.update_run(
                current["run_id"],
                {
                    "phase": "TRAIN",
                    "status": "QUEUED",
                    "execution_step": "TRAIN",
                    "progress": self._progress(40, "QUEUED", "Training step queued"),
                    "available_actions": ["PAUSE", "CANCEL"],
                    "pending_command_id": None,
                    "updated_at": iso_now(),
                },
                bump_revision=False,
            )
            if pending_command_id:
                await self.store.update_command(
                    pending_command_id,
                    {
                        "status": "SUCCEEDED",
                        "completed_at": iso_now(),
                        "resulting_run_revision": current["run_revision"],
                    },
                )
            return CHECKPOINT("TRAIN", checkpoint={"answers_applied": True})

    async def _execute_training(self, run: dict[str, Any]) -> WorkerResult:
        path, media_type = self._dataset_path(run)
        async with self._visibility_lock:
            current = await self.store.get_run(run["run_id"])
            if current is None or current["status"] == "TERMINAL":
                return COMPLETE
            if current["status"] == "PAUSED":
                return CHECKPOINT("TRAIN", status="WAITING", checkpoint={})
            current = await self.store.update_run(
                current["run_id"],
                {
                    "phase": "TRAIN",
                    "status": "RUNNING",
                    "execution_step": "TRAIN",
                    "progress": self._progress(55, "TRAIN", "Evaluating bounded model candidates"),
                    "stages": _stages(
                        active_phase="TRAIN",
                        active_status="RUNNING",
                        completed={"INGEST", "PROFILE", "PLAN"},
                    ),
                    "updated_at": iso_now(),
                },
                bump_revision=False,
            )
            await self._emit(
                current,
                "run.phase_changed.v1",
                {"previous_phase": "PLAN", "phase": "TRAIN", "status": "RUNNING"},
            )
            resolved = dict(current.get("resolved_inputs", {}))

        try:
            result = await asyncio.to_thread(
                self.backend_registry.run,
                current.get("backend_id") or resolved.get("backend_id") or "sklearn",
                path,
                media_type=media_type,
                target_column=resolved.get("target_column"),
                task_type=resolved.get("task_type"),
                positive_class=resolved.get("positive_class"),
                primary_metric=resolved.get("primary_metric"),
                iid_confirmed=resolved.get("iid_confirmed") is True,
                seed=1729,
                max_trials=int(current["budget"]["max_trials"]),
                max_wall_time_seconds=int(current["budget"]["max_wall_time_seconds"]),
            )
        except PositiveClassRequiredError as error:
            async with self._visibility_lock:
                current = await self.store.get_run(run["run_id"])
                assert current is not None
                await self._request_decision(
                    current,
                    [self._positive_class_question(error.context.get("classes", []))],
                    [],
                )
            return CHECKPOINT("RESOLVE_TASK", status="WAITING", checkpoint={})
        except MLEngineError as error:
            async with self._visibility_lock:
                current = await self.store.get_run(run["run_id"])
                if current is not None:
                    await self._fail_run(
                        current,
                        code=error.code,
                        message=str(error),
                        retriable=False,
                    )
            return COMPLETE

        async with self._visibility_lock:
            current = await self.store.get_run(run["run_id"])
            if current is None or current["status"] == "TERMINAL":
                return COMPLETE
            if current["status"] == "PAUSED":
                return CHECKPOINT("TRAIN", status="WAITING", checkpoint={})
            await self._publish_result(current, result)
            return COMPLETE

    async def _publish_result(self, run: dict[str, Any], result: TabularAutoMLResult) -> None:
        run_id = run["run_id"]
        resolved = run["resolved_inputs"]
        backend_descriptor = self.backend_registry.get(run.get("backend_id")).descriptor
        task_output, run = await self._commit_output(
            run,
            output_type="TASK_SPEC",
            phase="PLAN",
            summary={
                "code": "TASK_SPEC_CONFIRMED",
                "message": "The bounded tabular task specification is frozen.",
                "severity": "INFO",
            },
            payload={
                "kind": "TASK_SPEC",
                "backend_id": backend_descriptor.backend_id,
                "engine_version": backend_descriptor.engine_version,
                "task_type": result.task["task_type"],
                "target_column_id": resolved["target_column"],
                "positive_class": result.task.get("positive_class"),
                "primary_metric": result.task["primary_metric"],
                "guardrail_metrics": [],
                "split_strategy": (
                    "STRATIFIED_HOLDOUT"
                    if result.task["task_type"] == "BINARY_CLASSIFICATION"
                    else "RANDOM_HOLDOUT"
                ),
                "confidence": 1.0,
                "assumptions": ["Rows were explicitly confirmed as i.i.d."],
                "confirmed_by": "USER",
            },
        )
        run = await self.store.update_run(
            run_id, {"task_spec_output_id": task_output["output_id"]}, bump_revision=False
        )

        split_bytes = json.dumps(result.split, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        split_artifact = await self._create_artifact(
            run, "SPLIT_MANIFEST_JSON", "application/json", split_bytes
        )
        split_output, run = await self._commit_output(
            run,
            output_type="SPLIT_MANIFEST",
            phase="PLAN",
            summary={
                "code": "SPLIT_FROZEN",
                "message": "The development folds and sealed holdout are immutable.",
                "severity": "INFO",
            },
            payload={
                "kind": "SPLIT_MANIFEST",
                "strategy": result.split["strategy"],
                "train_rows": result.split["train_rows"],
                "validation_rows": result.split["validation_rows"],
                "test_rows": result.split["test_rows"],
                "leakage_checks": result.split["leakage_checks"],
            },
            artifact_refs=[self._artifact_ref(split_artifact)],
            lineage={"task_spec_output_id": task_output["output_id"]},
        )
        await self.store.update_artifact(
            split_artifact["artifact_id"], {"output_id": split_output["output_id"]}
        )
        run = await self.store.update_run(
            run_id,
            {"split_manifest_output_id": split_output["output_id"]},
            bump_revision=False,
        )

        baseline_metric = result.baseline["primary_metric"]
        baseline_value = result.baseline["cv_metrics"][baseline_metric]["mean"]
        _, run = await self._commit_output(
            run,
            output_type="BASELINE_RESULT",
            phase="TRAIN",
            summary={
                "code": "BASELINE_EVALUATED",
                "message": "The naive baseline was evaluated on development folds.",
                "severity": "INFO",
            },
            payload={
                "kind": "BASELINE_RESULT",
                "baselines": [
                    {
                        "name": result.baseline["family"],
                        "metrics": [_metric(baseline_metric, baseline_value)],
                        "compute_credits": 0,
                    }
                ],
            },
        )
        experiment_id = f"exp_{run_id}"
        for trial in result.trials:
            metrics = []
            if trial["status"] == "SUCCEEDED":
                metric_name = trial["primary_metric"]
                metrics = [_metric(metric_name, trial["primary_score"])]
            _, run = await self._commit_output(
                run,
                output_type="TRIAL_RESULT",
                phase="TRAIN",
                summary={
                    "code": "TRIAL_EVALUATED",
                    "message": f"{trial['family']} finished development-only evaluation.",
                    "severity": "INFO" if trial["status"] == "SUCCEEDED" else "WARNING",
                },
                payload={
                    "kind": "TRIAL_RESULT",
                    "experiment_id": experiment_id,
                    "trial_number": trial["trial_number"],
                    "status": trial["status"],
                    "backend_id": backend_descriptor.backend_id,
                    "engine_version": backend_descriptor.engine_version,
                    "model_family": trial["family"],
                    "metrics": metrics,
                    "compute_credits": 0,
                    "normalized_config": trial.get("config"),
                    "failure_code": trial.get("failure_code"),
                },
            )

        primary = result.evaluation["primary_metric"]
        baseline_holdout = result.evaluation["all_metrics"]["baseline"][primary]
        candidate_holdout = result.evaluation["all_metrics"]["candidate"][primary]
        evaluation_output, run = await self._commit_output(
            run,
            output_type="EVALUATION_REPORT",
            phase="EVALUATE",
            summary={
                "code": "CANDIDATE_EVALUATED",
                "message": "The frozen candidate was evaluated once on the sealed holdout.",
                "severity": "INFO",
            },
            payload={
                "kind": "EVALUATION_REPORT",
                "primary_metric": primary,
                "baseline": _metric(primary, baseline_holdout),
                "candidate": _metric(primary, candidate_holdout),
                "paired_delta": _metric(primary, result.evaluation["paired_improvement"]),
                "guardrails_passed": False,
                "eligible_candidate": False,
                "failed_gates": ["PRODUCTION_ELIGIBILITY_NOT_EVALUATED"],
                "limitations": result.evaluation["limitations"],
            },
        )

        model_artifact = await self._create_artifact(
            run,
            backend_descriptor.artifact_kind,
            backend_descriptor.artifact_media_type,
            result.model_bytes,
        )
        model_id = self.store.new_id("model")
        exportable = bool(result.model_metadata.get("exportable", True))
        model_card_output, run = await self._commit_output(
            run,
            output_type="MODEL_CARD",
            phase="PACKAGE",
            summary={
                "code": (
                    "EVALUATED_MODEL_PACKAGED" if exportable else "EVALUATION_METADATA_AVAILABLE"
                ),
                "message": (
                    "A trusted-store evaluated model package is available."
                    if exportable
                    else "Data-free backend evaluation metadata is available; no model was exported."
                ),
                "severity": "WARNING",
            },
            payload={
                "kind": "MODEL_CARD",
                "model_id": model_id,
                "backend_id": backend_descriptor.backend_id,
                "backend_version": backend_descriptor.backend_version,
                "engine_version": backend_descriptor.engine_version,
                "intended_use": (
                    "Offline evaluation only; production use is not approved."
                    if exportable
                    else "Evaluation evidence only; this Run did not export a loadable model."
                ),
                "limitations": result.evaluation["limitations"],
                "metrics": [_metric(primary, candidate_holdout)],
            },
            artifact_refs=[self._artifact_ref(model_artifact)],
            lineage={"evidence_refs": [evaluation_output["output_id"]]},
        )
        await self.store.update_artifact(
            model_artifact["artifact_id"], {"output_id": model_card_output["output_id"]}
        )

        report_artifact = await self._create_artifact(
            run, "RUN_REPORT_JSON", "application/json", result.report_bytes
        )
        report_output, run = await self._commit_output(
            run,
            output_type="RUN_REPORT",
            phase="PACKAGE",
            summary={
                "code": "REAL_RUN_COMPLETE",
                "message": "Real data was evaluated by the bounded tabular engine.",
                "severity": "INFO",
            },
            payload={
                "kind": "RUN_REPORT",
                "summary": "A deterministic evaluated candidate and evidence bundle were produced.",
                "recommendation": "Review limitations and define production quality gates.",
                "evidence_refs": [
                    task_output["output_id"],
                    split_output["output_id"],
                    evaluation_output["output_id"],
                    model_card_output["output_id"],
                ],
            },
            artifact_refs=[self._artifact_ref(report_artifact)],
            lineage={"evidence_refs": [evaluation_output["output_id"]]},
        )
        await self.store.update_artifact(
            report_artifact["artifact_id"], {"output_id": report_output["output_id"]}
        )

        outputs = await self.store.list_outputs(run_id=run_id)
        await self.store.set_result(
            run_id,
            {
                "result_manifest_id": self.store.new_id("result"),
                "run_id": run_id,
                "outcome": "SUCCEEDED",
                "model_disposition": "NO_ELIGIBLE_MODEL",
                "summary": "The real dataset was evaluated and an offline model artifact was produced.",
                "backend_id": backend_descriptor.backend_id,
                "backend_version": backend_descriptor.backend_version,
                "engine_version": backend_descriptor.engine_version,
                "output_refs": [self._output_ref(output) for output in outputs],
                "partial": False,
                "eligible_model": None,
                "reason": {
                    "code": "EVALUATED_CANDIDATE_REQUIRES_GATES",
                    "message": "No business threshold, risk approval, or production gate was evaluated.",
                    "retriable": False,
                    "failed_gates": ["PRODUCTION_ELIGIBILITY_NOT_EVALUATED"],
                    "evidence_refs": [evaluation_output["output_id"]],
                    "remediation": ["Define quality and risk gates before model registration."],
                },
                "completed_at": iso_now(),
            },
        )
        final = await self.store.update_run(
            run_id,
            {
                "phase": "PACKAGE",
                "status": "TERMINAL",
                "outcome": "SUCCEEDED",
                "execution_step": "COMPLETED",
                "progress": self._progress(100, "COMPLETED", "Run completed"),
                "stages": _stages(
                    active_phase="PACKAGE",
                    active_status="RUNNING",
                    completed={"INGEST", "PROFILE", "PLAN", "TRAIN", "EVALUATE", "PACKAGE"},
                ),
                "blocking": {"decision_packet_ids": [], "approval_ids": []},
                "available_actions": [],
                "latest_output_refs": [self._output_ref(report_output)],
                "budget_usage": {
                    **run["budget_usage"],
                    "trials": {
                        "used": len(result.trials),
                        "limit": run["budget_usage"]["trials"]["limit"],
                    },
                },
                "updated_at": iso_now(),
            },
            expected_revision=run["run_revision"],
            bump_revision=True,
        )
        await self._emit(
            final,
            "run.completed.v1",
            {"outcome": "SUCCEEDED", "result_href": f"/v1/runs/{run_id}/result"},
        )

    async def _create_artifact(
        self,
        run: dict[str, Any],
        kind: str,
        media_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        artifact_id = self.store.new_id("artifact")
        blob = await self.blob_store.put_artifact(
            tenant_id=run["tenant_id"],
            run_id=run["run_id"],
            artifact_id=artifact_id,
            content=content,
        )
        return await self.store.create_artifact(
            {
                "artifact_id": artifact_id,
                "tenant_id": run["tenant_id"],
                "run_id": run["run_id"],
                "output_id": None,
                "kind": kind,
                "media_type": media_type,
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
                    "task_spec_output_id": run.get("task_spec_output_id"),
                    "split_manifest_output_id": run.get("split_manifest_output_id"),
                    "policy_version": "policy.m2-local",
                    "method_version": "tabular-sklearn.v1",
                    "parent_refs": [],
                    "evidence_refs": [],
                },
            }
        )

    @staticmethod
    def _artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            key: artifact[key]
            for key in ["artifact_id", "kind", "media_type", "size_bytes", "sha256", "href"]
        }

    async def _fail_run(
        self,
        run: dict[str, Any],
        *,
        code: str,
        message: str,
        retriable: bool,
    ) -> None:
        failure_output, run = await self._commit_output(
            run,
            output_type="FAILURE_REPORT",
            phase=run["phase"],
            summary={"code": code, "message": message, "severity": "ERROR"},
            payload={
                "kind": "FAILURE_REPORT",
                "failure_code": code,
                "phase": run["phase"],
                "message": message,
                "retriable": retriable,
                "partial_output_ids": [
                    output["output_id"] for output in await self.store.list_outputs(run["run_id"])
                ],
                "remediation": ["Correct the dataset or task answers and create a new Run."],
            },
        )
        outputs = await self.store.list_outputs(run["run_id"])
        await self.store.set_result(
            run["run_id"],
            {
                "result_manifest_id": self.store.new_id("result"),
                "run_id": run["run_id"],
                "outcome": "FAILED",
                "model_disposition": "INCOMPLETE",
                "summary": message,
                "output_refs": [self._output_ref(output) for output in outputs],
                "partial": True,
                "eligible_model": None,
                "reason": {
                    "code": code,
                    "message": message,
                    "retriable": retriable,
                    "failed_gates": [code],
                    "evidence_refs": [failure_output["output_id"]],
                    "remediation": ["Correct the dataset or task answers and create a new Run."],
                },
                "completed_at": iso_now(),
            },
        )
        terminal = await self.store.update_run(
            run["run_id"],
            {
                "status": "TERMINAL",
                "outcome": "FAILED",
                "execution_step": "FAILED",
                "blocking": {"decision_packet_ids": [], "approval_ids": []},
                "available_actions": [],
                "latest_output_refs": [self._output_ref(failure_output)],
                "updated_at": iso_now(),
            },
            expected_revision=run["run_revision"],
            bump_revision=True,
        )
        await self._emit(
            terminal,
            "run.failed.v1",
            {
                "outcome": "FAILED",
                "failure_code": code,
                "result_href": f"/v1/runs/{run['run_id']}/result",
            },
        )


__all__ = ["DurableWorkflowService"]
