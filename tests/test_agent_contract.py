from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from automl_api.durable_workflow import DurableWorkflowService
from automl_api.models import (
    AgentActionList,
    AgentIfMatch,
    AgentInterfaceManifest,
    AgentOperationRef,
    AgentRunContext,
    DecisionPacket,
    Question,
    QuestionOption,
)
from automl_api.workflow import WorkflowService
from automl_sdk import AutoMLClient

from .helpers import AUTH, create_waiting_run


def test_agent_contract_models_fix_the_external_planner_boundary(client: TestClient) -> None:
    manifest = AgentInterfaceManifest.model_validate(
        {
            "openapi_href": "/openapi.yaml",
            "context_path_template": "/v1/runs/{run_id}/agent-context",
            "actions_path_template": "/v1/runs/{run_id}/agent-actions",
            "canonical_operation_ids": [
                "answerDecisionPacket",
                "pauseRun",
                "resumeRun",
                "cancelRun",
            ],
            "supported_task_types": ["BINARY_CLASSIFICATION", "REGRESSION"],
            "supported_media_types": [
                "text/csv",
                "application/vnd.apache.parquet",
            ],
        }
    ).model_dump(mode="json")
    assert manifest["service_role"] == "AUTOML_EXECUTION_BACKEND"
    assert manifest["planner_location"] == "EXTERNAL_AGENT_PLATFORM"
    assert manifest["internal_llm_calls"] is False
    assert manifest["llm_budget_owner"] == "EXTERNAL_AGENT_PLATFORM"
    assert manifest["max_llm_tokens_consumed"] is False
    assert manifest["credentials_must_remain_in_platform"] is True
    assert manifest["production_external_llm_safe"] is False

    with pytest.raises(ValidationError):
        AgentInterfaceManifest.model_validate({**manifest, "internal_llm_calls": True})
    assert AgentInterfaceManifest.model_validate(
        {**manifest, "production_external_llm_safe": True}
    ).production_external_llm_safe


def test_agent_action_refs_reuse_canonical_command_preconditions() -> None:
    answer = AgentOperationRef.model_validate(
        {
            "action": "ANSWER",
            "operation_id": "answerDecisionPacket",
            "href": "/v1/runs/run_1/decision-packets/ws_1:answer",
            "idempotency_key_required": True,
            "if_match": {
                "scope": "WAIT_SET_REVISION",
                "value": '"3"',
            },
            "request_schema_ref": "#/components/schemas/AnswerDecisionPacketRequest",
        }
    )
    actions = AgentActionList(
        run_id="run_1",
        run_revision=7,
        items=[answer],
    ).model_dump(mode="json")
    assert actions["items"][0]["method"] == "POST"
    assert actions["items"][0]["if_match"] == {
        "header": "If-Match",
        "scope": "WAIT_SET_REVISION",
        "value": '"3"',
    }

    with pytest.raises(ValidationError):
        AgentIfMatch(scope="RUN_REVISION", value="3")
    with pytest.raises(ValidationError):
        AgentIfMatch(scope="RUN_REVISION", value='"0"')


