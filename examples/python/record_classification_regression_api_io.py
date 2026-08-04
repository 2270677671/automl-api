#!/usr/bin/env python3
"""Run both 360-row examples and record every distinct HTTP interface input/output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


_EXAMPLES_ROOT = Path(__file__).resolve().parents[1]
_CASES = {
    "classification": {
        "data": _EXAMPLES_ROOT / "data" / "classification_360.csv",
        "request": _EXAMPLES_ROOT / "requests" / "sklearn-classification-360.json",
    },
    "regression": {
        "data": _EXAMPLES_ROOT / "data" / "regression_360.csv",
        "request": _EXAMPLES_ROOT / "requests" / "sklearn-regression-360.json",
    },
}
_RESPONSE_HEADERS = {"content-type", "etag", "location", "retry-after", "content-length"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUTOML_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=_EXAMPLES_ROOT / "api-io",
    )
    parser.add_argument("--timeout", type=float, default=1200)
    return parser


def _json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() == "authorization":
            result[key] = "Bearer <AUTOML_TOKEN>"
        else:
            result[key] = value
    return result


def _sanitize_download_ticket(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_download_ticket(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _sanitize_download_ticket(item) for key, item in value.items()}
    if {"ticket_id", "artifact_id", "expires_at"}.issubset(result):
        result["url"] = "<REDACTED_EPHEMERAL_DOWNLOAD_URL>"
        result["required_headers"] = {
            key: "<REDACTED>" for key in (result.get("required_headers") or {})
        }
    return result


def _redacted_path(path: str) -> str:
    if "/v1/artifact-downloads/" in path:
        return "/v1/artifact-downloads/<REDACTED_EPHEMERAL_TICKET>"
    return path


class Recorder:
    def __init__(self, client: httpx.Client, output_dir: Path) -> None:
        self.client = client
        self.output_dir = output_dir
        self.entries: list[dict[str, Any]] = []

    def call(
        self,
        number: int,
        slug: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        body_description: Any = None,
        expected: set[int],
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        recorded_path = _redacted_path(path)
        request_document = {
            "method": method,
            "path": recorded_path,
            "headers": _redacted_headers(request_headers),
            "query": params or {},
            "body": body_description if body_description is not None else json_body,
        }
        stem = f"{number:02d}-{slug}"
        _json_file(self.output_dir / f"{stem}.request.json", request_document)

        response = self.client.request(
            method,
            path,
            headers=request_headers,
            params=params,
            json=json_body if content is None else None,
            content=content,
        )
        try:
            response_body: Any = response.json() if response.content else None
        except ValueError:
            response_body = {
                "binary": True,
                "size_bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        response_document = {
            "status_code": response.status_code,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower() in _RESPONSE_HEADERS
            },
            "body": _sanitize_download_ticket(response_body),
        }
        _json_file(self.output_dir / f"{stem}.response.json", response_document)
        self.entries.append(
            {
                "number": number,
                "interface": f"{method} {recorded_path}",
                "request_file": f"{stem}.request.json",
                "response_file": f"{stem}.response.json",
                "status_code": response.status_code,
            }
        )
        if response.status_code not in expected:
            raise RuntimeError(
                f"{method} {path} returned {response.status_code}: {response.text[:1000]}"
            )
        return response


def _auth(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _is_same_origin(first: str, second: str) -> bool:
    left, right = urlsplit(first), urlsplit(second)
    return (left.scheme, left.hostname, left.port) == (right.scheme, right.hostname, right.port)


def _run_case(
    case_name: str,
    config: dict[str, Path],
    *,
    base_url: str,
    token: str,
    output_root: Path,
    timeout: float,
) -> None:
    data_path = config["data"]
    content = data_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    case_dir = output_root / case_name
    case_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=base_url, timeout=httpx.Timeout(timeout, connect=30)) as client:
        recorder = Recorder(client, case_dir)
        recorder.call(
            1,
            "get-agent-manifest",
            "GET",
            "/v1/agent/manifest",
            headers=_auth(token),
            expected={200},
        )

        create_dataset_body = {
            "name": f"api-io-{case_name}-360",
            "filename": data_path.name,
            "media_type": "text/csv",
            "size_bytes": len(content),
        }
        upload_session_response = recorder.call(
            2,
            "create-dataset-upload",
            "POST",
            "/v1/datasets",
            headers={
                **_auth(token, idempotency_key=f"api-io-{case_name}-dataset-0001"),
                "Content-Type": "application/json",
            },
            json_body=create_dataset_body,
            expected={201},
        )
        upload_session = upload_session_response.json()
        dataset_version_id = str(upload_session["dataset_version_id"])
        upload_id = str(upload_session["upload_id"])
        part = upload_session["parts"][0]
        upload_url = urljoin(base_url.rstrip("/") + "/", str(part["url"]).lstrip("/"))
        upload_headers = {key: str(value) for key, value in part["required_headers"].items()}
        if _is_same_origin(upload_url, base_url):
            upload_headers["Authorization"] = f"Bearer {token}"
        upload_headers["Content-Type"] = "text/csv"
        upload_headers["Content-Length"] = str(len(content))
        uploaded_response = recorder.call(
            3,
            "upload-dataset-part",
            "PUT",
            upload_url,
            headers=upload_headers,
            content=content,
            body_description={
                "file": f"../../data/{data_path.name}",
                "media_type": "text/csv",
                "size_bytes": len(content),
                "sha256": digest,
            },
            expected={200, 201, 204},
        )
        etag = uploaded_response.headers["ETag"]

        finalized_response = recorder.call(
            4,
            "finalize-dataset-version",
            "POST",
            f"/v1/dataset-versions/{dataset_version_id}:finalize",
            headers={
                **_auth(token, idempotency_key=f"api-io-{case_name}-finalize-0001"),
                "Content-Type": "application/json",
            },
            json_body={
                "upload_id": upload_id,
                "parts": [{"part_number": int(part["part_number"]), "etag": etag}],
                "sha256": digest,
            },
            expected={202},
        )
        if finalized_response.json()["status"] != "READY":
            raise RuntimeError("example dataset did not become READY synchronously")

        recorder.call(
            5,
            "get-dataset-version",
            "GET",
            f"/v1/dataset-versions/{dataset_version_id}",
            headers=_auth(token),
            expected={200},
        )

        run_body = json.loads(config["request"].read_text(encoding="utf-8"))
        run_body["dataset_version_id"] = dataset_version_id
        # These recordings exercise the external-Agent context and action interfaces.
        run_body["policy"]["allow_external_llm"] = True
        create_run_response = recorder.call(
            6,
            "create-run",
            "POST",
            "/v1/runs",
            headers={
                **_auth(token, idempotency_key=f"api-io-{case_name}-run-0001"),
                "Content-Type": "application/json",
            },
            json_body=run_body,
            expected={202},
        )
        run_id = str(create_run_response.json()["run_id"])

        recorder.call(
            7,
            "get-run-initial",
            "GET",
            f"/v1/runs/{run_id}",
            headers=_auth(token),
            expected={200},
        )
        deadline = time.monotonic() + timeout
        while True:
            polled = client.get(f"/v1/runs/{run_id}", headers=_auth(token))
            if polled.status_code != 200:
                raise RuntimeError(f"Run polling failed: {polled.status_code} {polled.text}")
            if polled.json()["status"] == "TERMINAL":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Run {run_id} did not finish within {timeout} seconds")
            time.sleep(0.2)
        terminal_response = recorder.call(
            8,
            "get-run-terminal",
            "GET",
            f"/v1/runs/{run_id}",
            headers=_auth(token),
            expected={200},
        )
        if terminal_response.json()["outcome"] != "SUCCEEDED":
            raise RuntimeError(f"example Run failed: {terminal_response.text}")

        recorder.call(
            9,
            "list-run-stages",
            "GET",
            f"/v1/runs/{run_id}/stages",
            headers=_auth(token),
            expected={200},
        )
        recorder.call(
            10,
            "read-run-events",
            "GET",
            f"/v1/runs/{run_id}/events",
            headers=_auth(token),
            params={"after_seq": 0, "limit": 100},
            expected={200},
        )
        recorder.call(
            11,
            "get-agent-context",
            "GET",
            f"/v1/runs/{run_id}/agent-context",
            headers=_auth(token),
            params={"output_limit": 20},
            expected={200},
        )
        recorder.call(
            12,
            "list-agent-actions",
            "GET",
            f"/v1/runs/{run_id}/agent-actions",
            headers=_auth(token),
            expected={200},
        )
        outputs_response = recorder.call(
            13,
            "list-run-outputs",
            "GET",
            f"/v1/runs/{run_id}/outputs",
            headers=_auth(token),
            params={"limit": 100},
            expected={200},
        )
        outputs = outputs_response.json()["items"]
        evaluation = next(item for item in outputs if item["type"] == "EVALUATION_REPORT")
        recorder.call(
            14,
            "get-evaluation-output",
            "GET",
            f"/v1/runs/{run_id}/outputs/{evaluation['output_id']}",
            headers=_auth(token),
            expected={200},
        )
        result_response = recorder.call(
            15,
            "get-run-result",
            "GET",
            f"/v1/runs/{run_id}/result",
            headers=_auth(token),
            expected={200},
        )
        result = result_response.json()
        visualization = result["visualization_refs"][0]
        artifact_id = str(visualization["artifact_id"])
        recorder.call(
            16,
            "get-artifact-metadata",
            "GET",
            f"/v1/artifacts/{artifact_id}",
            headers=_auth(token),
            expected={200},
        )
        ticket_response = recorder.call(
            17,
            "create-artifact-download-ticket",
            "POST",
            f"/v1/artifacts/{artifact_id}:download",
            headers=_auth(token, idempotency_key=f"api-io-{case_name}-artifact-download-0001"),
            expected={201},
        )
        ticket = ticket_response.json()
        download_url = urljoin(base_url.rstrip("/") + "/", str(ticket["url"]).lstrip("/"))
        download_headers = {
            key: str(value) for key, value in ticket.get("required_headers", {}).items()
        }
        if _is_same_origin(download_url, base_url):
            download_headers["Authorization"] = f"Bearer {token}"
        downloaded_response = recorder.call(
            18,
            "download-artifact-by-ticket",
            "GET",
            download_url,
            headers=download_headers,
            body_description=None,
            expected={200},
        )
        artifact_bytes = downloaded_response.content
        (case_dir / "18-download-artifact-by-ticket.response.png").write_bytes(artifact_bytes)

        _json_file(
            case_dir / "index.json",
            {
                "case": case_name,
                "dataset_file": f"../../data/{data_path.name}",
                "dataset_rows": 360,
                "dataset_sha256": digest,
                "run_id": run_id,
                "primary_metric": evaluation["payload"]["candidate"],
                "interfaces": recorder.entries,
                "notes": [
                    "Authorization values are redacted in request files.",
                    "The artifact ticket URL and required headers are redacted in response files.",
                    "GET /v1/runs/{run_id} is polled until TERMINAL; initial and terminal examples are recorded.",
                    "Only the first visualization is downloaded because all visualization downloads use the same interface.",
                ],
            },
        )


def main() -> int:
    args = _parser().parse_args()
    token = os.environ.get("AUTOML_TOKEN", "").strip()
    if not token:
        raise SystemExit("AUTOML_TOKEN is required")
    base_url = args.base_url.rstrip("/")
    for case_name, config in _CASES.items():
        _run_case(
            case_name,
            config,
            base_url=base_url,
            token=token,
            output_root=args.output_root,
            timeout=args.timeout,
        )
        print(f"recorded {case_name} API I/O under {args.output_root / case_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
