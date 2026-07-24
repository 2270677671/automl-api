from __future__ import annotations

import json
import logging
import os
import base64
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
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
    ApprovalPage,
    Artifact,
    CommandReceipt,
    CreateDatasetRequest,
    CreateWebhookEndpointRequest,
    CreateRunRequest,
    DatasetUploadSession,
    DatasetVersion,
    DecideApprovalRequest,
    DecisionPacketPage,
    DecisionPacketStatus,
    DeletionJob,
    DownloadTicket,
    FinalizeDatasetRequest,
    ModelCandidate,
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
    WebhookDelivery,
    WebhookDeliveryPage,
    WebhookEndpoint,
    WebhookEndpointCreated,
    WebhookRedeliveryReceipt,
    WebhookSecretRotated,
)
from .persistence import SqliteStore
from .production import ProductionSettings
from .protocol import (
    configure_cursor_secret,
    decode_cursor,
    encode_cursor,
    iso_now,
    parse_revision_etag,
    request_fingerprint,
    utcnow,
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
PYTHON_SDK_COMPATIBLE_VERSIONS = ">=0.7,<0.8"
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


def _new_webhook_secret() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def _public_webhook_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    hidden = {
        "tenant_id",
        "signing_secret",
        "previous_signing_secret",
        "previous_secret_valid_until",
    }
    return {key: value for key, value in endpoint.items() if key not in hidden}


def _public_webhook_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in delivery.items() if key != "tenant_id"}


def _public_approval(approval: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in approval.items() if key != "tenant_id"}


def _public_model(model: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in model.items() if key != "tenant_id"}


def _public_deletion(deletion: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in deletion.items() if key != "tenant_id"}


def _approval_expires_at(approval: dict[str, Any]) -> datetime:
    raw = approval.get("expires_at")
    if not isinstance(raw, str):
        raise APIProblem(409, "approval_expired", "Approval is expired", "Refresh the Run state.")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise APIProblem(
            409, "approval_expired", "Approval is expired", "Refresh the Run state."
        ) from error


def _require_human_approval(principal: Principal) -> None:
    if principal.authentication_mode == "production" and principal.actor_type != "human":
        raise APIProblem(
            403,
            "human_approval_required",
            "Human approval required",
            "Production deployment approvals must be decided by a human principal.",
        )