def test_decision_packet_exposes_structured_choice_and_resolution_policy() -> None:
    packet = DecisionPacket.model_validate(
        {
            "decision_packet_id": "dp_1",
            "wait_set_id": "ws_1",
            "wait_set_revision": 1,
            "run_id": "run_1",
            "run_revision": 2,
            "status": "OPEN",
            "kind": "CLARIFICATION",
            "reason": "Choose the target and split assumptions.",
            "blocking": True,
            "resolution_policy": "HUMAN_REQUIRED",
            "questions": [
                {
                    "question_id": "q_target",
                    "prompt": "Which target?",
                    "answer_schema": {"type": "string", "enum": ["a", "b"]},
                    "selection_mode": "SINGLE",
                    "min_selections": 1,
                    "max_selections": 1,
                    "options": [
                        {
                            "value": "a",
                            "label": "A",
                            "consequence": "Predict A.",
                            "risk": "MEDIUM",
                            "risk_reason": "Changes the task.",
                        },
                        {
                            "value": "b",
                            "label": "B",
                            "consequence": "Predict B.",
                            "risk": "HIGH",
                            "risk_reason": "May encode an outcome column.",
                        },
                    ],
                    "recommendation": "a",
                    "recommendation_reason": "No safe automatic inference.",
                }
            ],
            "created_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-02T00:00:00Z",
        }
    )
    question = packet.questions[0]
    assert packet.resolution_policy == "HUMAN_REQUIRED"
    assert question.selection_mode == "SINGLE"
    assert question.options[1].risk == "HIGH"
    assert question.recommendation == "a"

    with pytest.raises(ValidationError):
        Question(
            question_id="q_multi",
            prompt="Pick features",
            answer_schema={"type": "array"},
            selection_mode="MULTIPLE",
            min_selections=1,
            max_selections=2,
            options=[
                QuestionOption(value="x", label="X", consequence="Use X"),
            ],
        )

    legacy = packet.model_dump(mode="json")
    legacy.pop("resolution_policy")
    legacy_question = legacy["questions"][0]
    legacy_question.pop("selection_mode")
    legacy_question.pop("min_selections")
    legacy_question.pop("max_selections")
    for option in legacy_question["options"]:
        option.pop("risk")
        option.pop("risk_reason")
    migrated = DecisionPacket.model_validate(legacy)
    assert migrated.resolution_policy == "HUMAN_REQUIRED"
    assert migrated.questions[0].selection_mode == "SINGLE"
    assert migrated.questions[0].options[0].risk == "MEDIUM"


def test_human_gate_is_hidden_from_agent_actions_but_agent_allowed_is_exposed() -> None:
    run = {"run_id": "run_1", "run_revision": 3, "available_actions": ["ANSWER"]}
    packet = {
        "wait_set_id": "ws_1",
        "wait_set_revision": 1,
        "resolution_policy": "HUMAN_REQUIRED",
    }
    assert WorkflowService._agent_action_items(run, [packet]) == []
    approval = {**packet, "resolution_policy": "APPROVAL_REQUIRED"}
    assert WorkflowService._agent_action_items(run, [approval]) == []
    allowed = {**packet, "resolution_policy": "AGENT_ALLOWED"}
    actions = WorkflowService._agent_action_items(run, [allowed])
    assert len(actions) == 1
    assert actions[0]["action"] == "ANSWER"


def test_current_durable_questions_are_valid_multi_option_single_selects() -> None:
    initial = DurableWorkflowService._initial_questions(
        {"resolved_inputs": {}},
        {"columns": [{"name": "feature"}, {"name": "target"}]},
    )
    positive_class = DurableWorkflowService._positive_class_question(["no", "yes"])
    questions = [Question.model_validate(item) for item in [*initial, positive_class]]

    assert {question.question_id for question in questions} == {
        "q_target",
        "q_iid",
        "q_positive_class",
    }
    assert all(question.selection_mode == "SINGLE" for question in questions)
    assert all(question.min_selections == question.max_selections == 1 for question in questions)
    assert all(len(question.options) >= 2 for question in questions)
    assert all(option.risk_reason for question in questions for option in question.options)


def test_durable_decision_policy_only_delegates_low_risk_recommendations() -> None:
    agent_run = {"policy": {"allow_external_llm": True, "risk_tier": "STANDARD"}}
    human_run = {"policy": {"allow_external_llm": False, "risk_tier": "STANDARD"}}
    positive_class = DurableWorkflowService._positive_class_question(["0", "1"])
    initial = DurableWorkflowService._initial_questions(
        {"resolved_inputs": {}},
        {"columns": [{"name": "feature"}, {"name": "target"}]},
    )

    assert positive_class["recommendation"] == "1"
    assert (
        DurableWorkflowService._decision_resolution_policy(agent_run, [positive_class])
        == "AGENT_ALLOWED"
    )
    assert (
        DurableWorkflowService._decision_resolution_policy(human_run, [positive_class])
        == "HUMAN_REQUIRED"
    )
    assert (
        DurableWorkflowService._decision_resolution_policy(agent_run, initial) == "HUMAN_REQUIRED"
    )


