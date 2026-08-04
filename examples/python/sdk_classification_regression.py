#!/usr/bin/env python3
"""Run the 360-row classification or regression example through the AutoML SDK."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import ssl
from typing import Any

import httpx

from automl_sdk import AutoMLClient


_EXAMPLES_ROOT = Path(__file__).resolve().parents[1]
_TASKS: dict[str, dict[str, Any]] = {
    "classification": {
        "data": _EXAMPLES_ROOT / "data" / "classification_360.csv",
        "target": "churned",
        "task_type": "BINARY_CLASSIFICATION",
        "positive_class": 1,
        "primary_metric": "roc_auc",
        "business_context": "Predict whether a subscription customer will churn.",
    },
    "regression": {
        "data": _EXAMPLES_ROOT / "data" / "regression_360.csv",
        "target": "monthly_rent",
        "task_type": "REGRESSION",
        "positive_class": None,
        "primary_metric": "rmse",
        "business_context": "Estimate monthly rent from property attributes.",
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=tuple(_TASKS))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUTOML_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--backend", choices=("sklearn", "autogluon", "tabpfn"), default="sklearn")
    parser.add_argument("--data", type=Path, help="override the bundled dataset")
    parser.add_argument("--max-trials", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ca", type=Path, default=os.environ.get("AUTOML_CA_FILE"))
    parser.add_argument(
        "--callback-uri",
        default=os.environ.get("AUTOML_CALLBACK_URI"),
        help="optional callback URI already registered with the API",
    )
    parser.add_argument(
        "--webhook-endpoint-id",
        action="append",
        default=[],
        help="registered endpoint ID; repeat to bind multiple endpoints",
    )
    return parser


def _ssl_context(ca_file: Path | None) -> ssl.SSLContext | bool:
    return ssl.create_default_context(cafile=str(ca_file)) if ca_file else True


def _validate_dataset(path: Path, target: str) -> int:
    if not path.is_file():
        raise SystemExit(f"dataset does not exist: {path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if target not in (reader.fieldnames or []):
            raise SystemExit(f"target column {target!r} is missing from {path}")
        rows = list(reader)
    if len(rows) < 300:
        raise SystemExit(f"example requires at least 300 rows, found {len(rows)}")
    if any(row.get(target, "").strip() == "" for row in rows):
        raise SystemExit(f"target column {target!r} contains missing values")
    return len(rows)


def _artifact_suffix(media_type: str) -> str:
    return {"image/png": ".png", "application/json": ".json"}.get(media_type, ".artifact")


def main() -> int:
    args = _parser().parse_args()
    token = os.environ.get("AUTOML_TOKEN", "").strip()
    if not token:
        raise SystemExit("AUTOML_TOKEN is required")

    task = dict(_TASKS[args.task])
    data_path = args.data or task["data"]
    row_count = _validate_dataset(data_path, task["target"])
    output_dir = args.output_dir or Path("example-output") / args.task
    output_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        verify=_ssl_context(args.ca),
        timeout=httpx.Timeout(args.timeout, connect=30),
    ) as http_client:
        api = AutoMLClient(args.base_url, token=token, http_client=http_client)
        manifest = api.get_agent_manifest()
        backend = next(item for item in manifest["backends"] if item["backend_id"] == args.backend)
        if not backend["available"]:
            raise RuntimeError(
                f"backend {args.backend} is unavailable: {backend.get('unavailable_reason')}"
            )

        dataset = api.upload_dataset_file(
            data_path,
            name=f"example-{args.task}-{row_count}",
        )
        objective = {
            "backend_id": args.backend,
            "target_column": task["target"],
            "task_type": task["task_type"],
            "positive_class": task["positive_class"],
            "iid_confirmed": True,
            "primary_metric": task["primary_metric"],
            "business_context": task["business_context"],
        }
        create_fields: dict[str, Any] = {}
        if args.callback_uri:
            create_fields["callback_uri"] = args.callback_uri
        if args.webhook_endpoint_id:
            create_fields["webhook_endpoint_ids"] = args.webhook_endpoint_id
        run = api.create_run(
            dataset_version_id=dataset["dataset_version_id"],
            objective=objective,
            autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
            policy={
                "allow_pii": False,
                "allow_external_llm": False,
                "risk_tier": "STANDARD",
            },
            budget={
                "max_trials": args.max_trials,
                "max_compute_credits": 1,
                "max_wall_time_seconds": int(args.timeout),
                "max_llm_tokens": 0,
            },
            **create_fields,
        )
        result = api.wait_for_result(run["run_id"], timeout=args.timeout, poll_interval=0.25)
        outputs = list(api.iter_outputs(run["run_id"]))
        evaluation = next((item for item in outputs if item["type"] == "EVALUATION_REPORT"), None)

        downloaded: list[str] = []
        for artifact_ref in result.get("visualization_refs", []):
            metadata = api.get_artifact(artifact_ref["artifact_id"])
            suffix = _artifact_suffix(metadata["media_type"])
            artifact_name = metadata["kind"].lower()
            if suffix == ".png" and artifact_name.endswith("_png"):
                artifact_name = artifact_name.removesuffix("_png")
            destination = output_dir / f"{artifact_name}{suffix}"
            api.download_artifact_file(metadata["artifact_id"], destination)
            downloaded.append(str(destination))

        summary = {
            "task": args.task,
            "rows": row_count,
            "run_id": run["run_id"],
            "backend_id": result.get("backend_id"),
            "outcome": result["outcome"],
            "model_disposition": result["model_disposition"],
            "primary_metric": (
                evaluation.get("payload", {}).get("candidate") if evaluation else None
            ),
            "visualization_status": (
                evaluation.get("payload", {}).get("visualization_status") if evaluation else None
            ),
            "downloaded_visualizations": downloaded,
            "result_href": f"/v1/runs/{run['run_id']}/result",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result["outcome"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
