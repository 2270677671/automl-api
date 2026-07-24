"""Public Pydantic v2 models for the managed AutoML API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)

from .version import __version__


Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
RevisionETag = Annotated[str, StringConstraints(pattern=r'^"[1-9][0-9]*"$')]
PositiveRevision = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Percent = Annotated[float, Field(ge=0, le=100)]


class PublicModel(BaseModel):
    """Base for public response models; unknown implementation fields are dropped."""

    model_config = ConfigDict(use_enum_values=True)


class RequestModel(PublicModel):
    """Request bodies explicitly closed by the OpenAPI contract."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class DatasetMediaType(str, Enum):
    CSV = "text/csv"
    PARQUET = "application/vnd.apache.parquet"


class DatasetStatus(str, Enum):
    UPLOADING = "UPLOADING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    REJECTED = "REJECTED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class TaskType(str, Enum):
    BINARY_CLASSIFICATION = "BINARY_CLASSIFICATION"
    REGRESSION = "REGRESSION"


class RunPhase(str, Enum):
    INGEST = "INGEST"
    PROFILE = "PROFILE"
    PLAN = "PLAN"
    TRAIN = "TRAIN"
    EVALUATE = "EVALUATE"
    PACKAGE = "PACKAGE"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    RETRYING = "RETRYING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    TERMINAL = "TERMINAL"


class RunOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AvailableAction(str, Enum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    ANSWER = "ANSWER"
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


class OutputType(str, Enum):
    DATA_QUALITY_REPORT = "DATA_QUALITY_REPORT"
    TASK_SPEC = "TASK_SPEC"
    SPLIT_MANIFEST = "SPLIT_MANIFEST"
    BASELINE_RESULT = "BASELINE_RESULT"
    COST_ESTIMATE = "COST_ESTIMATE"
    TRIAL_RESULT = "TRIAL_RESULT"
    EVALUATION_REPORT = "EVALUATION_REPORT"
    LOG_SUMMARY = "LOG_SUMMARY"
    MODEL_CARD = "MODEL_CARD"
    RUN_REPORT = "RUN_REPORT"
    FAILURE_REPORT = "FAILURE_REPORT"


class OutputState(str, Enum):
    PARTIAL = "PARTIAL"
    FINAL = "FINAL"


class ExperimentStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PRUNED = "PRUNED"
    CANCELED = "CANCELED"


class DecisionPacketStatus(str, Enum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class DecisionResolutionPolicy(str, Enum):
    """Who may resolve a DecisionPacket exposed to an external Agent platform."""

    AGENT_ALLOWED = "AGENT_ALLOWED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class QuestionSelectionMode(str, Enum):
    SINGLE = "SINGLE"
    MULTIPLE = "MULTIPLE"
    FREEFORM = "FREEFORM"


class QuestionOptionRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CommandType(str, Enum):
    ANSWER = "ANSWER"
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


class CommandStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ArtifactState(str, Enum):
    COMMITTED = "COMMITTED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class ApprovalKind(str, Enum):
    PII_USE = "PII_USE"
    HIGH_IMPACT = "HIGH_IMPACT"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"
    PRODUCTION_DEPLOY = "PRODUCTION_DEPLOY"


class ApprovalStatus(str, Enum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class ApprovalDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


class WebhookEndpointStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED_DELIVERY_FAILURES = "PAUSED_DELIVERY_FAILURES"
    DISABLED = "DISABLED"


class WebhookDeliveryStatus(str, Enum):
    PENDING = "PENDING"
    DELIVERING = "DELIVERING"
    SUCCEEDED = "SUCCEEDED"
    RETRYING = "RETRYING"
    EXHAUSTED = "EXHAUSTED"


class DeletionJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeletionStoreStatus(str, Enum):
    PENDING = "PENDING"
    DELETING = "DELETING"
    INACCESSIBLE = "INACCESSIBLE"
    DELETED = "DELETED"
    RETAINED_UNTIL = "RETAINED_UNTIL"
    FAILED = "FAILED"


class Recovery(PublicModel):
    action: str | None = None
    href: str | None = None


class ProblemError(PublicModel):
    code: str
    message: str
    field: str | None = None


class Problem(PublicModel):
    type: str
    title: str
    status: Annotated[int, Field(ge=400, le=599)]
    code: str
    detail: str
    retriable: bool
    correlation_id: str
    run_id: str | None = None
    current_run_revision: PositiveRevision | None = None
    retained_from_seq: NonNegativeInt | None = None
    recovery: Recovery | None = None
    errors: list[ProblemError] = Field(default_factory=list)


class PageMeta(PublicModel):
    has_more: bool
    high_watermark: NonNegativeInt
    next_cursor: str | None = None


class IssueSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKING = "BLOCKING"


class Issue(PublicModel):
    code: str
    severity: IssueSeverity
    message: str
    remediation: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class ConfidenceInterval(PublicModel):
    level: Annotated[float, Field(gt=0, lt=1)]
    lower: float
    upper: float
    method: str


class MetricDirection(str, Enum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


class MetricValue(PublicModel):
    name: str
    value: float
    direction: MetricDirection
    confidence_interval: ConfidenceInterval | None = None


class Lineage(PublicModel):
    dataset_version_id: str
    policy_version: str
    method_version: str
    parent_refs: list[str]
    evidence_refs: list[str]
    task_spec_output_id: str | None = None
    split_manifest_output_id: str | None = None


class OutputSummarySeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class OutputSummary(PublicModel):
    code: str
    message: str
    severity: OutputSummarySeverity | None = None


class OutputRef(PublicModel):
    output_id: str
    type: OutputType
    state: OutputState
    href: str


class ArtifactRef(PublicModel):
    artifact_id: str
    kind: str
    media_type: str
    size_bytes: NonNegativeInt
    sha256: Sha256
    href: str


class CreateDatasetRequest(RequestModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    media_type: DatasetMediaType
    size_bytes: Annotated[int, Field(ge=1)]
    retention_days: Annotated[int, Field(ge=1, le=3650)] | None = None


class UploadPart(PublicModel):
    part_number: Annotated[int, Field(ge=1)]
    url: AnyUrl
    expires_at: datetime
    required_headers: dict[str, str] = Field(default_factory=dict)


class DatasetUploadSession(PublicModel):
    dataset_id: str
    dataset_version_id: str
    status: DatasetStatus
    upload_id: str
    expires_at: datetime
    parts: list[UploadPart]


class SignUploadPartsRequest(RequestModel):
    upload_id: str
    part_numbers: set[Annotated[int, Field(ge=1, le=10000)]] = Field(min_length=1, max_length=1000)


class UploadPartsResponse(PublicModel):
    upload_id: str
    expires_at: datetime
    parts: list[UploadPart]


class FinalizeDatasetPart(RequestModel):
    part_number: Annotated[int, Field(ge=1)]
    etag: str


class FinalizeDatasetRequest(RequestModel):
    upload_id: str
    parts: Annotated[list[FinalizeDatasetPart], Field(min_length=1)]
    sha256: Sha256


class DatasetVersion(PublicModel):
    dataset_id: str
    dataset_version_id: str
    status: DatasetStatus
    revision: PositiveRevision
    created_at: datetime
    updated_at: datetime
    media_type: str | None = None
    size_bytes: NonNegativeInt | None = None
    sha256: Sha256 | None = None
    validation_issues: list[Issue] = Field(default_factory=list)


class Objective(RequestModel):
    backend_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$"),
    ] = "sklearn"
    target_column: str | None = None
    task_type: TaskType | None = None
    positive_class: str | int | bool | None = None
    primary_metric: str | None = None
    iid_confirmed: bool | None = None
    business_context: Annotated[str, StringConstraints(max_length=2000)] | None = None


class AutonomyPolicy(RequestModel):
    mode: Literal["GUIDED"]
    production_deploy: Literal["REQUIRE_APPROVAL", "DISABLED"]


class RunPolicy(RequestModel):
    allow_pii: bool
    allow_external_llm: bool
    risk_tier: Literal["STANDARD", "HIGH_IMPACT"] = "STANDARD"


class RunBudget(RequestModel):
    max_trials: Annotated[int, Field(ge=1, le=10000)]
    max_compute_credits: NonNegativeFloat
    max_wall_time_seconds: Annotated[int, Field(ge=60)]
    max_llm_tokens: NonNegativeInt = Field(
        description=(
            "Reserved v1 compatibility field. The AutoML execution backend does not consume "
            "LLM tokens; an external Agent platform must enforce its own LLM budget."
        )
    )


class CreateRunRequest(RequestModel):
    dataset_version_id: str
    objective: Objective
    autonomy: AutonomyPolicy
    policy: RunPolicy
    budget: RunBudget
    webhook_endpoint_ids: set[str] = Field(default_factory=set)


class ContractVersions(PublicModel):
    event_schema: str
    output_schema: str
    policy_version: str
    tool_versions: dict[str, str] = Field(default_factory=dict)


class CurrentStep(PublicModel):
    code: str
    title: str
    message: str | None = None


class Progress(PublicModel):
    plan_version: PositiveRevision
    percent: Percent
    estimate_revision: PositiveRevision
    completed_steps: NonNegativeInt
    total_steps: Annotated[int, Field(ge=1)]
    current_step: CurrentStep
    eta_seconds: NonNegativeInt | None = None


class StageSnapshot(PublicModel):
    phase: RunPhase
    status: StageStatus
    progress_percent: Percent
    latest_output_refs: list[OutputRef]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    message: str | None = None


class BlockingSummary(PublicModel):
    decision_packet_ids: list[str]
    approval_ids: list[str]


class UsageLimit(PublicModel):
    used: NonNegativeFloat
    limit: NonNegativeFloat


class BudgetUsage(PublicModel):
    compute_credits: UsageLimit
    trials: UsageLimit
    wall_time_seconds: UsageLimit
    llm_tokens: UsageLimit


class RunSnapshot(PublicModel):
    api_version: Literal["v1"]
    run_id: str
    dataset_version_id: str
    phase: RunPhase
    status: RunStatus
    outcome: RunOutcome | None
    plan_version: PositiveRevision
    run_revision: PositiveRevision
    snapshot_seq: NonNegativeInt
    retained_from_seq: NonNegativeInt
    contract_versions: ContractVersions
    progress: Progress
    stages: list[StageSnapshot]
    blocking: BlockingSummary
    latest_output_refs: list[OutputRef]
    available_actions: list[AvailableAction]
    budget_usage: BudgetUsage
    created_at: datetime
    updated_at: datetime
    links: dict[str, str]

    @model_validator(mode="after")
    def validate_terminal_invariants(self) -> RunSnapshot:
        terminal = self.status == RunStatus.TERMINAL
        if terminal != (self.outcome is not None):
            raise ValueError("outcome must be set exactly when status is TERMINAL")
        if terminal and (
            self.available_actions
            or self.blocking.decision_packet_ids
            or self.blocking.approval_ids
        ):
            raise ValueError("a terminal Run cannot expose actions or blockers")
        return self


class RunPage(PublicModel):
    items: list[RunSnapshot]
    page: PageMeta


class BackendCapabilities(PublicModel):
    task_types: list[TaskType]
    media_types: list[DatasetMediaType]
    supports_categorical_features: bool
    supports_missing_values: bool
    supports_probability_predictions: bool
    supports_cross_validation: bool
    supports_sealed_holdout: bool
    supports_cpu: bool
    supports_gpu: bool
    limits: dict[str, int] = Field(default_factory=dict)
    runtime_requirements: list[str] = Field(default_factory=list)
    required_attributions: list[str] = Field(default_factory=list)


class BackendArtifactContract(PublicModel):
    kind: str
    media_type: str
    serialization: str


class BackendDescriptor(PublicModel):
    backend_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$"),
    ]
    display_name: str
    engine_version: str
    backend_version: str | None
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    installed: bool
    available: bool
    optional_dependency: str | None
    unavailable_reason: str | None
    capabilities: BackendCapabilities
    artifact: BackendArtifactContract
    deterministic: bool
    production_eligible: bool

    @model_validator(mode="after")
    def validate_availability_status(self) -> BackendDescriptor:
        if (self.status == "AVAILABLE") != self.available:
            raise ValueError("status and available must describe the same backend state")
        if self.available and not self.installed:
            raise ValueError("an available backend must be installed")
        if self.available and self.unavailable_reason is not None:
            raise ValueError("an available backend cannot have unavailable_reason")
        if not self.available and not self.unavailable_reason:
            raise ValueError("an unavailable backend must explain unavailable_reason")
        return self


class AgentInterfaceManifest(PublicModel):
    schema_version: Literal["1.0"] = "1.0"
    service_version: str = __version__
    api_version: Literal["v1"] = "v1"
    profile_id: str = "local-durable-tabular-v1"
    service_role: Literal["AUTOML_EXECUTION_BACKEND"] = "AUTOML_EXECUTION_BACKEND"
    planner_location: Literal["EXTERNAL_AGENT_PLATFORM"] = "EXTERNAL_AGENT_PLATFORM"
    internal_llm_calls: Literal[False] = False
    llm_budget_owner: Literal["EXTERNAL_AGENT_PLATFORM"] = "EXTERNAL_AGENT_PLATFORM"
    max_llm_tokens_consumed: Literal[False] = False
    credentials_must_remain_in_platform: Literal[True] = True
    production_external_llm_safe: bool = False
    openapi_href: str
    canonical_openapi_href: str | None = None
    agent_tools_openapi_href: str | None = None
    agent_tools_openapi_sha256: Sha256 | None = None
    python_sdk_compatible_versions: str | None = None
    context_path_template: str
    actions_path_template: str
    canonical_operation_ids: list[str]
    active_operation_ids: list[str] = Field(default_factory=list)
    operation_scopes: dict[str, str] = Field(default_factory=dict)
    runtime_limits: dict[str, int | float] = Field(default_factory=dict)
    default_backend_id: str = "sklearn"
    backends: list[BackendDescriptor] = Field(default_factory=list)
    supported_capabilities: list[str] = Field(default_factory=list)
    unsupported_capabilities: list[str] = Field(default_factory=list)
    supported_task_types: list[TaskType]
    supported_media_types: list[DatasetMediaType]


class AgentIfMatch(PublicModel):
    header: Literal["If-Match"] = "If-Match"
    scope: Literal["RUN_REVISION", "WAIT_SET_REVISION"]
    value: RevisionETag


class AgentOperationRef(PublicModel):
    action: AvailableAction
    operation_id: str
    method: Literal["POST"] = "POST"
    href: str
    idempotency_key_required: bool
    if_match: AgentIfMatch | None = None
    request_schema_ref: str | None = None


class AgentActionList(PublicModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    run_revision: PositiveRevision
    items: list[AgentOperationRef]


class DataQualityReportPayload(PublicModel):
    kind: Literal["DATA_QUALITY_REPORT"]
    row_count: NonNegativeInt
    column_count: NonNegativeInt
    quality_score: Percent
    issues: list[Issue]


class SplitStrategy(str, Enum):
    STRATIFIED_HOLDOUT = "STRATIFIED_HOLDOUT"
    RANDOM_HOLDOUT = "RANDOM_HOLDOUT"
    GROUP_HOLDOUT = "GROUP_HOLDOUT"
    TIME_HOLDOUT = "TIME_HOLDOUT"
    NESTED_CV = "NESTED_CV"


class ConfirmedBy(str, Enum):
    POLICY = "POLICY"
    USER = "USER"
    POLICY_AND_LLM = "POLICY_AND_LLM"


class TaskSpecPayload(PublicModel):
    kind: Literal["TASK_SPEC"]
    task_type: TaskType
    target_column_id: str
    primary_metric: str
    split_strategy: SplitStrategy
    confidence: Annotated[float, Field(ge=0, le=1)]
    assumptions: list[str]
    backend_id: str | None = None
    engine_version: str | None = None
    positive_class: str | int | bool | None = None
    guardrail_metrics: list[str] = Field(default_factory=list)
    confirmed_by: ConfirmedBy | None = None


class SplitManifestPayload(PublicModel):
    kind: Literal["SPLIT_MANIFEST"]
    strategy: str
    train_rows: NonNegativeInt
    validation_rows: NonNegativeInt
    test_rows: NonNegativeInt
    leakage_checks: list[Issue]


class BaselineMetricSet(PublicModel):
    name: str
    metrics: list[MetricValue]
    compute_credits: NonNegativeFloat | None = None


class BaselineResultPayload(PublicModel):
    kind: Literal["BASELINE_RESULT"]
    baselines: list[BaselineMetricSet]


class EstimateRange(PublicModel):
    lower: NonNegativeFloat
    upper: NonNegativeFloat


class CostEstimatePayload(PublicModel):
    kind: Literal["COST_ESTIMATE"]
    compute_credits: EstimateRange
    duration_seconds: EstimateRange
    assumptions: list[str]


class TrialResultPayload(PublicModel):
    kind: Literal["TRIAL_RESULT"]
    experiment_id: str
    trial_number: NonNegativeInt
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "PRUNED"]
    model_family: str
    metrics: list[MetricValue]
    compute_credits: NonNegativeFloat
    backend_id: str | None = None
    engine_version: str | None = None
    normalized_config: dict[str, Any] | None = None
    failure_code: str | None = None


class EvaluationReportPayload(PublicModel):
    kind: Literal["EVALUATION_REPORT"]
    primary_metric: str
    baseline: MetricValue
    candidate: MetricValue
    paired_delta: MetricValue
    guardrails_passed: bool
    eligible_candidate: bool
    failed_gates: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class LogSummaryPayload(PublicModel):
    kind: Literal["LOG_SUMMARY"]
    level: Literal["INFO", "WARNING", "ERROR"]
    code: str
    message: str
    count: Annotated[int, Field(ge=1)]
    window_started_at: datetime
    window_ended_at: datetime


class ModelCardPayload(PublicModel):
    kind: Literal["MODEL_CARD"]
    model_id: str
    intended_use: str
    limitations: list[str]
    metrics: list[MetricValue]
    backend_id: str | None = None
    backend_version: str | None = None
    engine_version: str | None = None


class RunReportPayload(PublicModel):
    kind: Literal["RUN_REPORT"]
    summary: str
    recommendation: str
    evidence_refs: list[str]


class FailureReportPayload(PublicModel):
    kind: Literal["FAILURE_REPORT"]
    failure_code: str
    phase: RunPhase
    message: str
    retriable: bool
    partial_output_ids: list[str]
    remediation: list[str] = Field(default_factory=list)


class BaseOutputResource(PublicModel):
    output_id: str
    schema_version: str
    run_id: str
    run_revision: PositiveRevision
    created_seq: Annotated[int, Field(ge=1)]
    phase: RunPhase
    state: OutputState
    summary: OutputSummary
    lineage: Lineage
    artifact_refs: list[ArtifactRef]
    supersedes: str | None
    created_at: datetime


class DataQualityReportOutput(BaseOutputResource):
    type: Literal["DATA_QUALITY_REPORT"]
    payload: DataQualityReportPayload


class TaskSpecOutput(BaseOutputResource):
    type: Literal["TASK_SPEC"]
    payload: TaskSpecPayload


class SplitManifestOutput(BaseOutputResource):
    type: Literal["SPLIT_MANIFEST"]
    payload: SplitManifestPayload


class BaselineResultOutput(BaseOutputResource):
    type: Literal["BASELINE_RESULT"]
    payload: BaselineResultPayload


class CostEstimateOutput(BaseOutputResource):
    type: Literal["COST_ESTIMATE"]
    payload: CostEstimatePayload


class TrialResultOutput(BaseOutputResource):
    type: Literal["TRIAL_RESULT"]
    payload: TrialResultPayload


class EvaluationReportOutput(BaseOutputResource):
    type: Literal["EVALUATION_REPORT"]
    payload: EvaluationReportPayload


class LogSummaryOutput(BaseOutputResource):
    type: Literal["LOG_SUMMARY"]
    payload: LogSummaryPayload


class ModelCardOutput(BaseOutputResource):
    type: Literal["MODEL_CARD"]
    payload: ModelCardPayload


class RunReportOutput(BaseOutputResource):
    type: Literal["RUN_REPORT"]
    payload: RunReportPayload


class FailureReportOutput(BaseOutputResource):
    type: Literal["FAILURE_REPORT"]
    payload: FailureReportPayload


OutputResourceVariant = Annotated[
    DataQualityReportOutput
    | TaskSpecOutput
    | SplitManifestOutput
    | BaselineResultOutput
    | CostEstimateOutput
    | TrialResultOutput
    | EvaluationReportOutput
    | LogSummaryOutput
    | ModelCardOutput
    | RunReportOutput
    | FailureReportOutput,
    Field(discriminator="type"),
]


class OutputResource(RootModel[OutputResourceVariant]):
    """Any committed output, discriminated by its public ``type``."""


class OutputPage(PublicModel):
    items: list[OutputResource]
    page: PageMeta


class PhaseChangedPayload(PublicModel):
    phase: RunPhase
    status: RunStatus
    previous_phase: RunPhase | None = None


class ProgressUpdatedPayload(PublicModel):
    progress: Progress


class OutputCommittedPayload(PublicModel):
    output: OutputRef


class DecisionPacketRequestedPayload(PublicModel):
    decision_packet_id: str
    wait_set_id: str
    href: str


class ApprovalRequestedPayload(PublicModel):
    approval_id: str
    href: str


class ExperimentMetricsUpdatedPayload(PublicModel):
    experiment_id: str
    status: ExperimentStatus
    metrics: list[MetricValue]
    href: str


class RunCompletedPayload(PublicModel):
    outcome: Literal["SUCCEEDED"]
    result_href: str


class RunFailedPayload(PublicModel):
    outcome: Literal["FAILED"]
    failure_code: str
    retriable: bool
    result_href: str


class RunCanceledPayload(PublicModel):
    outcome: Literal["CANCELED"]
    result_href: str


class RunExpiredPayload(PublicModel):
    outcome: Literal["EXPIRED"]
    result_href: str


class BaseRunEvent(PublicModel):
    event_id: str
    run_id: str
    seq: Annotated[int, Field(ge=1)]
    run_revision: PositiveRevision
    schema_version: Literal["1.0"]
    occurred_at: datetime
    links: dict[str, str] = Field(default_factory=dict)


class PhaseChangedEvent(BaseRunEvent):
    type: Literal["run.phase_changed.v1"]
    payload: PhaseChangedPayload


class ProgressUpdatedEvent(BaseRunEvent):
    type: Literal["run.progress_updated.v1"]
    payload: ProgressUpdatedPayload


class OutputCommittedEvent(BaseRunEvent):
    type: Literal["output.committed.v1"]
    payload: OutputCommittedPayload


class DecisionPacketRequestedEvent(BaseRunEvent):
    type: Literal["decision_packet.requested.v1"]
    payload: DecisionPacketRequestedPayload


class ApprovalRequestedEvent(BaseRunEvent):
    type: Literal["approval.requested.v1"]
    payload: ApprovalRequestedPayload


class ExperimentMetricsUpdatedEvent(BaseRunEvent):
    type: Literal["experiment.metrics_updated.v1"]
    payload: ExperimentMetricsUpdatedPayload


class RunCompletedEvent(BaseRunEvent):
    type: Literal["run.completed.v1"]
    payload: RunCompletedPayload


class RunFailedEvent(BaseRunEvent):
    type: Literal["run.failed.v1"]
    payload: RunFailedPayload


class RunCanceledEvent(BaseRunEvent):
    type: Literal["run.canceled.v1"]
    payload: RunCanceledPayload


class RunExpiredEvent(BaseRunEvent):
    type: Literal["run.expired.v1"]
    payload: RunExpiredPayload


RunEventVariant = Annotated[
    PhaseChangedEvent
    | ProgressUpdatedEvent
    | OutputCommittedEvent
    | DecisionPacketRequestedEvent
    | ApprovalRequestedEvent
    | ExperimentMetricsUpdatedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCanceledEvent
    | RunExpiredEvent,
    Field(discriminator="type"),
]


class RunEvent(RootModel[RunEventVariant]):
    """Any public run event, discriminated by its versioned event ``type``."""


class RunEventPage(PublicModel):
    items: list[RunEvent]
    next_cursor: str | None
    high_watermark: NonNegativeInt
    retained_from_seq: NonNegativeInt


class QuestionOption(PublicModel):
    value: Any
    label: str
    consequence: str
    risk: QuestionOptionRisk = QuestionOptionRisk.MEDIUM
    risk_reason: str = "Selection changes the task definition and downstream evaluation."


class Question(PublicModel):
    question_id: str
    prompt: str
    answer_schema: dict[str, Any]
    selection_mode: QuestionSelectionMode = QuestionSelectionMode.FREEFORM
    min_selections: NonNegativeInt = 1
    max_selections: Annotated[int, Field(ge=1)] = 1
    evidence: str | None = None
    options: list[QuestionOption] = Field(default_factory=list)
    recommendation: Any = None
    recommendation_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_selection_contract(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("selection_mode") is not None:
            return value
        migrated = dict(value)
        migrated["selection_mode"] = "SINGLE" if migrated.get("options") else "FREEFORM"
        migrated.setdefault("min_selections", 1)
        migrated.setdefault("max_selections", 1)
        return migrated

    @model_validator(mode="after")
    def validate_selection_contract(self) -> Question:
        if self.min_selections > self.max_selections:
            raise ValueError("min_selections cannot exceed max_selections")
        if self.selection_mode in {
            QuestionSelectionMode.SINGLE,
            QuestionSelectionMode.FREEFORM,
        } and (self.min_selections != 1 or self.max_selections != 1):
            raise ValueError("SINGLE and FREEFORM questions require exactly one selection")

        for index, option in enumerate(self.options):
            if any(
                type(option.value) is type(previous.value) and option.value == previous.value
                for previous in self.options[:index]
            ):
                raise ValueError("question option values must be unique")

        if self.selection_mode == QuestionSelectionMode.MULTIPLE:
            if self.options and self.max_selections > len(self.options):
                raise ValueError("max_selections cannot exceed the number of options")
            if self.recommendation is not None and not isinstance(self.recommendation, list):
                raise ValueError("a MULTIPLE recommendation must be a list")
            recommended_values = self.recommendation or []
            if recommended_values and not (
                self.min_selections <= len(recommended_values) <= self.max_selections
            ):
                raise ValueError("the recommendation violates selection cardinality")
        else:
            recommended_values = [] if self.recommendation is None else [self.recommendation]

        if self.options and any(
            not any(
                type(value) is type(option.value) and value == option.value
                for option in self.options
            )
            for value in recommended_values
        ):
            raise ValueError("the recommendation must reference declared option values")
        return self


class DecisionPacket(PublicModel):
    decision_packet_id: str
    wait_set_id: str
    wait_set_revision: PositiveRevision
    run_id: str
    run_revision: PositiveRevision
    status: DecisionPacketStatus
    kind: Literal["CLARIFICATION", "DATA_REMEDIATION", "BUDGET_EXTENSION"]
    reason: str
    blocking: bool
    resolution_policy: DecisionResolutionPolicy = DecisionResolutionPolicy.HUMAN_REQUIRED
    questions: Annotated[list[Question], Field(min_length=1)]
    created_at: datetime
    expires_at: datetime
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_question_ids(self) -> DecisionPacket:
        question_ids = [question.question_id for question in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("question_id values must be unique within a DecisionPacket")
        return self


class AnswerDecisionPacketItem(RequestModel):
    question_id: str
    value: Any


class AnswerDecisionPacketRequest(RequestModel):
    answers: Annotated[list[AnswerDecisionPacketItem], Field(min_length=1)]


class DecisionPacketPage(PublicModel):
    items: list[DecisionPacket]
    page: PageMeta


class CommandReceipt(PublicModel):
    command_id: str
    run_id: str
    type: CommandType
    status: CommandStatus
    submitted_at: datetime
    links: dict[str, str]
    completed_at: datetime | None = None
    resulting_run_revision: PositiveRevision | None = None
    problem: Problem | None = None


class ModelRef(PublicModel):
    model_id: str
    href: str


class ResultReason(PublicModel):
    code: str
    message: str
    retriable: bool
    failed_gates: list[str]
    evidence_refs: list[str]
    remediation: list[str]


class BaseRunResult(PublicModel):
    result_manifest_id: str
    run_id: str
    summary: str
    output_refs: list[OutputRef]
    completed_at: datetime
    backend_id: str | None = None
    backend_version: str | None = None
    engine_version: str | None = None


class EligibleModelRunResult(BaseRunResult):
    outcome: Literal["SUCCEEDED"]
    model_disposition: Literal["ELIGIBLE_MODEL_AVAILABLE"]
    partial: Literal[False]
    eligible_model: ModelRef
    reason: None


class NoEligibleModelRunResult(BaseRunResult):
    outcome: Literal["SUCCEEDED"]
    model_disposition: Literal["NO_ELIGIBLE_MODEL"]
    partial: Literal[False]
    eligible_model: None
    reason: ResultReason


class IncompleteRunResult(BaseRunResult):
    outcome: Literal["FAILED", "CANCELED", "EXPIRED"]
    model_disposition: Literal["INCOMPLETE"]
    partial: Literal[True]
    eligible_model: None
    reason: ResultReason


RunResultVariant = Annotated[
    EligibleModelRunResult | NoEligibleModelRunResult | IncompleteRunResult,
    Field(discriminator="model_disposition"),
]


class RunResult(RootModel[RunResultVariant]):
    """A terminal result whose disposition fixes all legal field combinations."""


class Approval(PublicModel):
    approval_id: str
    run_id: str
    run_revision: PositiveRevision
    evidence_version: PositiveRevision
    kind: ApprovalKind
    status: ApprovalStatus
    evidence_refs: list[str]
    created_at: datetime
    expires_at: datetime
    decision_reason: str | None = None


class DecideApprovalRequest(RequestModel):
    decision: ApprovalDecision
    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    evidence_version: PositiveRevision


class ApprovalPage(PublicModel):
    items: list[Approval]
    page: PageMeta


class FieldSignature(PublicModel):
    name: str
    data_type: str
    required: bool


class ModelSignature(PublicModel):
    inputs: list[FieldSignature]
    outputs: list[FieldSignature]


class ModelCandidate(PublicModel):
    model_id: str
    run_id: str
    status: Literal["ELIGIBLE_CANDIDATE"]
    signature: ModelSignature
    model_card_output_id: str
    package_artifact_ref: ArtifactRef
    created_at: datetime


class CreateWebhookEndpointRequest(RequestModel):
    url: AnyUrl
    event_types: Annotated[list[str], Field(min_length=1)]
    description: Annotated[str, StringConstraints(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_event_types(self) -> CreateWebhookEndpointRequest:
        normalized = [item.strip() for item in self.event_types]
        if any(not item for item in normalized):
            raise ValueError("event_types cannot contain empty values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("event_types must be unique")
        self.event_types = normalized
        return self


class WebhookEndpoint(PublicModel):
    webhook_endpoint_id: str
    url: AnyUrl
    event_types: list[str]
    status: WebhookEndpointStatus
    signature_version: Literal["v1"]
    replay_window_seconds: Literal[300]
    created_at: datetime
    status_reason: str | None = None
    paused_at: datetime | None = None


class WebhookEndpointCreated(WebhookEndpoint):
    signing_secret: Annotated[str, StringConstraints(min_length=43, max_length=43)]


class WebhookSecretRotated(PublicModel):
    webhook_endpoint_id: str
    signing_secret: Annotated[str, StringConstraints(min_length=43, max_length=43)]
    previous_secret_valid_until: datetime
    created_at: datetime


class WebhookDelivery(PublicModel):
    delivery_id: str
    webhook_endpoint_id: str
    event_id: str
    event_type: str
    run_id: str
    status: WebhookDeliveryStatus
    attempt_count: NonNegativeInt
    first_attempt_at: datetime | None
    next_attempt_at: datetime | None
    last_response_status: Annotated[int, Field(ge=100, le=599)] | None
    last_problem: Problem | None
    created_at: datetime
    delivered_at: datetime | None
    exhausted_at: datetime | None
    redeliver_until: datetime | None


class WebhookDeliveryPage(PublicModel):
    items: list[WebhookDelivery]
    page: PageMeta


class WebhookRedeliveryReceipt(PublicModel):
    redelivery_id: str
    delivery_id: str
    status: Literal["ACCEPTED"]
    submitted_at: datetime
    delivery_href: str


class DeletionStore(PublicModel):
    name: str
    status: DeletionStoreStatus
    retained_until: datetime | None = None
    verified_at: datetime | None = None


class DeletionJob(PublicModel):
    deletion_id: str
    dataset_id: str
    status: DeletionJobStatus
    affected_run_ids: list[str]
    stores: list[DeletionStore]
    created_at: datetime
    completed_at: datetime | None = None


class AgentEventCheckpoint(PublicModel):
    after_seq: NonNegativeInt
    events_href: str


class AgentRunContext(PublicModel):
    schema_version: Literal["1.0"] = "1.0"
    run: RunSnapshot
    objective: Objective
    open_decision_packets: list[DecisionPacket]
    recent_output_refs: list[OutputRef]
    output_refs_truncated: bool
    event_checkpoint: AgentEventCheckpoint
    result_available: bool
    contains_raw_dataset_rows: Literal[False] = False
    may_include_dataset_derived_values: Literal[True] = True
    dataset_derived_text_trust: Literal["UNTRUSTED"] = "UNTRUSTED"
    actions_href: str
    links: dict[str, str]


class Artifact(ArtifactRef):
    state: ArtifactState
    run_id: str
    created_at: datetime
    lineage: Lineage
    output_id: str | None = None
    etag: str | None = None
    supports_range: bool | None = None


class DownloadTicket(PublicModel):
    ticket_id: str
    artifact_id: str
    url: AnyUrl
    expires_in_seconds: Literal[900]
    expires_at: datetime
    etag: str
    sha256: Sha256
    size_bytes: NonNegativeInt
    supports_range: bool
    required_headers: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "AgentActionList",
    "AgentEventCheckpoint",
    "AgentIfMatch",
    "AgentInterfaceManifest",
    "AgentOperationRef",
    "AgentRunContext",
    "AnswerDecisionPacketRequest",
    "Approval",
    "ApprovalPage",
    "Artifact",
    "ArtifactRef",
    "CommandReceipt",
    "CreateWebhookEndpointRequest",
    "CreateDatasetRequest",
    "CreateRunRequest",
    "DatasetUploadSession",
    "DatasetVersion",
    "DecideApprovalRequest",
    "DecisionPacket",
    "DecisionPacketPage",
    "DecisionResolutionPolicy",
    "DeletionJob",
    "DownloadTicket",
    "FinalizeDatasetRequest",
    "ModelCandidate",
    "OutputPage",
    "OutputResource",
    "PageMeta",
    "Problem",
    "Question",
    "QuestionOption",
    "QuestionOptionRisk",
    "QuestionSelectionMode",
    "RunEvent",
    "RunEventPage",
    "RunPage",
    "RunResult",
    "RunSnapshot",
    "SignUploadPartsRequest",
    "UploadPartsResponse",
    "WebhookDelivery",
    "WebhookDeliveryPage",
    "WebhookEndpoint",
    "WebhookEndpointCreated",
    "WebhookRedeliveryReceipt",
    "WebhookSecretRotated",
]