def test_agent_run_context_is_bounded_and_marks_derived_text_untrusted(
    client: TestClient,
) -> None:
    run = create_waiting_run(client, "agent-context-model-0001")
    packets = client.get(
        f"/v1/runs/{run['run_id']}/decision-packets",
        headers=AUTH,
        params={"status": "OPEN"},
    ).json()["items"]
    context = AgentRunContext.model_validate(
        {
            "run": run,
            "objective": {},
            "open_decision_packets": packets,
            "recent_output_refs": run["latest_output_refs"],
            "output_refs_truncated": False,
            "event_checkpoint": {
                "after_seq": run["snapshot_seq"],
                "events_href": run["links"]["events"],
            },
            "result_available": False,
            "actions_href": f"/v1/runs/{run['run_id']}/agent-actions",
            "links": run["links"],
        }
    ).model_dump(mode="json")
    assert context["contains_raw_dataset_rows"] is False
    assert context["may_include_dataset_derived_values"] is True
    assert context["dataset_derived_text_trust"] == "UNTRUSTED"
    assert context["event_checkpoint"]["after_seq"] == run["snapshot_seq"]

    with pytest.raises(ValidationError):
        AgentRunContext.model_validate({**context, "contains_raw_dataset_rows": True})


def test_sdk_agent_reads_use_the_dedicated_read_only_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/agent/manifest":
            return httpx.Response(200, json={"schema_version": "1.0"})
        if request.url.path == "/v1/agent/tool-openapi.yaml":
            return httpx.Response(200, text="openapi: 3.1.0\n")
        if request.url.path == "/v1/runs/run_1/agent-context":
            if request.headers.get("if-none-match") == '"context-etag"':
                return httpx.Response(304)
            return httpx.Response(200, json={"schema_version": "1.0"})
        if request.url.path == "/v1/runs/run_1/agent-actions":
            return httpx.Response(200, json={"schema_version": "1.0", "items": []})
        raise AssertionError(f"unexpected SDK request: {request.method} {request.url}")

    sdk = AutoMLClient(
        "https://automl.example",
        token="platform-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert sdk.get_agent_manifest()["schema_version"] == "1.0"
        assert sdk.get_agent_tool_openapi() == "openapi: 3.1.0\n"
        assert sdk.get_agent_context("run_1", output_limit=7) == {"schema_version": "1.0"}
        assert (
            sdk.get_agent_context(
                "run_1",
                output_limit=7,
                if_none_match='"context-etag"',
            )
            is None
        )
        assert sdk.list_agent_actions("run_1")["items"] == []
        with pytest.raises(ValueError):
            sdk.get_agent_context("run_1", output_limit=0)
    finally:
        sdk.close()

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/v1/agent/manifest"),
        ("GET", "/v1/agent/tool-openapi.yaml"),
        ("GET", "/v1/runs/run_1/agent-context"),
        ("GET", "/v1/runs/run_1/agent-context"),
        ("GET", "/v1/runs/run_1/agent-actions"),
    ]
    assert seen[2].url.params["output_limit"] == "7"
    assert seen[3].headers["if-none-match"] == '"context-etag"'
    assert all(request.headers["authorization"] == "Bearer platform-token" for request in seen)


def test_canonical_openapi_declares_read_only_agent_integration() -> None:
    contract = (Path(__file__).resolve().parents[1] / "openapi" / "automl-api.yaml").read_text(
        encoding="utf-8"
    )
    assert "operationId: getAgentInterfaceManifest" in contract
    assert "operationId: getAgentRunContext" in contract
    assert "operationId: listAgentRunActions" in contract
    assert "/v1/runs/{run_id}/agent-actions:execute" not in contract
    for operation_id in ("answerDecisionPacket", "pauseRun", "resumeRun", "cancelRun"):
        assert contract.count(f"operationId: {operation_id}") == 1
