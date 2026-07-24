from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Protocol
from uuid import uuid4

import anyio


class BlobStoreError(RuntimeError):
    """Base error raised by the immutable byte store."""


class BlobNotFoundError(BlobStoreError):
    pass


class BlobIntegrityError(BlobStoreError):
    pass


class BlobSizeLimitExceeded(BlobStoreError):
    pass


class InvalidTicketError(BlobStoreError):
    pass


class ExpiredTicketError(BlobStoreError):
    pass


@dataclass(frozen=True, slots=True)
class BlobInfo:
    key: str
    size_bytes: int
    sha256: str
    etag: str


class BlobStore(Protocol):
    durable: bool

    def upload_url(
        self,
        public_base_url: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
    ) -> str: ...

    async def put_upload_part(
        self,
        *,
        tenant_id: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterable[bytes],
    ) -> BlobInfo: ...

    async def finalize_upload(
        self,
        *,
        tenant_id: str,
        dataset_version_id: str,
        upload_id: str,
        parts: Iterable[dict[str, Any]],
        expected_size: int,
        expected_sha256: str,
    ) -> BlobInfo: ...

    async def put_artifact(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
        content: bytes,
    ) -> BlobInfo: ...

    async def delete_key(self, key: str) -> bool: ...

    async def delete_dataset_version(self, *, tenant_id: str, dataset_version_id: str) -> None: ...

    def path_for_key(self, key: str) -> Path: ...

    def create_download_token(
        self, *, artifact_id: str, tenant_id: str, etag: str, expires_at: int
    ) -> str: ...

    def verify_download_token(self, token: str) -> dict[str, Any]: ...


def _safe_component(value: str, field: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError(f"{field} must not be empty")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in value
    ):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _etag(sha256: str) -> str:
    return f'"{sha256}"'


