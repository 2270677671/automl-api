from __future__ import annotations


CANONICAL_ROUTE_OPERATION_IDS = {
    ("GET", "/v1/agent/manifest"): "getAgentInterfaceManifest",
    ("GET", "/v1/runs/{run_id}/agent-context"): "getAgentRunContext",
    ("GET", "/v1/runs/{run_id}/agent-actions"): "listAgentRunActions",
    ("POST", "/v1/datasets"): "createDatasetUpload",
    (
        "POST",
        "/v1/dataset-versions/{dataset_version_id}/upload-parts:sign",
    ): "signDatasetUploadParts",
    ("POST", "/v1/dataset-versions/{dataset_version_id}:finalize"): "finalizeDatasetUpload",
    ("GET", "/v1/dataset-versions/{dataset_version_id}"): "getDatasetVersion",
    ("POST", "/v1/runs"): "createRun",
    ("GET", "/v1/runs"): "listRuns",
    ("GET", "/v1/runs/{run_id}"): "getRun",
    ("GET", "/v1/runs/{run_id}/stages"): "listRunStages",
    ("GET", "/v1/runs/{run_id}/events"): "readRunEvents",
    ("GET", "/v1/runs/{run_id}/outputs"): "listRunOutputs",
    ("GET", "/v1/runs/{run_id}/outputs/{output_id}"): "getRunOutput",
    ("GET", "/v1/runs/{run_id}/experiments"): "listRunExperiments",
    ("GET", "/v1/runs/{run_id}/experiments/{experiment_id}"): "getRunExperiment",
    ("GET", "/v1/runs/{run_id}/decision-packets"): "listDecisionPackets",
    (
        "POST",
        "/v1/runs/{run_id}/decision-packets/{wait_set_id}:answer",
    ): "answerDecisionPacket",
    ("GET", "/v1/runs/{run_id}/approvals"): "listRunApprovals",
    ("POST", "/v1/runs/{run_id}/approvals/{approval_id}:decide"): "decideApproval",
    ("POST", "/v1/runs/{run_id}:pause"): "pauseRun",
    ("POST", "/v1/runs/{run_id}:resume"): "resumeRun",
    ("POST", "/v1/runs/{run_id}:cancel"): "cancelRun",
    ("GET", "/v1/commands/{command_id}"): "getCommand",
    ("GET", "/v1/runs/{run_id}/result"): "getRunResult",
    ("GET", "/v1/artifacts/{artifact_id}"): "getArtifact",
    ("POST", "/v1/artifacts/{artifact_id}:download"): "createArtifactDownloadTicket",
    ("GET", "/v1/models/{model_id}"): "getModelCandidate",
    ("POST", "/v1/webhook-endpoints"): "createWebhookEndpoint",
    ("GET", "/v1/webhook-endpoints"): "listWebhookEndpoints",
    ("GET", "/v1/webhook-endpoints/{webhook_endpoint_id}"): "getWebhookEndpoint",
    ("DELETE", "/v1/webhook-endpoints/{webhook_endpoint_id}"): "deleteWebhookEndpoint",
    (
        "POST",
        "/v1/webhook-endpoints/{webhook_endpoint_id}:rotate-secret",
    ): "rotateWebhookEndpointSecret",
    ("POST", "/v1/webhook-endpoints/{webhook_endpoint_id}:enable"): "enableWebhookEndpoint",
    (
        "GET",
        "/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries",
    ): "listWebhookDeliveries",
    (
        "GET",
        "/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}",
    ): "getWebhookDelivery",
    (
        "POST",
        "/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}:redeliver",
    ): "redeliverWebhookDelivery",
    ("DELETE", "/v1/datasets/{dataset_id}"): "deleteDataset",
    ("GET", "/v1/deletions/{deletion_id}"): "getDeletionJob",
}

CANONICAL_OPERATION_IDS = tuple(CANONICAL_ROUTE_OPERATION_IDS.values())

ACTIVE_AGENT_OPERATION_IDS = (
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
)

KNOWN_OPERATION_IDS = frozenset(CANONICAL_OPERATION_IDS)


__all__ = [
    "ACTIVE_AGENT_OPERATION_IDS",
    "CANONICAL_OPERATION_IDS",
    "CANONICAL_ROUTE_OPERATION_IDS",
    "KNOWN_OPERATION_IDS",
]
