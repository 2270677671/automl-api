#!/usr/bin/env python3
"""Run the guided workflow with raw HTTP calls instead of the Python SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx


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
    parser.add_argument(
        "--task-type",
        choices=("BINARY_CLASSIFICATION", "REGRESSION"),
        default="BINARY_CLASSIFICATION",
    )
    parser.add_argument(
        "--primary-metric",
        help="defaults to roc_auc for classification and rmse for regression",
    )
    parser.add_argument(
        "--callback-uri",
        "--callback-url",
        dest="callback_uri",
        help="optional registered HTTPS stage callback URI",
    )
    parser.add_argument(
        "--webhook-endpoint-id",
        action="append",
        default=[],
        help="bind an existing Webhook endpoint; may be repeated",
    )
    parser.add_argument(
        "--preconfirm-objective",
        action="store_true",
        help="send target and i.i.d. confirmation at Run creation instead of waiting for a human",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("example-output"))
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--ca", type=Path, default=os.environ.get("AUTOML_CA_FILE"))
    return parser


def _ssl_context(ca_file: Path | None) -> ssl.SSLContext | bool:
    return ssl.create_default_context(cafile=str(ca_file)) if ca_file else True


def _same_origin(left: str, right: str) -> bool:
    left_url = urlsplit(left)
    right_url = urlsplit(right)
    return (left_url.scheme, left_url.hostname, left_url.port) == (
        right_url.scheme,
        right_url.hostname,
        right_url.port,
    )


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    token: str,
    expected: set[int],
    *,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    request_headers.update(headers or {})
    response = client.request(method, url, headers=request_headers, **kwargs)
    if response.status_code not in expected:
        raise RuntimeError(
            f"{method} {response.request.url} returned {response.status_code}: {response.text}"
        )
    return response


def _idempotency(prefix: str) -> dict[str, str]:
    return {"Idempotency-Key": f"example-{prefix}-{uuid4().hex}"}


def _answers(packet: dict[str, Any], target: str, task_type: str) -> list[dict[str, Any]]:
    supported = {"q_target": target, "q_iid": True}
    if task_type == "BINARY_CLASSIFICATION":
        supported["q_positive_class"] = 1
    answers = []
    for question in packet["questions"]:
        question_id = question["question_id"]
        if question_id not in supported:
            raise RuntimeError(f"example cannot answer question: {question_id}")
        answers.append({"question_id": question_id, "value": supported[question_id]})
    return answers


def main() -> int:
    args = _parser().parse_args()
    base_url = args.base_url.rstrip("/")
    primary_metric = args.primary_metric or (
        "roc_auc" if args.task_type == "BINARY_CLASSIFICATION" else "rmse"
    )
    token = os.environ.get("AUTOML_TOKEN", "")
    if not token:
        raise SystemExit(
            "AUTOML_TOKEN is required; do not pass production tokens on the command line"
        )
    human_token = os.environ.get("AUTOML_HUMAN_TOKEN", token)
    if not args.data.is_file():
        raise SystemExit(f"dataset does not exist: {args.data}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    content = args.data.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    deadline = time.monotonic() + args.timeout
    timeout = httpx.Timeout(args.timeout, connect=30)
    with httpx.Client(verify=_ssl_context(args.ca), timeout=timeout) as client:
        manifest = _request(client, "GET", f"{base_url}/v1/agent/manifest", token, {200}).json()
        backend = next(item for item in manifest["backends"] if item["backend_id"] == args.backend)
        if not backend["available"]:
            raise RuntimeError(
                f"backend {args.backend} is unavailable: {backend.get('unavailable_reason')}"
            )

        session = _request(
            client,
            "POST",
            f"{base_url}/v1/datasets",
            token,
            {201},
            headers=_idempotency("upload"),
            json={
                "name": f"raw-http-{args.backend}-guided",
                "filename": args.data.name,
                "media_type": "text/csv",
                "size_bytes": len(content),
            },
        ).json()
        part = session["parts"][0]
        upload_url = urljoin(f"{base_url}/", part["url"])
        upload_headers = dict(part.get("required_headers", {}))
        upload_headers.update({"Content-Type": "text/csv", "Content-Length": str(len(content))})
        if _same_origin(upload_url, base_url):
            upload_headers["Authorization"] = f"Bearer {token}"
        uploaded = client.put(upload_url, headers=upload_headers, content=content)
        if uploaded.status_code not in {200, 201, 204}:
            raise RuntimeError(f"PUT {upload_url} returned {uploaded.status_code}: {uploaded.text}")
        etag = uploaded.headers.get("ETag")
        if not etag:
            raise RuntimeError("upload response did not include ETag")

        dataset = _request(
            client,
            "POST",
            f"{base_url}/v1/dataset-versions/{session['dataset_version_id']}:finalize",
            token,
            {202},
            headers=_idempotency("finalize"),
            json={
                "upload_id": session["upload_id"],
                "parts": [{"part_number": part["part_number"], "etag": etag}],
                "sha256": digest,
            },
        ).json()

        run_payload = {
            "dataset_version_id": dataset["dataset_version_id"],
            "objective": {
                "backend_id": args.backend,
                "target_column": args.target if args.preconfirm_objective else None,
                "task_type": args.task_type,
                "positive_class": (1 if args.task_type == "BINARY_CLASSIFICATION" else None),
                "iid_confirmed": True if args.preconfirm_objective else None,
                "primary_metric": primary_metric,
                "business_context": "synthetic repository HTTP example",
            },
            "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
            "policy": {
                "allow_pii": False,
                "allow_external_llm": False,
                "risk_tier": "STANDARD",
            },
            "budget": {
                "max_trials": 2 if args.backend == "sklearn" else 1,
                "max_compute_credits": 1,
                "max_wall_time_seconds": int(args.timeout),
                "max_llm_tokens": 0,
            },
        }
        if args.callback_uri:
            run_payload["callback_uri"] = args.callback_uri
        if args.webhook_endpoint_id:
            run_payload["webhook_endpoint_ids"] = args.webhook_endpoint_id

        run = _request(
            client,
            "POST",
            f"{base_url}/v1/runs",
            token,
            {202},
            headers=_idempotency("run"),
            json=run_payload,
        ).json()
        run_id = run["run_id"]
        answered = False
        prior_status = None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} did not finish within {args.timeout} seconds")
            snapshot = _request(client, "GET", f"{base_url}/v1/runs/{run_id}", token, {200}).json()
            if snapshot["status"] != prior_status:
                print(f"run {run_id}: {snapshot['status']}")
                prior_status = snapshot["status"]
            if snapshot["status"] == "WAITING_USER" and not answered:
                page = _request(
                    client,
                    "GET",
                    f"{base_url}/v1/runs/{run_id}/decision-packets",
                    token,
                    {200},
                    params={"status": "OPEN"},
                ).json()
                packet = page["items"][0]
                receipt = _request(
                    client,
                    "POST",
                    (
                        f"{base_url}/v1/runs/{run_id}/decision-packets/"
                        f"{packet['wait_set_id']}:answer"
                    ),
                    human_token,
                    {202},
                    headers={
                        **_idempotency("answer"),
                        "If-Match": f'"{packet["wait_set_revision"]}"',
                    },
                    json={"answers": _answers(packet, args.target, args.task_type)},
                ).json()
                command_id = receipt["command_id"]
                while True:
                    command = _request(
                        client,
                        "GET",
                        f"{base_url}/v1/commands/{command_id}",
                        human_token,
                        {200},
                    ).json()
                    if command["status"] == "SUCCEEDED":
                        break
                    if command["status"] == "FAILED":
                        raise RuntimeError(f"answer command failed: {command}")
                    time.sleep(0.25)
                answered = True
            if snapshot["status"] == "TERMINAL":
                break
            time.sleep(0.25)

        result = _request(client, "GET", f"{base_url}/v1/runs/{run_id}/result", token, {200}).json()
        events = _request(
            client,
            "GET",
            f"{base_url}/v1/runs/{run_id}/events",
            token,
            {200},
            headers={"Accept": "application/json"},
            params={"after_seq": 0, "limit": 100},
        ).json()["items"]
        outputs = _request(
            client,
            "GET",
            f"{base_url}/v1/runs/{run_id}/outputs",
            token,
            {200},
            params={"limit": 100},
        ).json()["items"]

        artifacts: list[dict[str, Any]] = []
        for output in outputs:
            for artifact_ref in output.get("artifact_refs", []):
                artifact_id = artifact_ref["artifact_id"]
                ticket = _request(
                    client,
                    "POST",
                    f"{base_url}/v1/artifacts/{artifact_id}:download",
                    token,
                    {201},
                    headers=_idempotency("download"),
                ).json()
                downloaded = client.get(ticket["url"], headers=ticket["required_headers"])
                if downloaded.status_code != 200:
                    raise RuntimeError(
                        f"artifact download returned {downloaded.status_code}: {downloaded.text}"
                    )
                if downloaded.headers.get("ETag") != ticket["etag"]:
                    raise RuntimeError(f"artifact {artifact_id} ETag mismatch")
                if len(downloaded.content) != ticket["size_bytes"]:
                    raise RuntimeError(f"artifact {artifact_id} size mismatch")
                actual_sha256 = hashlib.sha256(downloaded.content).hexdigest()
                if actual_sha256 != ticket["sha256"]:
                    raise RuntimeError(f"artifact {artifact_id} SHA-256 mismatch")
                destination = args.output_dir / f"{run_id}-{artifact_id}.artifact"
                destination.write_bytes(downloaded.content)
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "path": str(destination),
                        "size_bytes": len(downloaded.content),
                        "sha256": actual_sha256,
                    }
                )

        print(
            json.dumps(
                {
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