def _completed_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed_at = iso_now()
    result: list[dict[str, Any]] = []
    for stage in stages:
        item = dict(stage)
        if item.get("status") not in {"COMPLETED", "SKIPPED"}:
            item.update(
                {
                    "status": "COMPLETED",
                    "completed_at": completed_at,
                    "message": "Completed after production approval decision",
                }
            )
        result.append(item)
    return result


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
    production_settings = ProductionSettings.from_env()
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
    application.state.production = production_settings
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
    async def health() -> dict[str, Any]:
        mode = "milestone-2-local-durable" if durable_mode else "milestone-1-synthetic"
        return {
            "status": "ok",
            "mode": mode,
            "deployment_profile": production_settings.profile,
        }

    @application.get("/readyz", include_in_schema=False)
    async def readiness() -> dict[str, Any]:
        if isinstance(state, SqliteStore):
            await state.list_runs()
        production = production_settings.manifest()
        if production_settings.strict and not production_settings.ready:
            raise APIProblem(
                503,
                "production_preflight_failed",
                "Production preflight failed",
                "The formal production profile is not fully configured.",
                retriable=True,
                extras={"production": production},
            )
        return {"status": "ready", "production": production}

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
    async def list_approvals(
        run_id: str,
        principal: PrincipalDependency,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listRunApprovals")
        await service.get_run(principal, run_id)
        approvals = [
            _public_approval(item)
            for item in await state.list_approvals(run_id)
            if item.get("tenant_id") == principal.tenant_id
        ]
        approvals.sort(key=lambda item: item["approval_id"])
        if cursor:
            if limit is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first Approval page, send only cursor.",
                )
            cursor_data = _validate_cursor(
                cursor, kind="approvals", principal=principal, parent_id=run_id
            )
            high_id = cursor_data.get("high_id")
            position = cursor_data.get("position")
            page_size = int(cursor_data.get("limit", 0))
            high_watermark = int(cursor_data.get("high_watermark", -1))
            if (
                not isinstance(high_id, str)
                or not isinstance(position, str)
                or page_size < 1
                or high_watermark < 0
            ):
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
        else:
            position = ""
            page_size = limit or 50
            high_watermark = len(approvals)
            high_id = approvals[-1]["approval_id"] if approvals else ""
        window = [item for item in approvals if position < item["approval_id"] <= high_id]
        selected = window[:page_size]
        has_more = len(window) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "approvals",
                    "tenant_id": principal.tenant_id,
                    "parent_id": run_id,
                    "high_id": high_id,
                    "position": selected[-1]["approval_id"],
                    "high_watermark": high_watermark,
                    "limit": page_size,
                }
            )
            if has_more
            else None
        )
        return JSONResponse(
            content=_validated(
                ApprovalPage,
                {
                    "items": selected,
                    "page": {
                        "next_cursor": next_cursor,
                        "has_more": has_more,
                        "high_watermark": high_watermark,
                    },
                },
            )
        )

    @application.post("/v1/runs/{run_id}/approvals/{approval_id}:decide")
    async def decide_approval(
        run_id: str,
        approval_id: str,
        body: DecideApprovalRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> JSONResponse:
        _authorize_operation(principal, "decideApproval")
        payload = body.model_dump(mode="json")
        expected_version = parse_revision_etag(if_match, field="evidence_version")
        if expected_version != int(payload["evidence_version"]):
            raise APIProblem(
                412,
                "stale_revision",
                "Approval evidence version mismatch",
                "If-Match must match the request evidence_version.",
            )

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            run = service._owned(await state.get_run(run_id), principal)
            approval = service._owned(await state.get_approval(run_id, approval_id), principal)
            _require_human_approval(principal)
            if approval["status"] != "OPEN" or run["status"] != "WAITING_APPROVAL":
                raise APIProblem(
                    409,
                    "approval_not_open",
                    "Approval is not open",
                    "Only the current OPEN approval can be decided.",
                )
            if int(approval["evidence_version"]) != expected_version:
                raise APIProblem(
                    412,
                    "stale_revision",
                    "Approval changed",
                    "Refresh the Approval and retry against its evidence_version.",
                    extras={"current_evidence_version": approval["evidence_version"]},
                )
            if _approval_expires_at(approval) <= utcnow():
                await state.update_approval(
                    run_id,
                    approval_id,
                    {
                        "status": "EXPIRED",
                        "evidence_version": int(approval["evidence_version"]) + 1,
                    },
                )
                raise APIProblem(
                    409,
                    "approval_expired",
                    "Approval is expired",
                    "The production approval expired before a decision was recorded.",
                )
            decision = payload["decision"]
            status = {
                "APPROVE": "APPROVED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "REJECT": "REJECTED",
            }[decision]
            await state.update_approval(
                run_id,
                approval_id,
                {
                    "status": status,
                    "decision_reason": payload["reason"],
                    "evidence_version": int(approval["evidence_version"]) + 1,
                },
            )
            command = await state.create_command(
                {
                    "tenant_id": principal.tenant_id,
                    "run_id": run_id,
                    "type": decision,
                    "status": "SUCCEEDED",
                    "submitted_at": iso_now(),
                    "completed_at": iso_now(),
                    "resulting_run_revision": None,
                    "problem": None,
                    "links": {"self": "", "run": f"/v1/runs/{run_id}"},
                }
            )
            await state.update_command(
                command["command_id"],
                {
                    "links": {
                        "self": f"/v1/commands/{command['command_id']}",
                        "run": f"/v1/runs/{run_id}",
                    }
                },
            )
            command = await state.get_command(command["command_id"])
            assert command is not None

            if decision == "APPROVE":
                candidate = run.get("pending_model_candidate")
                if candidate:
                    model = await state.create_model(
                        {
                            **candidate,
                            "tenant_id": principal.tenant_id,
                            "created_at": iso_now(),
                        }
                    )
                    outputs = await state.list_outputs(run_id)
                    await state.set_result(
                        run_id,
                        {
                            "result_manifest_id": state.new_id("result"),
                            "run_id": run_id,
                            "outcome": "SUCCEEDED",
                            "model_disposition": "ELIGIBLE_MODEL_AVAILABLE",
                            "summary": "Production deployment approval was granted.",
                            "backend_id": run.get("backend_id"),
                            "backend_version": run.get("backend_version"),
                            "engine_version": run.get("method_version"),
                            "output_refs": [service._output_ref(output) for output in outputs],
                            "partial": False,
                            "eligible_model": {
                                "model_id": model["model_id"],
                                "href": f"/v1/models/{model['model_id']}",
                            },
                            "reason": None,
                            "completed_at": iso_now(),
                        },
                    )
                updated = await state.update_run(
                    run_id,
                    {
                        "phase": "PACKAGE",
                        "status": "TERMINAL",
                        "outcome": "SUCCEEDED",
                        "execution_step": "COMPLETED",
                        "progress": service._progress(100, "COMPLETED", "Run completed"),
                        "stages": _completed_stages(run["stages"]),
                        "blocking": {"decision_packet_ids": [], "approval_ids": []},
                        "available_actions": [],
                        "updated_at": iso_now(),
                    },
                    expected_revision=run["run_revision"],
                    bump_revision=True,
                )
                updated = await service._emit(
                    updated,
                    "run.completed.v1",
                    {"outcome": "SUCCEEDED", "result_href": f"/v1/runs/{run_id}/result"},
                )
                await state.update_command(
                    command["command_id"],
                    {"resulting_run_revision": updated["run_revision"]},
                )
            else:
                await state.set_result(
                    run_id,
                    {
                        "result_manifest_id": state.new_id("result"),
                        "run_id": run_id,
                        "outcome": "SUCCEEDED",
                        "model_disposition": "NO_ELIGIBLE_MODEL",
                        "summary": "Production deployment approval was not granted.",
                        "backend_id": run.get("backend_id"),
                        "backend_version": run.get("backend_version"),
                        "engine_version": run.get("method_version"),
                        "output_refs": [
                            service._output_ref(output)
                            for output in await state.list_outputs(run_id)
                        ],
                        "partial": False,
                        "eligible_model": None,
                        "reason": {
                            "code": "PRODUCTION_APPROVAL_NOT_GRANTED",
                            "message": payload["reason"],
                            "retriable": decision == "REQUEST_CHANGES",
                            "failed_gates": [status],
                            "evidence_refs": approval.get("evidence_refs", []),
                            "remediation": ["Create a new Run after resolving approval feedback."],
                        },
                        "completed_at": iso_now(),
                    },
                )
                updated = await state.update_run(
                    run_id,
                    {
                        "phase": "PACKAGE",
                        "status": "TERMINAL",
                        "outcome": "SUCCEEDED",
                        "execution_step": "COMPLETED",
                        "progress": service._progress(100, "COMPLETED", "Run completed"),
                        "stages": _completed_stages(run["stages"]),
                        "blocking": {"decision_packet_ids": [], "approval_ids": []},
                        "available_actions": [],
                        "updated_at": iso_now(),
                    },
                    expected_revision=run["run_revision"],
                    bump_revision=True,
                )
                updated = await service._emit(
                    updated,
                    "run.completed.v1",
                    {"outcome": "SUCCEEDED", "result_href": f"/v1/runs/{run_id}/result"},
                )
                await state.update_command(
                    command["command_id"],
                    {"resulting_run_revision": updated["run_revision"]},
                )
            command = await state.get_command(command["command_id"])
            assert command is not None
            return 202, _validated(CommandReceipt, command), {"Retry-After": "1"}

        return await _idempotent(
            state=state,
            operation_id="decideApproval",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.get("/v1/models/{model_id}")
    async def get_model(model_id: str, principal: PrincipalDependency) -> JSONResponse:
        _authorize_operation(principal, "getModelCandidate")
        model = service._owned(await state.get_model(model_id), principal)
        model_run = service._owned(await state.get_run(model["run_id"]), principal)
        version = service._owned(
            await state.get_dataset_version(model_run["dataset_version_id"]), principal
        )
        if version.get("status") == "DELETED":
            raise _not_found("Model candidate")
        return JSONResponse(content=_validated(ModelCandidate, _public_model(model)))

    @application.post("/v1/webhook-endpoints", status_code=201)
    async def create_webhook_endpoint(
        body: CreateWebhookEndpointRequest,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "createWebhookEndpoint")
        payload = body.model_dump(mode="json")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            secret = _new_webhook_secret()
            endpoint = await state.create_webhook_endpoint(
                {
                    "tenant_id": principal.tenant_id,
                    "url": payload["url"],
                    "event_types": payload["event_types"],
                    "description": payload.get("description"),
                    "status": "ACTIVE",
                    "status_reason": None,
                    "paused_at": None,
                    "signature_version": "v1",
                    "replay_window_seconds": 300,
                    "signing_secret": secret,
                    "previous_signing_secret": None,
                    "previous_secret_valid_until": None,
                    "created_at": iso_now(),
                }
            )
            public = {**_public_webhook_endpoint(endpoint), "signing_secret": secret}
            return 201, _validated(WebhookEndpointCreated, public), {}

        return await _idempotent(
            state=state,
            operation_id="createWebhookEndpoint",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=payload,
            execute=execute,
        )

    @application.get("/v1/webhook-endpoints")
    async def list_webhook_endpoints(
        principal: PrincipalDependency,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listWebhookEndpoints")
        endpoints = [
            _public_webhook_endpoint(item)
            for item in await state.list_webhook_endpoints(principal.tenant_id)
        ]
        endpoints.sort(key=lambda item: item["webhook_endpoint_id"])
        if cursor:
            if limit is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first Webhook page, send only cursor.",
                )
            cursor_data = _validate_cursor(cursor, kind="webhook-endpoints", principal=principal)
            high_id = cursor_data.get("high_id")
            position = cursor_data.get("position")
            page_size = int(cursor_data.get("limit", 0))
            high_watermark = int(cursor_data.get("high_watermark", -1))
            if (
                not isinstance(high_id, str)
                or not isinstance(position, str)
                or page_size < 1
                or high_watermark < 0
            ):
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
        else:
            position = ""
            page_size = limit or 50
            high_watermark = len(endpoints)
            high_id = endpoints[-1]["webhook_endpoint_id"] if endpoints else ""
        window = [item for item in endpoints if position < item["webhook_endpoint_id"] <= high_id]
        selected = window[:page_size]
        has_more = len(window) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "webhook-endpoints",
                    "tenant_id": principal.tenant_id,
                    "high_id": high_id,
                    "position": selected[-1]["webhook_endpoint_id"],
                    "high_watermark": high_watermark,
                    "limit": page_size,
                }
            )
            if has_more
            else None
        )
        return JSONResponse(
            content={
                "items": [_validated(WebhookEndpoint, item) for item in selected],
                "page": {
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "high_watermark": high_watermark,
                },
            }
        )

    @application.get("/v1/webhook-endpoints/{webhook_endpoint_id}")
    async def get_webhook_endpoint(
        webhook_endpoint_id: str, principal: PrincipalDependency
    ) -> JSONResponse:
        _authorize_operation(principal, "getWebhookEndpoint")
        endpoint = service._owned(await state.get_webhook_endpoint(webhook_endpoint_id), principal)
        return JSONResponse(content=_validated(WebhookEndpoint, _public_webhook_endpoint(endpoint)))

    @application.delete("/v1/webhook-endpoints/{webhook_endpoint_id}", status_code=204)
    async def delete_webhook_endpoint(
        webhook_endpoint_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "deleteWebhookEndpoint")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            endpoint = service._owned(
                await state.get_webhook_endpoint(webhook_endpoint_id), principal
            )
            if endpoint["status"] != "DISABLED":
                await state.update_webhook_endpoint(
                    webhook_endpoint_id,
                    {
                        "status": "DISABLED",
                        "status_reason": "Deleted by API request.",
                        "paused_at": iso_now(),
                    },
                )
            return 204, {}, {}

        return await _idempotent(
            state=state,
            operation_id="deleteWebhookEndpoint",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.post("/v1/webhook-endpoints/{webhook_endpoint_id}:rotate-secret", status_code=201)
    async def rotate_webhook_endpoint_secret(
        webhook_endpoint_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "rotateWebhookEndpointSecret")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            endpoint = service._owned(
                await state.get_webhook_endpoint(webhook_endpoint_id), principal
            )
            if endpoint["status"] == "DISABLED":
                raise APIProblem(
                    409,
                    "webhook_disabled",
                    "Webhook endpoint is disabled",
                    "Enable or recreate the endpoint before rotating its secret.",
                )
            new_secret = _new_webhook_secret()
            valid_until = (utcnow() + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
            await state.update_webhook_endpoint(
                webhook_endpoint_id,
                {
                    "signing_secret": new_secret,
                    "previous_signing_secret": endpoint.get("signing_secret"),
                    "previous_secret_valid_until": valid_until,
                },
            )
            return (
                201,
                _validated(
                    WebhookSecretRotated,
                    {
                        "webhook_endpoint_id": webhook_endpoint_id,
                        "signing_secret": new_secret,
                        "previous_secret_valid_until": valid_until,
                        "created_at": iso_now(),
                    },
                ),
                {},
            )

        return await _idempotent(
            state=state,
            operation_id="rotateWebhookEndpointSecret",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.post("/v1/webhook-endpoints/{webhook_endpoint_id}:enable")
    async def enable_webhook_endpoint(
        webhook_endpoint_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "enableWebhookEndpoint")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            endpoint = service._owned(
                await state.get_webhook_endpoint(webhook_endpoint_id), principal
            )
            if endpoint["status"] != "ACTIVE":
                endpoint = await state.update_webhook_endpoint(
                    webhook_endpoint_id,
                    {"status": "ACTIVE", "status_reason": None, "paused_at": None},
                )
            return 200, _validated(WebhookEndpoint, _public_webhook_endpoint(endpoint)), {}

        return await _idempotent(
            state=state,
            operation_id="enableWebhookEndpoint",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.get("/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries")
    async def list_webhook_deliveries(
        webhook_endpoint_id: str,
        principal: PrincipalDependency,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        status: str | None = None,
    ) -> JSONResponse:
        _authorize_operation(principal, "listWebhookDeliveries")
        endpoint = service._owned(await state.get_webhook_endpoint(webhook_endpoint_id), principal)
        deliveries = [
            _public_webhook_delivery(item)
            for item in await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
            if item.get("tenant_id") == principal.tenant_id
        ]
        statuses = _csv_values(status)
        if statuses is not None:
            allowed = {"PENDING", "DELIVERING", "SUCCEEDED", "RETRYING", "EXHAUSTED"}
            _validate_filter(statuses, allowed, name="Webhook delivery status")
            deliveries = [item for item in deliveries if item["status"] in set(statuses)]
        deliveries.sort(key=lambda item: item["delivery_id"])
        if cursor:
            if limit is not None or status is not None:
                raise APIProblem(
                    400,
                    "invalid_cursor",
                    "Invalid cursor",
                    "After the first Delivery page, send only cursor.",
                )
            cursor_data = _validate_cursor(
                cursor,
                kind="webhook-deliveries",
                principal=principal,
                parent_id=webhook_endpoint_id,
            )
            high_id = cursor_data.get("high_id")
            position = cursor_data.get("position")
            page_size = int(cursor_data.get("limit", 0))
            high_watermark = int(cursor_data.get("high_watermark", -1))
            statuses = cursor_data.get("statuses")
            if (
                not isinstance(high_id, str)
                or not isinstance(position, str)
                or page_size < 1
                or high_watermark < 0
            ):
                raise APIProblem(
                    400, "invalid_cursor", "Invalid cursor", "Cursor payload is invalid."
                )
            if statuses:
                deliveries = [item for item in deliveries if item["status"] in set(statuses)]
        else:
            position = ""
            page_size = limit or 50
            high_watermark = len(deliveries)
            high_id = deliveries[-1]["delivery_id"] if deliveries else ""
        window = [item for item in deliveries if position < item["delivery_id"] <= high_id]
        selected = window[:page_size]
        has_more = len(window) > page_size
        next_cursor = (
            encode_cursor(
                {
                    "kind": "webhook-deliveries",
                    "tenant_id": principal.tenant_id,
                    "parent_id": webhook_endpoint_id,
                    "high_id": high_id,
                    "position": selected[-1]["delivery_id"],
                    "high_watermark": high_watermark,
                    "limit": page_size,
                    "statuses": statuses,
                }
            )
            if has_more
            else None
        )
        return JSONResponse(
            content=_validated(
                WebhookDeliveryPage,
                {
                    "items": selected,
                    "page": {
                        "next_cursor": next_cursor,
                        "has_more": has_more,
                        "high_watermark": high_watermark,
                    },
                },
            )
        )

    @application.get("/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}")
    async def get_webhook_delivery(
        webhook_endpoint_id: str,
        delivery_id: str,
        principal: PrincipalDependency,
    ) -> JSONResponse:
        _authorize_operation(principal, "getWebhookDelivery")
        endpoint = service._owned(await state.get_webhook_endpoint(webhook_endpoint_id), principal)
        delivery = service._owned(
            await state.get_webhook_delivery(endpoint["webhook_endpoint_id"], delivery_id),
            principal,
        )
        return JSONResponse(content=_validated(WebhookDelivery, _public_webhook_delivery(delivery)))

    @application.post(
        "/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}:redeliver",
        status_code=202,
    )
    async def redeliver_webhook_delivery(
        webhook_endpoint_id: str,
        delivery_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "redeliverWebhookDelivery")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            endpoint = service._owned(
                await state.get_webhook_endpoint(webhook_endpoint_id), principal
            )
            if endpoint["status"] != "ACTIVE":
                raise APIProblem(
                    409,
                    "webhook_not_active",
                    "Webhook endpoint is not active",
                    "Enable the endpoint before requesting redelivery.",
                )
            delivery = service._owned(
                await state.get_webhook_delivery(endpoint["webhook_endpoint_id"], delivery_id),
                principal,
            )
            if delivery["status"] not in {"PENDING", "RETRYING", "EXHAUSTED"}:
                raise APIProblem(
                    409,
                    "delivery_not_redeliverable",
                    "Delivery is not redeliverable",
                    "Only pending, retrying, or exhausted deliveries can be redelivered.",
                )
            await state.update_webhook_delivery(
                webhook_endpoint_id,
                delivery_id,
                {
                    "status": "PENDING",
                    "attempt_count": int(delivery["attempt_count"]) + 1,
                    "next_attempt_at": iso_now(),
                    "last_problem": None,
                },
            )
            redelivery_id = state.new_id("redelivery")
            return (
                202,
                _validated(
                    WebhookRedeliveryReceipt,
                    {
                        "redelivery_id": redelivery_id,
                        "delivery_id": delivery_id,
                        "status": "ACCEPTED",
                        "submitted_at": iso_now(),
                        "delivery_href": (
                            f"/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}"
                        ),
                    },
                ),
                {"Retry-After": "1"},
            )

        return await _idempotent(
            state=state,
            operation_id="redeliverWebhookDelivery",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.delete("/v1/datasets/{dataset_id}")
    async def delete_dataset(
        dataset_id: str,
        request: Request,
        principal: PrincipalDependency,
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        _authorize_operation(principal, "deleteDataset")

        async def execute() -> tuple[int, dict[str, Any], dict[str, str]]:
            dataset = service._owned(await state.get_dataset(dataset_id), principal)
            versions = [
                item
                for item in await state.list_dataset_versions(dataset_id)
                if item.get("tenant_id") == principal.tenant_id
            ]
            version_ids = {item["dataset_version_id"] for item in versions}
            runs = [
                item
                for item in await state.list_runs()
                if item.get("tenant_id") == principal.tenant_id
                and item.get("dataset_version_id") in version_ids
            ]
            affected_run_ids = sorted(item["run_id"] for item in runs)
            now = iso_now()
            deletion = await state.create_deletion(
                {
                    "tenant_id": principal.tenant_id,
                    "dataset_id": dataset["dataset_id"],
                    "status": "RUNNING",
                    "affected_run_ids": affected_run_ids,
                    "stores": [
                        {"name": "metadata", "status": "DELETING", "verified_at": None},
                        {"name": "object_store", "status": "DELETING", "verified_at": None},
                        {"name": "model_registry", "status": "DELETING", "verified_at": None},
                    ],
                    "created_at": now,
                    "completed_at": None,
                }
            )
            try:
                # Revoke control-plane access before deleting bytes so any existing ticket or
                # concurrent request cannot read a resource after deletion is accepted.
                await state.update_dataset(dataset_id, {"deleted_at": now})
                for version in versions:
                    await state.update_dataset_version(
                        version["dataset_version_id"],
                        {"status": "DELETED", "updated_at": now},
                    )
                artifacts = []
                for run in runs:
                    artifacts.extend(await state.list_artifacts(run_id=run["run_id"]))
                for artifact in artifacts:
                    await state.update_artifact(artifact["artifact_id"], {"state": "DELETING"})
                for run in runs:
                    if run.get("status") != "TERMINAL":
                        await service.cancel(principal, run["run_id"])

                for version in versions:
                    await blob_store.delete_dataset_version(
                        tenant_id=principal.tenant_id,
                        dataset_version_id=version["dataset_version_id"],
                    )
                for artifact in artifacts:
                    await blob_store.delete_key(str(artifact.get("blob_key", "")))
                    await state.update_artifact(artifact["artifact_id"], {"state": "DELETED"})

                completed_at = iso_now()
                deletion = await state.update_deletion(
                    deletion["deletion_id"],
                    {
                        "status": "COMPLETED",
                        "stores": [
                            {"name": "metadata", "status": "DELETED", "verified_at": completed_at},
                            {
                                "name": "object_store",
                                "status": "DELETED",
                                "verified_at": completed_at,
                            },
                            {
                                "name": "model_registry",
                                "status": "INACCESSIBLE",
                                "verified_at": completed_at,
                            },
                        ],
                        "completed_at": completed_at,
                    },
                )
            except Exception:
                failed_at = iso_now()
                await state.update_deletion(
                    deletion["deletion_id"],
                    {
                        "status": "FAILED",
                        "stores": [
                            {"name": "metadata", "status": "FAILED", "verified_at": failed_at},
                            {"name": "object_store", "status": "FAILED", "verified_at": failed_at},
                            {
                                "name": "model_registry",
                                "status": "FAILED",
                                "verified_at": failed_at,
                            },
                        ],
                        "completed_at": failed_at,
                    },
                )
                raise
            return 202, _validated(DeletionJob, _public_deletion(deletion)), {"Retry-After": "1"}

        return await _idempotent(
            state=state,
            operation_id="deleteDataset",
            principal=principal,
            key=idempotency_key,
            request=request,
            body=None,
            execute=execute,
        )

    @application.get("/v1/deletions/{deletion_id}")
    async def get_deletion(deletion_id: str, principal: PrincipalDependency) -> JSONResponse:
        _authorize_operation(principal, "getDeletionJob")
        deletion = service._owned(await state.get_deletion(deletion_id), principal)
        return JSONResponse(content=_validated(DeletionJob, _public_deletion(deletion)))

    return application


app = create_app()
