from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from automl_api.models import (
    AgentInterfaceManifest,
    BackendDescriptor,
    DecisionPacket,
    DecisionResolutionPolicy,
    Objective,
    Question,
    QuestionOption,
    QuestionOptionRisk,
    QuestionSelectionMode,
)
from scripts.generate_agent_openapi import (
    ACTIVE_OPERATION_IDS,
    parse_operations,
    render_agent_contract,
)


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = ROOT / "openapi" / "automl-api.yaml"
AGENT_TOOLS_PATH = ROOT / "openapi" / "automl-agent-tools.yaml"


def _schema_block(contract: str, name: str) -> str:
    schemas_start = contract.index("  schemas:\n")
    marker = f"    {name}:\n"
    start = contract.index(marker, schemas_start)
    remainder = contract[start + len(marker) :]
    next_schema = re.search(r"^    [A-Za-z][A-Za-z0-9_]*:\s*$", remainder, re.MULTILINE)
    end = start + len(marker) + (next_schema.start() if next_schema else len(remainder))
    return contract[start:end]


def _enum_values(contract: str, name: str) -> list[str]:
    match = re.search(r"^      enum: \[([^]]+)]\s*$", _schema_block(contract, name), re.MULTILINE)
    assert match is not None, f"{name} must use a deterministic inline enum"
    return [value.strip() for value in match.group(1).split(",")]


def _property_block(schema: str, name: str) -> str:
    marker = re.compile(rf"^        {re.escape(name)}:\s*", re.MULTILINE)
    match = marker.search(schema)
    assert match is not None, f"missing property {name}"
    remainder = schema[match.start() :]
    next_property = re.search(r"^        [A-Za-z][A-Za-z0-9_]*:\s*", remainder[1:], re.MULTILINE)
    return remainder if next_property is None else remainder[: next_property.start() + 1]


def _components(contract: str) -> str:
    return contract[contract.index("\ncomponents:\n") :]


def test_decision_contract_matches_the_public_pydantic_models() -> None:
    contract = CANONICAL_PATH.read_text(encoding="utf-8")

    assert _enum_values(contract, "DecisionResolutionPolicy") == [
        item.value for item in DecisionResolutionPolicy
    ]
    assert _enum_values(contract, "QuestionSelectionMode") == [
        item.value for item in QuestionSelectionMode
    ]
    assert _enum_values(contract, "QuestionOptionRisk") == [
        item.value for item in QuestionOptionRisk
    ]

    decision_schema = DecisionPacket.model_json_schema()
    decision_block = _schema_block(contract, "DecisionPacket")
    resolution_policy = _property_block(decision_block, "resolution_policy")
    assert "$ref: '#/components/schemas/DecisionResolutionPolicy'" in resolution_policy
    assert (
        f"default: {decision_schema['properties']['resolution_policy']['default']}"
        in resolution_policy
    )

    question_schema = Question.model_json_schema()
    question_block = _schema_block(contract, "Question")
    selection_mode = _property_block(question_block, "selection_mode")
    assert "$ref: '#/components/schemas/QuestionSelectionMode'" in selection_mode
    for field in ("selection_mode", "min_selections", "max_selections"):
        expected = question_schema["properties"][field]["default"]
        assert f"default: {expected}" in _property_block(question_block, field)
    assert "minimum: 0" in _property_block(question_block, "min_selections")
    assert "minimum: 1" in _property_block(question_block, "max_selections")
    assert "items: {$ref: '#/components/schemas/QuestionOption'}" in _property_block(
        question_block, "options"
    )

    option_schema = QuestionOption.model_json_schema()
    option_block = _schema_block(contract, "QuestionOption")
    risk = _property_block(option_block, "risk")
    risk_reason = _property_block(option_block, "risk_reason")
    assert "$ref: '#/components/schemas/QuestionOptionRisk'" in risk
    assert f"default: {option_schema['properties']['risk']['default']}" in risk
    assert option_schema["properties"]["risk_reason"]["default"] in risk_reason


def test_backend_selection_contract_matches_the_public_pydantic_models() -> None:
    contract = CANONICAL_PATH.read_text(encoding="utf-8")

    objective_schema = Objective.model_json_schema()
    objective_block = _schema_block(contract, "Objective")
    backend_id = _property_block(objective_block, "backend_id")
    assert f"default: {objective_schema['properties']['backend_id']['default']}" in backend_id
    assert objective_schema["properties"]["backend_id"]["pattern"] in backend_id

    manifest_block = _schema_block(contract, "AgentInterfaceManifest")
    assert "- default_backend_id" in manifest_block
    assert "- backends" in manifest_block
    assert "$ref: '#/components/schemas/BackendDescriptor'" in _property_block(
        manifest_block, "backends"
    )

    descriptor_required = set(BackendDescriptor.model_json_schema()["required"])
    descriptor_block = _schema_block(contract, "BackendDescriptor")
    for field in descriptor_required:
        assert f"- {field}" in descriptor_block

    assert (
        AgentInterfaceManifest.model_json_schema()["properties"]["default_backend_id"]["default"]
        == "sklearn"
    )


def test_agent_tools_contract_is_a_fresh_canonical_filter() -> None:
    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    active = AGENT_TOOLS_PATH.read_text(encoding="utf-8")

    assert active == render_agent_contract(canonical)
    assert _components(active) == _components(canonical)
    assert [item.operation_id for item in parse_operations(active)] == list(ACTIVE_OPERATION_IDS)
    assert "x-maturity: not-implemented" not in active
    assert "'501':" not in active


def test_agent_tools_operations_exist_in_the_runtime_routes(client) -> None:
    active = AGENT_TOOLS_PATH.read_text(encoding="utf-8")
    operations = parse_operations(active)
    assert len(operations) == len(ACTIVE_OPERATION_IDS)
    assert len({item.operation_id for item in operations}) == len(operations)

    runtime_routes = {
        (route.path, method.lower())
        for route in client.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    missing = {(item.path, item.method) for item in operations}.difference(runtime_routes)
    assert not missing


def test_canonical_operation_ids_are_unique() -> None:
    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    operation_ids = [item.operation_id for item in parse_operations(canonical)]
    assert len(operation_ids) == len(set(operation_ids))
