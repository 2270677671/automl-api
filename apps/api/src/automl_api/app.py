from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from hashlib import sha256
from importlib import resources
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, AsyncIterator

import anyio
from fastapi import Depends, FastAPI, Header, Path as PathParameter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .auth import (
    AuthConfigurationError,
    AuthSettings,
    Principal,
    build_authenticator,
    enforce_operation_scope,
    require_principal,
    scope_for_operation,
)
from .durable_workflow import DurableWorkflowService
from .errors import APIProblem, current_correlation_id, install_problem_handlers
from .limits import RuntimeLimits
from .models import (
    AgentActionList,
    AgentInterfaceManifest,
    AgentRunContext,
    AnswerDecisionPacketRequest,
    Artifact,
    CommandReceipt,
    CreateDatasetRequest,
    CreateRunRequest,
    DatasetUploadSession,
    DatasetVersion,
    DecisionPacketPage,
    DecisionPacketStatus,
    DownloadTicket,
    FinalizeDatasetRequest,
    OutputPage,
    OutputResource,
    OutputState,
    OutputType,
    RunEventPage,
    RunPage,
    RunPhase,
    RunResult,
    RunSnapshot,
    RunStatus,
    SignUploadPartsRequest,
    UploadPartsResponse,
)
from .persistence import SqliteStore
from .protocol import (
    configure_cursor_secret,
    decode_cursor,
    encode_cursor,
    parse_revision_etag,
    request_fingerprint,
)
from .security import validate_shared_secret
from .storage import (
    BlobNotFoundError,
    BlobStore,
    ExpiredTicketError,
    InvalidTicketError,
    LocalBlobStore,
    SyntheticBlobStore,
)
from .store import IdempotencyState, InMemoryStore, StoredResponse
from .version import __version__ as SERVICE_VERSION
from .worker import LocalExecutionWorker
from .workflow import WorkflowService


PrincipalDependency = Annotated[Principal, Depends(require_principal)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=16, max_length=128),
]
IfMatch = Annotated[str, Header(alias="If-Match")]
Operation = Callable[[], Awaitable[tuple[int, dict[str, Any], dict[str, str]]]]

ROOT_DIR = Path(__file__).resolve().parents[4]
OPENAPI_PATH = ROOT_DIR / "openapi" / "automl-api.yaml"
ACTIVE_OPENAPI_PATH = ROOT_DIR / "openapi" / "automl-agent-tools.yaml"
API_VERSION = "v1"
PYTHON_SDK_COMPATIBLE_VERSIONS = ">=0.6,<0.7"
ACTIVE_OPERATION_IDS = [
    "getAgentInterfaceManifest",
    "getAgentRunContext",
    "listAgentRunActions",
    "createDatasetUpload",
    "signDatasetUploadParts",
    "finalizeDatasetUpload",
    "getDatasetVersion",
    "createRun",
    "getRun",
    "readRunEvents",
    "listRunOutputs",
    "getRunOutput",
    "listDecisionPackets",
    "answerDecisionPacket",
    "pauseRun",
    "resumeRun",
    "cancelRun",
    "getCommand",
    "getRunResult",
    "getArtifact",
    "createArtifactDownloadTicket",
]
LOGGER = logging.getLogger("automl_api")
_INSECURE_TICKET_SECRETS = frozenset(
    {
        "replace-with-an-independent-random-secret",
        "change-this-ticket-secret-before-deployment",
    }
)


def _configured_ticket_secret(*, required: bool) -> bytes | None:
    configured = os.environ.get("AUTOML_TICKET_SECRET")
    if configured is None or not configured.strip():
        if required:
            raise AuthConfigurationError("AUTOML_TICKET_SECRET is required in production mode.")
        return None
    try:
        return validate_shared_secret(
            configured,
            name="AUTOML_TICKET_SECRET",
            rejected_values=_INSECURE_TICKET_SECRETS,
        )
    except ValueError as error:
        raise AuthConfigurationError("AUTOML_TICKET_SECRET is not production-safe.") from error


def _load_or_create_ticket_secret(root: Path) -> bytes:
    secret_path = root / "ticket-secret"
    try:
        return validate_shared_secret(
            secret_path.read_bytes(),
            name="Persisted ticket signing secret",
        )
    except FileNotFoundError:
        secret = os.urandom(32)
        secret_path.write_bytes(secret)
        secret_path.chmod(0o600)
        return secret
    except ValueError as error:
        raise AuthConfigurationError(
            "The persisted ticket signing secret is not production-safe."
        ) from error


def _validated(model: Any, value: Any, *, exclude_none: bool = False) -> dict[str, Any]:
    return model.model_validate(value).model_dump(mode="json", exclude_none=exclude_none)


def _authorize_operation(principal: Principal, operation_id: str) -> Principal:
    return enforce_operation_scope(principal, operation_id)


def _contract_bytes(filename: str) -> bytes:
    path = ROOT_DIR / "openapi" / filename
    if path.is_file():
        return path.read_bytes()
    return resources.files("automl_api._contracts").joinpath(filename).read_bytes()


def _active_openapi_sha256() -> str:
    return sha256(_contract_bytes("automl-agent-tools.yaml")).hexdigest()


def _page(items: list[dict[str, Any]], *, high_watermark: int) -> dict[str, Any]:
    return {
        "items": items,
        "page": {
            "next_cursor": None,
            "has_more": False,
            "high_watermark": high_watermark,
        },
    }


