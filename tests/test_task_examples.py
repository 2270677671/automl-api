from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from automl_api.ml_engine import run_tabular_automl
from automl_api.models import CreateRunRequest, EvaluationReportPayload


_ROOT = Path(__file__).resolve().parents[1]
_CASES = {
    "classification": {
        "data": _ROOT / "examples" / "data" / "classification_360.csv",
        "request": _ROOT / "examples" / "requests" / "sklearn-classification-360.json",
        "sha256": "ff76b47ff7a1b93dde983c495c55ffb7425af7691a237d74d449f2c00e29caec",
        "target": "churned",
        "task_type": "BINARY_CLASSIFICATION",
        "positive_class": 1,
        "metric": "roc_auc",
    },
    "regression": {
        "data": _ROOT / "examples" / "data" / "regression_360.csv",
        "request": _ROOT / "examples" / "requests" / "sklearn-regression-360.json",
        "sha256": "1f475d0153648dbb0ce0029c22abbef2c5128081120b330919d2070d13b720c8",
        "target": "monthly_rent",
        "task_type": "REGRESSION",
        "positive_class": None,
        "metric": "rmse",
    },
}


@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_360_row_example_contracts_and_datasets(case_name: str) -> None:
    case = _CASES[case_name]
    data_path = Path(case["data"])
    frame = pd.read_csv(data_path)
    target = str(case["target"])

    assert len(frame) == 360
    assert len(frame) >= 300
    assert target in frame.columns
    assert frame[target].notna().all()
    assert hashlib.sha256(data_path.read_bytes()).hexdigest() == case["sha256"]

    request = json.loads(Path(case["request"]).read_text(encoding="utf-8"))
    request["dataset_version_id"] = "dsv_example"
    validated = CreateRunRequest.model_validate(request)
    assert validated.objective.target_column == target
    assert validated.objective.task_type == case["task_type"]
    assert validated.objective.primary_metric == case["metric"]


@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_360_row_examples_complete_real_sklearn_evaluation(case_name: str) -> None:
    case = _CASES[case_name]
    result = run_tabular_automl(
        Path(case["data"]),
        target_column=str(case["target"]),
        media_type="text/csv",
        task_type=str(case["task_type"]),
        positive_class=case["positive_class"],
        primary_metric=str(case["metric"]),
        iid_confirmed=True,
        max_trials=1,
    )
    evaluation = EvaluationReportPayload.model_validate(result.evaluation)

    assert evaluation.primary_metric == case["metric"]
    assert evaluation.candidate.name == case["metric"]
    assert evaluation.visualization_status == "COMPLETE"
    assert all(item.status == "GENERATED" for item in evaluation.visualizations)
    if case_name == "classification":
        assert evaluation.candidate.value > 0.65
    else:
        assert evaluation.candidate.value < 500


@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_recorded_api_io_is_complete_parseable_and_redacted(case_name: str) -> None:
    case = _CASES[case_name]
    case_dir = _ROOT / "examples" / "api-io" / case_name
    request_paths = sorted(case_dir.glob("*.request.json"))
    response_paths = sorted(case_dir.glob("*.response.json"))

    assert len(request_paths) == 18
    assert len(response_paths) == 18

    for path in request_paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["headers"]["Authorization"] == "Bearer <AUTOML_TOKEN>"
        assert "local-development-token" not in path.read_text(encoding="utf-8")

    for path in response_paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert 200 <= document["status_code"] < 300

    download_request = json.loads(
        (case_dir / "18-download-artifact-by-ticket.request.json").read_text(encoding="utf-8")
    )
    assert download_request["path"] == ("/v1/artifact-downloads/<REDACTED_EPHEMERAL_TICKET>")
    ticket_response = json.loads(
        (case_dir / "17-create-artifact-download-ticket.response.json").read_text(encoding="utf-8")
    )
    assert ticket_response["body"]["url"] == "<REDACTED_EPHEMERAL_DOWNLOAD_URL>"
    assert all(
        value == "<REDACTED>" for value in ticket_response["body"]["required_headers"].values()
    )

    image = case_dir / "18-download-artifact-by-ticket.response.png"
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    index = json.loads((case_dir / "index.json").read_text(encoding="utf-8"))
    assert index["dataset_rows"] == 360
    assert index["dataset_sha256"] == case["sha256"]
    assert index["primary_metric"]["name"] == case["metric"]
    assert [item["number"] for item in index["interfaces"]] == list(range(1, 19))
