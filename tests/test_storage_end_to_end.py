from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from automl_api.app import create_app
from automl_api.storage import LocalBlobStore
from automl_api.store import InMemoryStore

from .helpers import AUTH, mutation_headers, run_request


def _upload_csv(client: TestClient, content: bytes, suffix: str) -> dict[str, object]:
    created = client.post(
        "/v1/datasets",
        headers=mutation_headers(f"real-upload-create-{suffix}"),
        json={
            "name": "real-csv",
            "filename": "training.csv",
            "media_type": "text/csv",
            "size_bytes": len(content),
        },
    )
    assert created.status_code == 201, created.text
    session = created.json()
    part = session["parts"][0]
    uploaded = client.put(
        part["url"],
        headers={**AUTH, **part["required_headers"]},
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized = client.post(
        f"/v1/dataset-versions/{session['dataset_version_id']}:finalize",
        headers=mutation_headers(f"real-upload-finalize-{suffix}"),
        json={
            "upload_id": session["upload_id"],
            "parts": [{"part_number": 1, "etag": uploaded.headers["etag"]}],
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert finalized.status_code == 202, finalized.text
    assert finalized.json()["status"] == "READY"
    return session


def test_real_upload_integrity_and_artifact_range_download(tmp_path) -> None:
    blob_store = LocalBlobStore(tmp_path / "objects", ticket_secret=b"test-secret" * 4)
    application = create_app(InMemoryStore(), blob_store=blob_store)
    content = b"feature,target\n1,0\n2,1\n3,0\n4,1\n"

    with TestClient(application) as client:
        session = _upload_csv(client, content, "0001")
        run = client.post(
            "/v1/runs",
            headers=mutation_headers("real-run-create-0001"),
            json=run_request(str(session["dataset_version_id"])),
        ).json()
        packet = client.get(
            f"/v1/runs/{run['run_id']}/decision-packets",
            headers=AUTH,
            params={"status": "OPEN"},
        ).json()["items"][0]
        answered = client.post(
            f"/v1/runs/{run['run_id']}/decision-packets/{packet['wait_set_id']}:answer",
            headers=mutation_headers(
                "real-answer-0001", **{"If-Match": f'"{packet["wait_set_revision"]}"'}
            ),
            json={"answers": [{"question_id": "q_target", "value": "target"}]},
        )
        assert answered.status_code == 202, answered.text

        report = client.get(
            f"/v1/runs/{run['run_id']}/outputs",
            headers=AUTH,
            params={"type": "RUN_REPORT"},
        ).json()["items"][0]
        artifact_id = report["artifact_refs"][0]["artifact_id"]
        ticket = client.post(
            f"/v1/artifacts/{artifact_id}:download",
            headers=mutation_headers("real-download-ticket-0001"),
        ).json()
        downloaded = client.get(ticket["url"], headers=ticket["required_headers"])
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).hexdigest() == ticket["sha256"]
        assert downloaded.headers["etag"] == ticket["etag"]

        resumed = client.get(
            ticket["url"],
            headers={**ticket["required_headers"], "Range": "bytes=10-"},
        )
        assert resumed.status_code == 206
        assert resumed.headers["content-range"].startswith("bytes 10-")
        assert resumed.content == downloaded.content[10:]


def test_finalize_rejects_a_declared_hash_that_does_not_match_bytes(tmp_path) -> None:
    blob_store = LocalBlobStore(tmp_path / "objects", ticket_secret=b"test-secret" * 4)
    with TestClient(create_app(InMemoryStore(), blob_store=blob_store)) as client:
        content = b"x,target\n1,0\n2,1\n"
        created = client.post(
            "/v1/datasets",
            headers=mutation_headers("bad-hash-create-0001"),
            json={
                "name": "bad-hash",
                "filename": "bad.csv",
                "media_type": "text/csv",
                "size_bytes": len(content),
            },
        ).json()
        part = created["parts"][0]
        uploaded = client.put(
            part["url"], headers={**AUTH, **part["required_headers"]}, content=content
        )
        rejected = client.post(
            f"/v1/dataset-versions/{created['dataset_version_id']}:finalize",
            headers=mutation_headers("bad-hash-finalize-0001"),
            json={
                "upload_id": created["upload_id"],
                "parts": [{"part_number": 1, "etag": uploaded.headers["etag"]}],
                "sha256": "f" * 64,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "upload_integrity_failed"
        version = client.get(
            f"/v1/dataset-versions/{created['dataset_version_id']}", headers=AUTH
        ).json()
        assert version["status"] == "UPLOADING"
