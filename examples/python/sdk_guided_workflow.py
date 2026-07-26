#!/usr/bin/env python3
"""Run a guided upload/train/result/artifact workflow through the Python SDK."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
from typing import Any

import httpx

from automl_sdk import AutoMLClient


_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUTOML_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=_ROOT / "data" / "customer_churn.csv",
    )
    parser.add_argument("--backend", choices=("sklearn", "autogluon", "tabpfn"), default="sklearn")
    parser.add_argument("--target", default="churned")
    parser.add_argument("--output-dir", type=Path, default=Path("example-output"))
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--ca", type=Path, default=os.environ.get("AUTOML_CA_FILE"))
    return parser


def _ssl_context(ca_file: Path | None) -> ssl.SSLContext | bool:
    return ssl.create_default_context(cafile=str(ca_file)) if ca_file else True


def _answers(packet: dict[str, Any], target: str) -> dict[str, Any]:
    supported = {
        "q_target": target,
        "q_iid": True,
        "q_positive_class": 1,
    }
    question_ids = {question["question_id"] for question in packet["questions"]}
    unknown = question_ids - supported.keys()
    if unknown:
        raise RuntimeError(f"example cannot answer questions: {sorted(unknown)}")
    return {question_id: supported[question_id] for question_id in question_ids}


def main() -> int:
    args = _parser().parse_args()
    token = os.environ.get("AUTOML_TOKEN", "")
    if not token:
        raise SystemExit(
            "AUTOML_TOKEN is required; do not pass production tokens on the command line"
        )
    human_token = os.environ.get("AUTOML_HUMAN_TOKEN", token)
    if not args.data.is_file():
        raise SystemExit(f"dataset does not exist: {args.data}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    verify = _ssl_context(args.ca)
    timeout = httpx.Timeout(args.timeout, connect=30)
    with (
        httpx.Client(verify=verify, timeout=timeout) as agent_http,
        httpx.Client(verify=verify, timeout=timeout) as human_http,
    ):
        agent = AutoMLClient(args.base_url, token=token, http_client=agent_http)
        human = AutoMLClient(args.base_url, token=human_token, http_client=human_http)

        manifest = agent.get_agent_manifest()
        backend = next(item for item in manifest["backends"] if item["backend_id"] == args.backend)
        if not backend["available"]:
            raise RuntimeError(
                f"backend {args.backend} is unavailable: {backend.get('unavailable_reason')}"
            )

        dataset = agent.upload_dataset_file(
            args.data,
            name=f"example-{args.backend}-guided",
        )
        run = agent.create_run(
            dataset_version_id=dataset["dataset_version_id"],
            objective={
                "backend_id": args.backend,
                "target_column": None,
                "task_type": "BINARY_CLASSIFICATION",
                "positive_class": 1,
                "iid_confirmed": None,
                "primary_metric": "roc_auc",
                "business_context": "synthetic repository example",
            },
            autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
            policy={
                "allow_pii": False,
                "allow_external_llm": False,
                "risk_tier": "STANDARD",
            },
            budget={
                "max_trials": 2 if args.backend == "sklearn" else 1,
                "max_compute_credits": 1,
                "max_wall_time_seconds": int(args.timeout),
                "max_llm_tokens": 0,
            },
        )
        run_id = run["run_id"]
        packet = agent.wait_for_question(run_id, timeout=args.timeout, poll_interval=0.25)
        human.answer_and_wait(
            run_id,
            packet,
            _answers(packet, args.target),
            timeout=args.timeout,
            poll_interval=0.25,
        )
        result = agent.wait_for_result(run_id, timeout=args.timeout, poll_interval=0.25)

        events = agent.get_run_events(run_id, after_seq=0, limit=100)["items"]
        artifacts: list[dict[str, Any]] = []
        for output in agent.iter_outputs(run_id):
            for artifact_ref in output.get("artifact_refs", []):
                artifact_id = artifact_ref["artifact_id"]
                metadata = agent.get_artifact(artifact_id)
                destination = args.output_dir / f"{run_id}-{artifact_id}.artifact"
                agent.download_artifact_file(artifact_id, destination)
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": metadata["kind"],
                        "path": str(destination),
                        "size_bytes": destination.stat().st_size,
                        "sha256": metadata["sha256"],
                    }
                )

        print(
            json.dumps(
                {
                    "service_version": manifest["service_version"],
                    "run_id": run_id,
                    "backend_id": result["backend_id"],
                    "outcome": result["outcome"],
                    "model_disposition": result["model_disposition"],
                    "event_count": len(events),
                    "last_event": events[-1]["type"] if events else None,
                    "artifacts": artifacts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