class LocalBlobStore:
    """Immutable local byte store for a durable single-node deployment.

    The layout never includes user-provided filenames. Files are staged in the
    destination directory and atomically renamed after hashes have been checked.
    """

    durable = True

    def __init__(
        self,
        root: str | Path,
        *,
        ticket_secret: bytes | None = None,
        max_upload_part_bytes: int | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        configured = os.environ.get("AUTOML_TICKET_SECRET")
        self._ticket_secret = ticket_secret or (
            configured.encode("utf-8") if configured else os.urandom(32)
        )
        self._max_upload_part_bytes = max_upload_part_bytes

    def upload_url(
        self,
        public_base_url: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
    ) -> str:
        base = public_base_url.rstrip("/")
        return (
            f"{base}/v1/dataset-versions/{dataset_version_id}/upload-parts/{part_number}"
            f"?upload_id={upload_id}"
        )

    def _upload_part_path(
        self,
        tenant_id: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
    ) -> Path:
        tenant = _safe_component(tenant_id, "tenant_id")
        version = _safe_component(dataset_version_id, "dataset_version_id")
        upload = _safe_component(upload_id, "upload_id")
        if part_number < 1:
            raise ValueError("part_number must be positive")
        return self.root / "uploads" / tenant / version / upload / f"part-{part_number:05d}"

    async def put_upload_part(
        self,
        *,
        tenant_id: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterable[bytes],
    ) -> BlobInfo:
        destination = self._upload_part_path(tenant_id, dataset_version_id, upload_id, part_number)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            async with await anyio.open_file(temporary, "wb") as handle:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload chunks must be bytes")
                    if not chunk:
                        continue
                    if (
                        self._max_upload_part_bytes is not None
                        and size + len(chunk) > self._max_upload_part_bytes
                    ):
                        raise BlobSizeLimitExceeded(
                            "upload part exceeds the configured service limit"
                        )
                    digest.update(chunk)
                    size += len(chunk)
                    await handle.write(chunk)
                await handle.flush()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        value = digest.hexdigest()
        return BlobInfo(
            key=self._relative_key(destination),
            size_bytes=size,
            sha256=value,
            etag=_etag(value),
        )

    async def finalize_upload(
        self,
        *,
        tenant_id: str,
        dataset_version_id: str,
        upload_id: str,
        parts: Iterable[dict[str, Any]],
        expected_size: int,
        expected_sha256: str,
    ) -> BlobInfo:
        supplied = sorted(parts, key=lambda item: int(item["part_number"]))
        if not supplied or [int(item["part_number"]) for item in supplied] != list(
            range(1, len(supplied) + 1)
        ):
            raise BlobIntegrityError("upload parts must be contiguous and start at one")

        tenant = _safe_component(tenant_id, "tenant_id")
        version = _safe_component(dataset_version_id, "dataset_version_id")
        destination = self.root / "datasets" / tenant / version / "source"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            current = await anyio.to_thread.run_sync(self._hash_file, destination)
            if current.size_bytes == expected_size and current.sha256 == expected_sha256:
                return BlobInfo(
                    key=self._relative_key(destination),
                    size_bytes=current.size_bytes,
                    sha256=current.sha256,
                    etag=current.etag,
                )
            raise BlobIntegrityError("an immutable dataset object already exists with other bytes")

        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        total = 0
        try:
            async with await anyio.open_file(temporary, "wb") as target:
                for supplied_part in supplied:
                    part_number = int(supplied_part["part_number"])
                    source = self._upload_part_path(
                        tenant_id, dataset_version_id, upload_id, part_number
                    )
                    if not source.is_file():
                        raise BlobNotFoundError(f"upload part {part_number} is missing")
                    actual_part = await anyio.to_thread.run_sync(self._hash_file, source)
                    if supplied_part.get("etag") != actual_part.etag:
                        raise BlobIntegrityError(f"upload part {part_number} ETag does not match")
                    async with await anyio.open_file(source, "rb") as part_handle:
                        while chunk := await part_handle.read(1024 * 1024):
                            digest.update(chunk)
                            total += len(chunk)
                            await target.write(chunk)
                await target.flush()
            actual_sha = digest.hexdigest()
            if total != expected_size:
                raise BlobIntegrityError(
                    f"uploaded size is {total} bytes, expected {expected_size} bytes"
                )
            if not hmac.compare_digest(actual_sha, expected_sha256):
                raise BlobIntegrityError("uploaded SHA-256 does not match the declaration")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

        return BlobInfo(
            key=self._relative_key(destination),
            size_bytes=total,
            sha256=expected_sha256,
            etag=_etag(expected_sha256),
        )

    async def put_artifact(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
        content: bytes,
    ) -> BlobInfo:
        tenant = _safe_component(tenant_id, "tenant_id")
        run = _safe_component(run_id, "run_id")
        artifact = _safe_component(artifact_id, "artifact_id")
        destination = self.root / "artifacts" / tenant / run / artifact
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        if destination.exists():
            current = await anyio.to_thread.run_sync(self._hash_file, destination)
            if current.sha256 != digest:
                raise BlobIntegrityError("an immutable artifact already exists with other bytes")
            return BlobInfo(
                key=self._relative_key(destination),
                size_bytes=current.size_bytes,
                sha256=current.sha256,
                etag=current.etag,
            )
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            async with await anyio.open_file(temporary, "wb") as handle:
                await handle.write(content)
                await handle.flush()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return BlobInfo(
            key=self._relative_key(destination),
            size_bytes=len(content),
            sha256=digest,
            etag=_etag(digest),
        )

    async def delete_key(self, key: str) -> bool:
        try:
            path = self.path_for_key(key)
        except BlobNotFoundError:
            return False
        await anyio.to_thread.run_sync(path.unlink)
        return True

    async def delete_dataset_version(self, *, tenant_id: str, dataset_version_id: str) -> None:
        tenant = _safe_component(tenant_id, "tenant_id")
        version = _safe_component(dataset_version_id, "dataset_version_id")
        directories = (
            self.root / "uploads" / tenant / version,
            self.root / "datasets" / tenant / version,
        )
        for directory in directories:
            if directory.exists():
                await anyio.to_thread.run_sync(shutil.rmtree, directory)

    def path_for_key(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise BlobNotFoundError("blob key escapes the configured root") from error
        if not candidate.is_file():
            raise BlobNotFoundError("blob does not exist")
        return candidate

    def create_download_token(
        self, *, artifact_id: str, tenant_id: str, etag: str, expires_at: int
    ) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "ticket_id": f"tkt_{uuid4().hex}",
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "etag": etag,
                "exp": expires_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._ticket_secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")

    def verify_download_token(self, token: str) -> dict[str, Any]:
        try:
            padded = token + "=" * (-len(token) % 4)
            combined = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(combined) <= 32:
                raise ValueError("token is too short")
            payload, supplied_signature = combined[:-32], combined[-32:]
            expected_signature = hmac.new(self._ticket_secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature mismatch")
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get("v") != 1:
                raise ValueError("unsupported ticket")
            if not isinstance(value.get("exp"), int):
                raise ValueError("ticket expiry is invalid")
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise InvalidTicketError("download ticket is invalid") from error
        if value["exp"] < int(time()):
            raise ExpiredTicketError("download ticket has expired")
        return value

    def _relative_key(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @staticmethod
    def _hash_file(path: Path) -> BlobInfo:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        value = digest.hexdigest()
        return BlobInfo(key="", size_bytes=size, sha256=value, etag=_etag(value))


class SyntheticBlobStore:
    """Metadata-only adapter retained for isolated M1 contract tests."""

    durable = False

    def upload_url(
        self,
        public_base_url: str,
        dataset_version_id: str,
        upload_id: str,
        part_number: int,
    ) -> str:
        return f"https://uploads.invalid/{dataset_version_id}/{upload_id}/{part_number}"

    async def put_upload_part(self, **_kwargs: Any) -> BlobInfo:
        raise BlobStoreError("the synthetic blob store does not accept bytes")

    async def finalize_upload(
        self,
        *,
        dataset_version_id: str,
        expected_size: int,
        expected_sha256: str,
        **_kwargs: Any,
    ) -> BlobInfo:
        return BlobInfo(
            key=f"synthetic/{dataset_version_id}",
            size_bytes=expected_size,
            sha256=expected_sha256,
            etag=_etag(expected_sha256),
        )

    async def put_artifact(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
        content: bytes,
    ) -> BlobInfo:
        digest = hashlib.sha256(content).hexdigest()
        return BlobInfo(
            key=f"synthetic/{tenant_id}/{run_id}/{artifact_id}",
            size_bytes=len(content),
            sha256=digest,
            etag=_etag(digest),
        )

    async def delete_key(self, key: str) -> bool:
        return False

    async def delete_dataset_version(self, *, tenant_id: str, dataset_version_id: str) -> None:
        return None

    def path_for_key(self, key: str) -> Path:
        raise BlobNotFoundError(f"synthetic blob {key!r} has no bytes")

    def create_download_token(
        self, *, artifact_id: str, tenant_id: str, etag: str, expires_at: int
    ) -> str:
        return f"synthetic-{artifact_id}"

    def verify_download_token(self, token: str) -> dict[str, Any]:
        raise InvalidTicketError(f"synthetic ticket {token!r} cannot be downloaded")
