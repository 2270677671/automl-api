from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from automl_sdk import AutoMLClient, ProtocolError


def _download_ticket(
    content: bytes,
    *,
    sha256: str | None = None,
    url: str = "https://objects.test/artifacts/art_1",
) -> dict[str, object]:
    return {
        "ticket_id": "ticket_1",
        "artifact_id": "art_1",
        "url": url,
        "expires_in_seconds": 900,
        "expires_at": "2026-07-23T12:00:00Z",
        "required_headers": {"X-Download-Signature": "signed"},
        "etag": '"artifact-etag"',
        "sha256": sha256 or hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "supports_range": True,
    }


def test_upload_dataset_file_streams_one_relative_part_and_finalizes(
    tmp_path: Path,
) -> None:
    content = b"feature,label\n" + (b"1,0\n" * 40_000)
    source = tmp_path / "training.csv"
    source.write_bytes(content)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/datasets":
            body = json.loads(request.read())
            assert body == {
                "name": "customer-churn",
                "filename": "training.csv",
                "media_type": "text/csv",
                "size_bytes": len(content),
            }
            return httpx.Response(
                201,
                json={
                    "dataset_id": "ds_1",
                    "dataset_version_id": "dsv_1",
                    "status": "UPLOADING",
                    "upload_id": "upload_1",
                    "expires_at": "2026-07-23T12:00:00Z",
                    "parts": [
                        {
                            "part_number": 1,
                            "url": "/storage/uploads/upload_1/1",
                            "expires_at": "2026-07-23T12:00:00Z",
                            "required_headers": {"X-Upload-Signature": "signed"},
                        }
                    ],
                },
            )
        if request.method == "PUT" and request.url.path == "/storage/uploads/upload_1/1":
            assert request.headers["Authorization"] == "Bearer sdk-token"
            assert request.headers["X-Upload-Signature"] == "signed"
            assert request.headers["Content-Type"] == "text/csv"
            assert request.headers["Content-Length"] == str(len(content))
            assert request.read() == content
            return httpx.Response(200, headers={"ETag": '"part-etag"'})
        if request.method == "POST" and request.url.path == "/v1/dataset-versions/dsv_1:finalize":
            body = json.loads(request.read())
            assert body == {
                "upload_id": "upload_1",
                "parts": [{"part_number": 1, "etag": '"part-etag"'}],
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            return httpx.Response(
                202,
                json={
                    "dataset_id": "ds_1",
                    "dataset_version_id": "dsv_1",
                    "status": "READY",
                    "revision": 2,
                    "media_type": "text/csv",
                    "size_bytes": len(content),
                    "sha256": body["sha256"],
                    "validation_issues": [],
                    "created_at": "2026-07-23T10:00:00Z",
                    "updated_at": "2026-07-23T10:01:00Z",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with AutoMLClient(
        "https://api.test",
        token="sdk-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.upload_dataset_file(source, name="customer-churn")

    assert result["status"] == "READY"
    assert requests == [
        ("POST", "/v1/datasets"),
        ("PUT", "/storage/uploads/upload_1/1"),
        ("POST", "/v1/dataset-versions/dsv_1:finalize"),
    ]


def test_download_artifact_file_verifies_and_atomically_replaces_target(
    tmp_path: Path,
) -> None:
    content = b"verified artifact bytes"
    target = tmp_path / "report.json"
    target.write_bytes(b"previous contents")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.test":
            assert request.method == "POST"
            assert request.url.path == "/v1/artifacts/art_1:download"
            return httpx.Response(201, json=_download_ticket(content))
        assert request.url == httpx.URL("https://objects.test/artifacts/art_1")
        assert "Authorization" not in request.headers
        assert request.headers["X-Download-Signature"] == "signed"
        assert "Range" not in request.headers
        return httpx.Response(
            200,
            headers={
                "ETag": '"artifact-etag"',
                "Content-Length": str(len(content)),
            },
            content=content,
        )

    with AutoMLClient(
        "https://api.test",
        token="sdk-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        downloaded = client.download_artifact_file("art_1", target)

    assert downloaded == target
    assert target.read_bytes() == content
    assert not target.with_name(f"{target.name}.part").exists()


def test_download_artifact_file_removes_bad_hash_without_creating_target(
    tmp_path: Path,
) -> None:
    content = b"artifact with a bad advertised hash"
    target = tmp_path / "bad.bin"
    wrong_hash = hashlib.sha256(b"different artifact").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.test":
            return httpx.Response(
                201,
                json=_download_ticket(content, sha256=wrong_hash),
            )
        return httpx.Response(
            200,
            headers={
                "ETag": '"artifact-etag"',
                "Content-Length": str(len(content)),
            },
            content=content,
        )

    with AutoMLClient(
        "https://api.test",
        token="sdk-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProtocolError, match="SHA-256"):
            client.download_artifact_file("art_1", target)

    assert not target.exists()
    assert not target.with_name(f"{target.name}.part").exists()


class _InterruptedStream(httpx.SyncByteStream):
    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield self._prefix
        raise httpx.ReadError("simulated connection loss")


def test_download_artifact_file_resumes_interrupted_transfer_with_range(
    tmp_path: Path,
) -> None:
    prefix = b"a" * (64 * 1024)
    content = prefix + b"remaining artifact bytes"
    target = tmp_path / "model.bin"
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.test":
            return httpx.Response(201, json=_download_ticket(content))

        range_header = request.headers.get("Range")
        ranges.append(range_header)
        if range_header is None:
            return httpx.Response(
                200,
                headers={
                    "ETag": '"artifact-etag"',
                    "Content-Length": str(len(content)),
                },
                stream=_InterruptedStream(prefix),
            )
        assert range_header == f"bytes={len(prefix)}-"
        remainder = content[len(prefix) :]
        return httpx.Response(
            206,
            headers={
                "ETag": '"artifact-etag"',
                "Content-Length": str(len(remainder)),
                "Content-Range": (f"bytes {len(prefix)}-{len(content) - 1}/{len(content)}"),
            },
            content=remainder,
        )

    with AutoMLClient(
        "https://api.test",
        token="sdk-token",
        transport=httpx.MockTransport(handler),
        max_transport_retries=1,
        sleep=lambda _delay: None,
    ) as client:
        client.download_artifact_file("art_1", target)

    assert ranges == [None, f"bytes={len(prefix)}-"]
    assert target.read_bytes() == content
    assert not target.with_name(f"{target.name}.part").exists()