def _csv_values(value: str | None) -> list[str] | None:
    if value is None:
        return None
    values = sorted({part.strip() for part in value.split(",") if part.strip()})
    if not values:
        raise APIProblem(400, "invalid_filter", "Invalid filter", "A filter cannot be empty.")
    return values


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _representation_etag(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f'"{sha256(canonical).hexdigest()}"'


def _parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise APIProblem(416, "invalid_range", "Invalid Range", "Only one byte range is supported.")
    start_text, separator, end_text = value.removeprefix("bytes=").partition("-")
    if not separator or (not start_text and not end_text):
        raise APIProblem(416, "invalid_range", "Invalid Range", "The byte range is malformed.")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
    except ValueError as error:
        raise APIProblem(
            416, "invalid_range", "Invalid Range", "The byte range is malformed."
        ) from error
    if start < 0 or end < start or start >= size:
        raise APIProblem(
            416,
            "range_not_satisfiable",
            "Range not satisfiable",
            "The requested range is outside the artifact.",
            extras={"content_range": f"bytes */{size}"},
        )
    return start, min(end, size - 1)


def _validate_filter(values: list[str] | None, allowed: set[str], *, name: str) -> list[str] | None:
    invalid = sorted(set(values or []) - allowed)
    if invalid:
        raise APIProblem(
            400,
            "invalid_filter",
            "Invalid filter",
            f"Unsupported {name} value: {invalid[0]}.",
        )
    return values


def _webhook_route_shape(remaining_path: str) -> str:
    if remaining_path in {"", "/"}:
        return remaining_path
    parts = [part for part in remaining_path.split("/") if part]
    if len(parts) == 1:
        if parts[0].endswith(":rotate-secret"):
            return "/{id}:rotate-secret"
        if parts[0].endswith(":enable"):
            return "/{id}:enable"
        return "/{id}"
    if len(parts) == 2 and parts[1] == "deliveries":
        return "/{id}/deliveries"
    if len(parts) == 3 and parts[1] == "deliveries":
        if parts[2].endswith(":redeliver"):
            return "/{id}/deliveries/{id}:redeliver"
        return "/{id}/deliveries/{id}"
    return remaining_path


def _not_found(resource: str = "Resource") -> APIProblem:
    return APIProblem(
        404,
        "not_found",
        f"{resource} not found",
        "The resource does not exist or is not visible to this principal.",
    )


def _unsupported(capability: str) -> APIProblem:
    return APIProblem(
        501,
        "capability_not_implemented",
        "Capability not implemented",
        f"{capability} is part of the public contract but is not available in this profile.",
    )


def _event_cursor_expired(run_id: str, retained_from: int, position: int) -> APIProblem:
    return APIProblem(
        410,
        "cursor_expired",
        "Event cursor expired",
        "Refresh the RunSnapshot and resume from the retained event boundary.",
        extras={
            "run_id": run_id,
            "retained_from_seq": retained_from,
            "lost_event_range": {
                "from_seq": position + 1,
                "through_seq": retained_from - 1,
                "historical_events_recoverable": False,
            },
            "recovery": {
                "action": "GET_RUN_SNAPSHOT",
                "href": f"/v1/runs/{run_id}",
            },
        },
    )


def _validate_cursor(
    cursor: str,
    *,
    kind: str,
    principal: Principal,
    parent_id: str | None = None,
) -> dict[str, Any]:
    payload = decode_cursor(cursor)
    if (
        payload.get("kind") != kind
        or payload.get("tenant_id") != principal.tenant_id
        or (parent_id is not None and payload.get("parent_id") != parent_id)
    ):
        raise APIProblem(
            400,
            "invalid_cursor",
            "Invalid cursor",
            "The cursor is malformed or was issued for a different query.",
        )
    return payload


async def _idempotent(
    *,
    state: InMemoryStore,
    operation_id: str,
    principal: Principal,
    key: str,
    request: Request,
    body: Any,
    execute: Operation,
) -> JSONResponse:
    if any(ord(character) < 33 or ord(character) > 126 for character in key):
        raise APIProblem(
            400,
            "invalid_idempotency_key",
            "Invalid Idempotency-Key",
            "Idempotency-Key must contain visible ASCII characters only.",
        )
    fingerprint = request_fingerprint(
        method=request.method,
        path=request.url.path,
        query=dict(sorted(request.query_params.multi_items())),
        body=body,
        conditions={
            name.lower(): value
            for name in ("If-Match",)
            if (value := request.headers.get(name)) is not None
        },
    )
    scoped_operation = f"{principal.tenant_id}:{operation_id}"
    decision = await state.begin_idempotent_request(scoped_operation, key, fingerprint)
    if decision.state is IdempotencyState.CONFLICT:
        raise APIProblem(
            409,
            "idempotency_key_reused",
            "Idempotency-Key was reused",
            "Use a new Idempotency-Key when the request path, query, or body changes.",
        )
    if decision.state is IdempotencyState.IN_PROGRESS:
        decision = await state.wait_for_idempotent_response(
            scoped_operation, key, fingerprint, timeout=30.0
        )
    if decision.state is IdempotencyState.REPLAY and decision.response is not None:
        replay = decision.response
        return JSONResponse(
            status_code=replay.status_code,
            content=replay.body,
            headers=replay.headers,
        )
    if decision.state is not IdempotencyState.NEW:
        if decision.state is IdempotencyState.CONFLICT:
            raise APIProblem(
                409,
                "idempotency_key_reused",
                "Idempotency-Key was reused",
                "Use a new Idempotency-Key when the request changes.",
            )
        raise APIProblem(
            409,
            "idempotency_request_in_progress",
            "Request is still in progress",
            "Retry the same request with the same Idempotency-Key.",
            retriable=True,
            headers={"Retry-After": "1"},
        )

    try:
        status_code, response_body, headers = await execute()
        encoded_body = jsonable_encoder(response_body)
        stored: StoredResponse = await state.complete_idempotent_request(
            scoped_operation,
            key,
            fingerprint,
            status_code=status_code,
            body=encoded_body,
            headers={"X-Correlation-ID": current_correlation_id(), **headers},
        )
    except APIProblem as problem:
        stored = await state.complete_idempotent_request(
            scoped_operation,
            key,
            fingerprint,
            status_code=problem.status,
            body=problem.body(),
            headers={
                "Content-Type": "application/problem+json",
                "X-Correlation-ID": current_correlation_id(),
                **problem.headers,
            },
        )
    except Exception:
        await state.abort_idempotent_request(scoped_operation, key, fingerprint)
        raise
    return JSONResponse(
        status_code=stored.status_code,
        content=stored.body,
        headers=stored.headers,
    )


def create_app(
    state: InMemoryStore | None = None,
    *,
    blob_store: BlobStore | None = None,
) -> FastAPI:
    auth_settings = AuthSettings.from_env()
    try:
        build_authenticator(auth_settings)
    except AuthConfigurationError:
        raise
    if auth_settings.cursor_secret is not None:
        configure_cursor_secret(auth_settings.cursor_secret)
    elif configured_cursor_secret := os.environ.get("AUTOML_CURSOR_SECRET"):
        configure_cursor_secret(configured_cursor_secret)
    configured_ticket_secret = _configured_ticket_secret(
        required=auth_settings.mode == "production"
    )

    limits = RuntimeLimits.from_env()
    owns_state = state is None
    if state is None:
        state_root = Path(os.environ.get("AUTOML_STATE_DIR", ".automl-data")).resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        state = SqliteStore(state_root / "automl.db")
        blob_store = blob_store or LocalBlobStore(
            state_root / "objects",
            ticket_secret=configured_ticket_secret or _load_or_create_ticket_secret(state_root),
            max_upload_part_bytes=limits.max_upload_part_bytes,
        )
    else:
        blob_store = blob_store or SyntheticBlobStore()

    durable_mode = isinstance(state, SqliteStore) and blob_store.durable
    worker: LocalExecutionWorker | None = None
    if durable_mode:
        assert isinstance(state, SqliteStore)
        durable_service = DurableWorkflowService(state, blob_store=blob_store, limits=limits)
        service: WorkflowService = durable_service
        worker = LocalExecutionWorker(
            state,
            durable_service.handle_execution_job,
            on_dead=durable_service.handle_dead_job,
        )
    else:
        service = WorkflowService(state, blob_store=blob_store, limits=limits)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        if durable_mode:
            LOGGER.info("Managed AutoML local durable mode is active at %s", state.database_path)
        else:
            LOGGER.warning(
                "Synthetic in-memory mode is active; state is lost on restart and byte "
                "transfer may be disabled."
            )
        try:
            if worker is not None:
                await durable_service.ensure_execution_jobs()
                await worker.start()
            yield
        finally:
            if worker is not None:
                await worker.stop()
            if owns_state and isinstance(state, SqliteStore):
                await state.close()

    application = FastAPI(
        title="Managed AutoML API",
        version=SERVICE_VERSION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.store = state
    application.state.blob_store = blob_store
    application.state.workflow = service
    application.state.backend_registry = service.backend_registry
    application.state.worker = worker
    application.state.runtime_limits = limits
    install_problem_handlers(application)

    @application.exception_handler(RequestValidationError)
    async def validation_problem(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "code": str(item["type"]),
                "message": str(item["msg"]),
                "field": ".".join(str(part) for part in item.get("loc", ())),
            }
            for item in exc.errors()
        ]
        problem = APIProblem(
            422,
            "validation_failed",
            "Request validation failed",
            "One or more request fields are invalid.",
            extras={"errors": errors},
        )
        return JSONResponse(
            status_code=422,
            content=problem.body(),
            media_type="application/problem+json",
        )

    @application.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        mode = "milestone-2-local-durable" if durable_mode else "milestone-1-synthetic"
        return {"status": "ok", "mode": mode}

    @application.get("/readyz", include_in_schema=False)
    async def readiness() -> dict[str, str]:
        if isinstance(state, SqliteStore):
            await state.list_runs()
        return {"status": "ready"}

    @application.get("/openapi.yaml", include_in_schema=False)
    async def canonical_openapi() -> Response:
        if OPENAPI_PATH.is_file():
            return FileResponse(OPENAPI_PATH, media_type="application/yaml")
        return Response(
            content=_contract_bytes("automl-api.yaml"),
            media_type="application/yaml",
        )

    @application.get("/v1/agent/tool-openapi.yaml", include_in_schema=False)
    async def active_agent_openapi(principal: PrincipalDependency) -> Response:
        _authorize_operation(principal, "getAgentInterfaceManifest")
        if ACTIVE_OPENAPI_PATH.is_file():
            return FileResponse(
                ACTIVE_OPENAPI_PATH,
                media_type="application/yaml",
                headers={
                    "ETag": f'"{_active_openapi_sha256()}"',
                    "Cache-Control": "private, max-age=300",
                    "Vary": "Authorization",
                },
            )
        body = _contract_bytes("automl-agent-tools.yaml")
        return Response(
            content=body,
            media_type="application/yaml",
            headers={
                "ETag": f'"{sha256(body).hexdigest()}"',
                "Cache-Control": "private, max-age=300",
                "Vary": "Authorization",
            },
        )

    @application.get("/v1/agent/manifest")
    async def get_agent_manifest(principal: PrincipalDependency) -> dict[str, Any]:
        _authorize_operation(principal, "getAgentInterfaceManifest")
        return _validated(
            AgentInterfaceManifest,
            {
                "schema_version": "1.0",
                "service_version": SERVICE_VERSION,
                "api_version": API_VERSION,
                "profile_id": "local-durable-tabular-v1" if durable_mode else "synthetic-v1",
                "service_role": "AUTOML_EXECUTION_BACKEND",
                "planner_location": "EXTERNAL_AGENT_PLATFORM",
                "internal_llm_calls": False,
                "llm_budget_owner": "EXTERNAL_AGENT_PLATFORM",
                "max_llm_tokens_consumed": False,
                "credentials_must_remain_in_platform": True,
                "production_external_llm_safe": False,
                "openapi_href": "/openapi.yaml",
                "canonical_openapi_href": "/openapi.yaml",
                "agent_tools_openapi_href": "/v1/agent/tool-openapi.yaml",
                "agent_tools_openapi_sha256": _active_openapi_sha256(),
                "python_sdk_compatible_versions": PYTHON_SDK_COMPATIBLE_VERSIONS,
                "context_path_template": "/v1/runs/{run_id}/agent-context",
                "actions_path_template": "/v1/runs/{run_id}/agent-actions",
                "canonical_operation_ids": ACTIVE_OPERATION_IDS,
                "active_operation_ids": ACTIVE_OPERATION_IDS,
                "operation_scopes": {
                    operation_id: scope_for_operation(operation_id)
                    for operation_id in ACTIVE_OPERATION_IDS
                },
                "runtime_limits": limits.manifest(),
                "default_backend_id": service.backend_registry.default_backend_id,
                "backends": service.backend_registry.status(),
                "supported_capabilities": [
                    "single_table_tabular_upload",
                    "csv",
                    "parquet",
                    "binary_classification",
                    "regression",
                    "offline_evaluation",
                    "human_interrupt_resume",
                    "agent_allowed_recommended_answers",
                ],
                "unsupported_capabilities": [
                    "production_deployment",
                    "online_inference",
                    "multi_table_learning",
                    "forecasting",
                    "ranking",
                    "unbounded_hyperparameter_search",
                    "internal_llm_planning",
                ],
                "supported_task_types": ["BINARY_CLASSIFICATION", "REGRESSION"],
                "supported_media_types": [
                    "text/csv",
                    "application/vnd.apache.parquet",
                ],
            },
        )

    @application.post("/v1/datasets", status_code=201)
    async def create_dataset(
        body: CreateDatasetRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "createDatasetUpload")
        payload = body.model_dump(mode="json")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.create_dataset(
                principal,
                payload,
                public_base_url=str(request.base_url).rstrip("/"),
            )
            return 201, _validated(DatasetUploadSession, result), {}

        return await _idempotent(
            state=state,
            operation_id="createDatasetUpload",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.post("/v1/dataset-versions/{dataset_version_id}/upload-parts:sign")
    async def sign_upload_parts(
        dataset_version_id: str,
        body: SignUploadPartsRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "signDatasetUploadParts")
        payload = body.model_dump(mode="json")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.sign_upload_parts(
                principal,
                dataset_version_id,
                payload,
                public_base_url=str(request.base_url).rstrip("/"),
            )
            return 200, _validated(UploadPartsResponse, result), {}

        return await _idempotent(
            state=state,
            operation_id="signDatasetUploadParts",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.put(
        "/v1/dataset-versions/{dataset_version_id}/upload-parts/{part_number}",
        include_in_schema=False,
        status_code=204,
    )
    async def upload_dataset_part(
        dataset_version_id: str,
        part_number: Annotated[int, PathParameter(ge=1)],
        upload_id: str,
        request: Request,
        principal: PrincipalDependency,
        declared_part: Annotated[str | None, Header(alias="x-automl-upload-part")] = None,
    ) -> Response:
        _authorize_operation(principal, "signDatasetUploadParts")
        if declared_part != str(part_number):
            raise APIProblem(
                400,
                "invalid_upload_header",
                "Invalid upload header",
                "x-automl-upload-part must match the part number in the URL.",
            )
        info = await service.put_upload_part(
            principal,
            dataset_version_id,
            upload_id,
            part_number,
            request.stream(),
        )
        return Response(
            status_code=204,
            headers={
                "ETag": info.etag,
                "X-Content-SHA256": info.sha256,
                "X-Content-Length": str(info.size_bytes),
            },
        )

    @application.post("/v1/dataset-versions/{dataset_version_id}:finalize", status_code=202)
    async def finalize_dataset(
        dataset_version_id: str,
        body: FinalizeDatasetRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "finalizeDatasetUpload")
        payload = body.model_dump(mode="json")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.finalize_dataset(principal, dataset_version_id, payload)
            return 202, _validated(DatasetVersion, result, exclude_none=True), {"Retry-After": "1"}

        return await _idempotent(
            state=state,
            operation_id="finalizeDatasetUpload",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.get("/v1/dataset-versions/{dataset_version_id}")
    async def get_dataset_version(
        dataset_version_id: str,
        principal: PrincipalDependency,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        _authorize_operation(principal, "getDatasetVersion")
        result = await service.get_dataset_version(principal, dataset_version_id)
        etag = _etag(int(result["revision"]))
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(
            content=_validated(DatasetVersion, result, exclude_none=True),
            headers={"ETag": etag},
        )

    @application.post("/v1/runs", status_code=202)
    async def create_run(
        body: CreateRunRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "createRun")
        payload = body.model_dump(mode="json")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.create_run(principal, payload)
            validated = _validated(RunSnapshot, result)
            return (
                202,
                validated,
                {
                    "ETag": _representation_etag(validated),
                    "Retry-After": "1",
                },
            )

        return await _idempotent(
            state=state,
            operation_id="createRun",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.get("/v1/runs")
    async def list_runs(
        principal: PrincipalDependency,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        status: str | None = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listRuns")
        requested_statuses = _validate_filter(
            _csv_values(status), {item.value for item in RunStatus}, name="Run status"
        )
        all_runs = await service.list_runs(principal)
        all_runs.sort(key=lambda item: item["run_id"])
        if cursor:
            if limit is not None or status is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first Run page, send only cursor.",
                )
            cursor_data = _validate_cursor(cursor, kind="runs", principal=principal)
            frozen = int(cursor_data.get("high_watermark", -1))
            high_id = cursor_data.get("high_id")
            position = cursor_data.get("position")
            page_size = int(cursor_data.get("limit", 0))
            requested_statuses = cursor_data.get("statuses")
            if (
                frozen < 0
                or not isinstance(high_id, str)
                or not isinstance(position, str)
                or page_size < 1
            ):
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
        else:
            position = ""
            page_size = limit or 50
            frozen = len(all_runs)
            high_id = all_runs[-1]["run_id"] if all_runs else ""
        window = [item for item in all_runs if position < item["run_id"] <= high_id]
        if requested_statuses is not None:
            allowed = set(requested_statuses)
            window = [item for item in window if item["status"] in allowed]
        selected = window[:page_size]
        has_more = len(window) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "runs",
                    "tenant_id": principal.tenant_id,
                    "high_watermark": frozen,
                    "high_id": high_id,
                    "position": selected[-1]["run_id"],
                    "limit": page_size,
                    "statuses": requested_statuses,
                }
            )
            if has_more
            else None
        )
        page = {
            "items": selected,
            "page": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "high_watermark": frozen,
            },
        }
        return JSONResponse(content=_validated(RunPage, page))

    @application.get("/v1/runs/{run_id}")
    async def get_run(
        run_id: str,
        principal: PrincipalDependency,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        _authorize_operation(principal, "getRun")
        result = await service.get_run(principal, run_id)
        validated = _validated(RunSnapshot, result)
        etag = _representation_etag(validated)
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(content=validated, headers={"ETag": etag})

    @application.get("/v1/runs/{run_id}/agent-context")
    async def get_agent_context(
        run_id: str,
        principal: PrincipalDependency,
        output_limit: Annotated[int, Query(ge=1, le=100)] = 20,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        _authorize_operation(principal, "getAgentRunContext")
        result = await service.get_agent_context(
            principal,
            run_id,
            output_limit=output_limit,
        )
        validated = _validated(AgentRunContext, result, exclude_none=True)
        etag = _representation_etag(validated)
        headers = {
            "ETag": etag,
            "Cache-Control": "private, no-cache",
            "Vary": "Authorization",
        }
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=validated, headers=headers)

    @application.get("/v1/runs/{run_id}/agent-actions")
    async def list_agent_actions(
        run_id: str,
        principal: PrincipalDependency,
    ) -> JSONResponse:
        _authorize_operation(principal, "listAgentRunActions")
        result = await service.get_agent_actions(principal, run_id)
        validated = _validated(AgentActionList, result)
        return JSONResponse(
            content=validated,
            headers={
                "ETag": _representation_etag(validated),
                "Cache-Control": "private, no-cache",
                "Vary": "Authorization",
            },
        )

    @application.get("/v1/runs/{run_id}/stages")
    async def list_run_stages(run_id: str, principal: PrincipalDependency) -> dict[str, Any]:
        _authorize_operation(principal, "listRunStages")
        snapshot = await service.get_run(principal, run_id)
        return {
            "run_id": run_id,
            "snapshot_seq": snapshot["snapshot_seq"],
            "stages": snapshot["stages"],
        }

    async def sse_stream(
        *,
        run_id: str,
        principal: Principal,
        after_seq: int,
        event_types: list[str] | None,
    ):
        position = after_seq
        allowed = set(event_types) if event_types is not None else None
        last_authorized_at = monotonic()
        while True:
            if monotonic() - last_authorized_at >= 30:
                await service.get_run(principal, run_id)
                last_authorized_at = monotonic()
            events = await state.get_events(run_id, after_seq=position)
            if events:
                for event in events:
                    if monotonic() - last_authorized_at >= 30:
                        await service.get_run(principal, run_id)
                        last_authorized_at = monotonic()
                    position = max(position, int(event["seq"]))
                    if allowed is not None and event["type"] not in allowed:
                        continue
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
                continue
            snapshot = await service.get_run(principal, run_id)
            if snapshot["status"] == "TERMINAL" and position >= snapshot["snapshot_seq"]:
                return
            events = await state.wait_for_events(run_id, position, timeout=15.0)
            if not events:
                await service.get_run(principal, run_id)
                last_authorized_at = monotonic()
                yield ": heartbeat\n\n"

    @application.get("/v1/runs/{run_id}/events")
    async def read_run_events(
        run_id: str,
        principal: PrincipalDependency,
        accept: Annotated[str, Header(alias="Accept")] = "application/json",
        after_seq: Annotated[int | None, Query(ge=0)] = None,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        types: str | None = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        _authorize_operation(principal, "readRunEvents")
        snapshot = await service.get_run(principal, run_id)
        event_types = _csv_values(types)
        retained_from = int(snapshot["retained_from_seq"])
        if "text/event-stream" in accept:
            if cursor is not None:
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "SSE does not use cursor."
                )
            if last_event_id is not None:
                if not last_event_id.isdigit():
                    raise APIProblem(
                        400,
                        "invalid_last_event_id",
                        "Invalid Last-Event-ID",
                        "Last-Event-ID must be a non-negative integer.",
                    )
                start = int(last_event_id)
            elif after_seq is not None:
                start = after_seq
            else:
                start = int(snapshot["snapshot_seq"])
            if start < max(0, retained_from - 1):
                raise _event_cursor_expired(run_id, retained_from, start)
            return StreamingResponse(
                sse_stream(
                    run_id=run_id,
                    principal=principal,
                    after_seq=start,
                    event_types=event_types,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        if last_event_id is not None:
            raise APIProblem(
                400,
                "invalid_last_event_id",
                "Invalid Last-Event-ID",
                "Last-Event-ID is only valid for SSE subscriptions.",
            )
        if cursor:
            if after_seq is not None or limit is not None or types is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first JSON event page, send only cursor.",
                )
            cursor_data = _validate_cursor(
                cursor, kind="events", principal=principal, parent_id=run_id
            )
            position = int(cursor_data.get("position", -1))
            high_watermark = int(cursor_data.get("high_watermark", -1))
            page_size = int(cursor_data.get("limit", 0))
            cursor_types = cursor_data.get("types")
            if position < 0 or high_watermark < 0 or page_size < 1:
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
            event_types = cursor_types
        else:
            position = max(0, retained_from - 1) if after_seq is None else after_seq
            high_watermark = int(snapshot["snapshot_seq"])
            page_size = limit or 50
        if position < max(0, retained_from - 1):
            raise _event_cursor_expired(run_id, retained_from, position)
        candidates = await state.get_events(run_id, after_seq=position, types=event_types)
        candidates = [item for item in candidates if int(item["seq"]) <= high_watermark]
        selected = candidates[:page_size]
        has_more = len(candidates) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "events",
                    "tenant_id": principal.tenant_id,
                    "parent_id": run_id,
                    "position": int(selected[-1]["seq"]),
                    "high_watermark": high_watermark,
                    "limit": page_size,
                    "types": event_types,
                }
            )
            if has_more
            else None
        )
        page = {
            "items": selected,
            "next_cursor": next_cursor,
            "high_watermark": high_watermark,
            "retained_from_seq": retained_from,
        }
        return JSONResponse(content=_validated(RunEventPage, page))

    @application.get("/v1/runs/{run_id}/outputs")
    async def list_outputs(
        run_id: str,
        principal: PrincipalDependency,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        output_types: Annotated[str | None, Query(alias="type")] = None,
        phases: Annotated[str | None, Query(alias="phase")] = None,
        state_filter: Annotated[str | None, Query(alias="state")] = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listRunOutputs")
        snapshot = await service.get_run(principal, run_id)
        requested_types = _validate_filter(
            _csv_values(output_types),
            {item.value for item in OutputType},
            name="Output type",
        )
        requested_phases = _validate_filter(
            _csv_values(phases),
            {item.value for item in RunPhase},
            name="Run phase",
        )
        if state_filter is not None:
            _validate_filter(
                [state_filter],
                {item.value for item in OutputState},
                name="Output state",
            )
        if cursor:
            if (
                limit is not None
                or output_types is not None
                or phases is not None
                or state_filter is not None
            ):
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first Output page, send only cursor.",
                )
            cursor_data = _validate_cursor(
                cursor, kind="outputs", principal=principal, parent_id=run_id
            )
            position = int(cursor_data.get("position", -1))
            high_watermark = int(cursor_data.get("high_watermark", -1))
            page_size = int(cursor_data.get("limit", 0))
            requested_types = cursor_data.get("types")
            requested_phases = cursor_data.get("phases")
            state_filter = cursor_data.get("state")
            if position < 0 or high_watermark < 0 or page_size < 1:
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
        else:
            position = 0
            high_watermark = int(snapshot["snapshot_seq"])
            page_size = limit or 50
        outputs = await service.list_outputs(principal, run_id)
        outputs.sort(key=lambda item: (int(item["created_seq"]), item["output_id"]))
        allowed_types = set(requested_types or [])
        allowed_phases = set(requested_phases or [])
        candidates = [
            item
            for item in outputs
            if position < int(item["created_seq"]) <= high_watermark
            and (not allowed_types or item["type"] in allowed_types)
            and (not allowed_phases or item["phase"] in allowed_phases)
            and (state_filter is None or item["state"] == state_filter)
        ]
        selected = candidates[:page_size]
        has_more = len(candidates) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "outputs",
                    "tenant_id": principal.tenant_id,
                    "parent_id": run_id,
                    "position": int(selected[-1]["created_seq"]),
                    "high_watermark": high_watermark,
                    "limit": page_size,
                    "types": requested_types,
                    "phases": requested_phases,
                    "state": state_filter,
                }
            )
            if has_more
            else None
        )
        page = {
            "items": selected,
            "page": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "high_watermark": high_watermark,
            },
        }
        return JSONResponse(content=_validated(OutputPage, page))

    @application.get("/v1/runs/{run_id}/outputs/{output_id}")
    async def get_output(
        run_id: str, output_id: str, principal: PrincipalDependency
    ) -> JSONResponse:
        _authorize_operation(principal, "getRunOutput")
        result = await service.get_output(principal, run_id, output_id)
        return JSONResponse(content=_validated(OutputResource, result))

    @application.get("/v1/runs/{run_id}/decision-packets")
    async def list_decision_packets(
        run_id: str,
        principal: PrincipalDependency,
        status: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listDecisionPackets")
        if status is not None:
            _validate_filter(
                [status],
                {item.value for item in DecisionPacketStatus},
                name="DecisionPacket status",
            )
        packets = await service.list_decision_packets(principal, run_id, status=status)
        packets.sort(key=lambda item: item["decision_packet_id"])
        if cursor:
            if status is not None or limit is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first DecisionPacket page, send only cursor.",
                )
            cursor_data = _validate_cursor(
                cursor, kind="decision-packets", principal=principal, parent_id=run_id
            )
            high_watermark = int(cursor_data.get("high_watermark", -1))
            high_id = cursor_data.get("high_id")
            position = cursor_data.get("position")
            page_size = int(cursor_data.get("limit", 0))
            status = cursor_data.get("status")
            if (
                high_watermark < 0
                or not isinstance(high_id, str)
                or not isinstance(position, str)
                or page_size < 1
            ):
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
            packets = await service.list_decision_packets(principal, run_id, status=status)
            packets.sort(key=lambda item: item["decision_packet_id"])
        else:
            position = ""
            high_watermark = len(packets)
            page_size = limit or 50
            high_id = packets[-1]["decision_packet_id"] if packets else ""
        window = [item for item in packets if position < item["decision_packet_id"] <= high_id]
        selected = window[:page_size]
        has_more = len(window) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "decision-packets",
                    "tenant_id": principal.tenant_id,
                    "parent_id": run_id,
                    "high_id": high_id,
                    "position": selected[-1]["decision_packet_id"],
                    "high_watermark": high_watermark,
                    "limit": page_size,
                    "status": status,
                }
            )
            if has_more
            else None
        )
        page = {
            "items": selected,
            "page": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "high_watermark": high_watermark,
            },
        }
        return JSONResponse(content=_validated(DecisionPacketPage, page, exclude_none=True))

    @application.post("/v1/runs/{run_id}/decision-packets/{wait_set_id}:answer", status_code=202)
    async def answer_decision_packet(
        run_id: str,
        wait_set_id: str,
        body: AnswerDecisionPacketRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> JSONResponse:
        _authorize_operation(principal, "answerDecisionPacket")
        payload = body.model_dump(mode="json")
        revision = parse_revision_etag(if_match, field="wait_set_revision")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.answer(principal, run_id, wait_set_id, revision, payload)
            return 202, _validated(CommandReceipt, result), {"Retry-After": "1"}

        return await _idempotent(
            state=state,
            operation_id="answerDecisionPacket",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    async def control_run(
        *,
        operation_id: str,
        run_id: str,
        principal: Principal,
        key: str,
        request: Request,
        revision: int | None,
    ) -> JSONResponse:
        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            if operation_id == "pauseRun":
                result = await service.pause(principal, run_id, revision or 0)
            elif operation_id == "resumeRun":
                result = await service.resume(principal, run_id, revision or 0)
            else:
                result = await service.cancel(principal, run_id)
            return 202, _validated(CommandReceipt, result), {}

        return await _idempotent(
            state=state,
            operation_id=operation_id,
            principal=principal,
            key=key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.post("/v1/runs/{run_id}:pause", status_code=202)
    async def pause_run(
        run_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> JSONResponse:
        _authorize_operation(principal, "pauseRun")
        return await control_run(
            operation_id="pauseRun",
            run_id=run_id,
            principal=principal,
            key=idempotency_key,
            request=request,
            revision=parse_revision_etag(if_match, field="run_revision"),
        )

    @application.post("/v1/runs/{run_id}:resume", status_code=202)
    async def resume_run(
        run_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> JSONResponse:
        _authorize_operation(principal, "resumeRun")
        return await control_run(
            operation_id="resumeRun",
            run_id=run_id,
            principal=principal,
            key=idempotency_key,
            request=request,
            revision=parse_revision_etag(if_match, field="run_revision"),
        )

    @application.post("/v1/runs/{run_id}:cancel", status_code=202)
    async def cancel_run(
        run_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "cancelRun")
        return await control_run(
            operation_id="cancelRun",
            run_id=run_id,
            principal=principal,
            key=idempotency_key,
            request=request,
            revision=None,
        )

    @application.get("/v1/commands/{command_id}")
    async def get_command(command_id: str, principal: PrincipalDependency) -> JSONResponse:
        _authorize_operation(principal, "getCommand")
        result = await service.get_command(principal, command_id)
        return JSONResponse(content=_validated(CommandReceipt, result))

    @application.get("/v1/runs/{run_id}/result")
    async def get_run_result(run_id: str, principal: PrincipalDependency) -> JSONResponse:
        _authorize_operation(principal, "getRunResult")
        result = await service.get_result(principal, run_id)
        return JSONResponse(content=_validated(RunResult, result))

    @application.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, principal: PrincipalDependency) -> JSONResponse:
        _authorize_operation(principal, "getArtifact")
        result = await service.get_artifact(principal, artifact_id)
        return JSONResponse(content=_validated(Artifact, result))

    @application.post("/v1/artifacts/{artifact_id}:download", status_code=201)
    async def create_download_ticket(
        artifact_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "createArtifactDownloadTicket")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            result = await service.create_download_ticket(
                principal,
                artifact_id,
                public_base_url=str(request.base_url).rstrip("/"),
            )
            return 201, _validated(DownloadTicket, result), {}

        return await _idempotent(
            state=state,
            operation_id="createArtifactDownloadTicket",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.get("/v1/artifact-downloads/{token}", include_in_schema=False)
    async def download_artifact_bytes(
        token: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> StreamingResponse:
        try:
            ticket = blob_store.verify_download_token(token)
        except ExpiredTicketError as error:
            raise APIProblem(
                410,
                "download_ticket_expired",
                "Download ticket expired",
                "Request a new artifact download ticket.",
            ) from error
        except InvalidTicketError as error:
            raise _not_found("Download ticket") from error

        artifact = await state.get_artifact(str(ticket.get("artifact_id", "")))
        if (
            artifact is None
            or artifact.get("tenant_id") != ticket.get("tenant_id")
            or artifact.get("etag") != ticket.get("etag")
            or artifact.get("state") != "COMMITTED"
        ):
            raise _not_found("Artifact")
        if if_match != artifact["etag"]:
            raise APIProblem(
                412,
                "artifact_etag_mismatch",
                "Artifact ETag mismatch",
                "Use the If-Match value returned in the download ticket.",
            )
        try:
            path = blob_store.path_for_key(str(artifact.get("blob_key", "")))
        except BlobNotFoundError as error:
            raise APIProblem(
                410,
                "artifact_gone",
                "Artifact is unavailable",
                "The artifact bytes are no longer available.",
            ) from error

        size = int(artifact["size_bytes"])
        selected = _parse_byte_range(range_header, size)
        start, end = selected or (0, size - 1)
        length = end - start + 1

        async def body() -> AsyncIterator[bytes]:
            remaining = length
            async with await anyio.open_file(path, "rb") as handle:
                await handle.seek(start)
                while remaining:
                    chunk = await handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "ETag": artifact["etag"],
            "X-Content-SHA256": artifact["sha256"],
        }
        if selected is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            body(),
            status_code=206 if selected is not None else 200,
            media_type=artifact["media_type"],
            headers=headers,
        )

    @application.get("/v1/runs/{run_id}/experiments")
    async def list_experiments(run_id: str, principal: PrincipalDependency) -> dict[str, Any]:
        _authorize_operation(principal, "listRunExperiments")
        await service.get_run(principal, run_id)
        return _page([], high_watermark=0)

    @application.get("/v1/runs/{run_id}/experiments/{experiment_id}")
    async def get_experiment(
        run_id: str, experiment_id: str, principal: PrincipalDependency
    ) -> Response:
        _authorize_operation(principal, "getRunExperiment")
        await service.get_run(principal, run_id)
        raise _not_found("Experiment")

    @application.get("/v1/runs/{run_id}/approvals")
    async def list_approvals(run_id: str, principal: PrincipalDependency) -> dict[str, Any]:
        _authorize_operation(principal, "listRunApprovals")
        await service.get_run(principal, run_id)
        return _page([], high_watermark=0)

    @application.post("/v1/runs/{run_id}/approvals/{approval_id}:decide")
    async def decide_approval(
        run_id: str, approval_id: str, principal: PrincipalDependency
    ) -> Response:
        _authorize_operation(principal, "decideApproval")
        await service.get_run(principal, run_id)
        raise _unsupported("Approval decisions")

    @application.get("/v1/models/{model_id}")
    async def get_model(model_id: str, principal: PrincipalDependency) -> Response:
        _authorize_operation(principal, "getModelCandidate")
        raise _not_found("Model candidate")

    @application.api_route(
        "/v1/webhook-endpoints{remaining_path:path}",
        methods=["GET", "POST", "DELETE"],
    )
    async def webhooks_not_implemented(
        remaining_path: str, principal: PrincipalDependency, request: Request
    ) -> Response:
        operation_id = {
            ("POST", ""): "createWebhookEndpoint",
            ("GET", ""): "listWebhookEndpoints",
            ("GET", "/"): "listWebhookEndpoints",
            ("GET", "/{id}"): "getWebhookEndpoint",
            ("DELETE", "/{id}"): "deleteWebhookEndpoint",
            ("POST", "/{id}:rotate-secret"): "rotateWebhookEndpointSecret",
            ("POST", "/{id}:enable"): "enableWebhookEndpoint",
            ("GET", "/{id}/deliveries"): "listWebhookDeliveries",
            ("GET", "/{id}/deliveries/{id}"): "getWebhookDelivery",
            ("POST", "/{id}/deliveries/{id}:redeliver"): "redeliverWebhookDelivery",
        }.get((request.method, _webhook_route_shape(remaining_path)))
        if operation_id is not None:
            _authorize_operation(principal, operation_id)
        raise _unsupported("Webhook management")

    @application.delete("/v1/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, principal: PrincipalDependency) -> Response:
        _authorize_operation(principal, "deleteDataset")
        dataset = await state.get_dataset(dataset_id)
        service._owned(dataset, principal)
        raise _unsupported("Dataset deletion")

    @application.get("/v1/deletions/{deletion_id}")
    async def get_deletion(deletion_id: str, principal: PrincipalDependency) -> Response:
        _authorize_operation(principal, "getDeletionJob")
        raise _not_found("Deletion job")

    return application


app = create_app()
