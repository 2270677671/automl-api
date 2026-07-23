from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from automl_api.app import create_app
from automl_sdk import AutoMLClient


def _write_binary_classification_csv(path: Path, *, rows: int = 60) -> None:
    lines = ["feature_a,feature_b,segment,target"]
    for index in range(rows):
        target = int(index % 6 in {0, 1, 2})
        feature_a = index + target * 0.75
        feature_b = (index % 11) * 0.4 + target
        segment = ("north", "south", "central")[index % 3]
        lines.append(f"{feature_a},{feature_b},{segment},{target}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_sdk_completes_real_local_durable_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    source = tmp_path / "training.csv"
    report_path = tmp_path / "run-report.json"
    _write_binary_classification_csv(source)
    monkeypatch.setenv("AUTOML_STATE_DIR", str(state_dir))

    with TestClient(create_app()) as http_client:
        with AutoMLClient(
            "http://testserver",
            token="sdk-durable-test-token",
            http_client=http_client,
        ) as sdk:
            dataset = sdk.upload_dataset_file(source, name="durable-sdk-test")
            assert dataset["status"] == "READY"

            run = sdk.create_run(
                dataset_version_id=dataset["dataset_version_id"],
                objective={},
                autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
                policy={
                    "allow_pii": False,
                    "allow_external_llm": False,
                    "risk_tier": "STANDARD",
                },
                budget={
                    "max_trials": 2,
                    "max_compute_credits": 1,
                    "max_wall_time_seconds": 60,
                    "max_llm_tokens": 0,
                },
            )
            assert run["status"] == "QUEUED"

            question = sdk.wait_for_question(run["run_id"], timeout=20, poll_interval=0.01)
            assert {item["question_id"] for item in question["questions"]} == {
                "q_target",
                "q_iid",
            }

            command = sdk.answer_and_wait(
                run["run_id"],
                question,
                {"q_target": "target", "q_iid": True},
                timeout=20,
                poll_interval=0.01,
            )
            assert command["status"] == "SUCCEEDED"

            result = sdk.wait_for_result(run["run_id"], timeout=30, poll_interval=0.01)
            assert result["outcome"] == "SUCCEEDED"
            assert result["model_disposition"] == "NO_ELIGIBLE_MODEL"

            report = next(sdk.iter_outputs(run["run_id"], types=["RUN_REPORT"]))
            artifact_id = report["artifact_refs"][0]["artifact_id"]
            downloaded = sdk.download_artifact_file(artifact_id, report_path)

    assert downloaded == report_path
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_payload["task"]["target_column_id"] == "target"
    assert report_payload["evaluation"]["production_eligible"] is False
    assert (state_dir / "automl.db").is_file()
