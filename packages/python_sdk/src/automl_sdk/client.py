"""Synchronous HTTP client for the managed AutoML public API.

The client supports both finite JSON replay and resumable SSE consumption and
never imports server-side packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, MutableSet, Sequence
from pathlib import Path
from typing import Any

import httpx

from .exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    CollectionCursorExpiredError,
    CommandFailedError,
    ConflictError,
    EventCursorExpiredError,
    GoneError,
    NotFoundError,
    PreconditionFailedError,
    ProtocolError,
    RateLimitError,
    RunTerminalError,
    ServerError,
    TransportError,
    ValidationError,
    WaitTimeoutError,
)

JSONDict = dict[str, Any]

_TERMINAL_EVENT_TYPES = {
    "run.completed.v1",
    "run.failed.v1",
    "run.canceled.v1",
    "run.expired.v1",
}
_COMMAND_TERMINAL = {"SUCCEEDED", "FAILED"}
_REVISION_ETAG = re.compile(r'^"[1-9][0-9]*"$')
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_TRANSFER_CHUNK_SIZE = 64 * 1024
_DATASET_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".parquet": "application/vnd.apache.parquet",
    ".pq": "application/vnd.apache.parquet",
}


class AutoMLClient:
    """A synchronous, dependency-light client for Managed AutoML.

    Mutating methods generate an ``Idempotency-Key`` unless one is supplied.
    Transport-level retries reuse the same key. Event helpers de-duplicate
    at-least-once events by ``event_id``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | Callable[[], str] | None = None,
        api_key: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        max_transport_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url must not be empty")
        if token and api_key:
            raise ValueError("token and api_key are aliases; provide only one")
        if max_transport_retries < 0:
            raise ValueError("max_transport_retries must be non-negative")
        if http_client is not None and transport is not None:
            raise ValueError("transport cannot be combined with http_client")

        self._base_url = base_url.rstrip("/")
        self._max_transport_retries = max_transport_retries
        self._sleep = sleep
        self._clock = clock
        self._default_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "automl-python-sdk/0.7.0",
        }
        self._token_provider = token if callable(token) else None
        credential = api_key or (token if isinstance(token, str) else None)
        if credential:
            self._default_headers["Authorization"] = f"Bearer {credential}"
        if headers:
            self._default_headers.update(headers)

        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout, transport=transport)
        self._last_response: httpx.Response | None = None

    def __enter__(self) -> AutoMLClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the internally owned HTTP connection pool."""

        if self._owns_client:
            self._client.close()

    @property
    def last_response_headers(self) -> Mapping[str, str]:
        """Headers from the latest completed API request, including ETag/Retry-After."""

        return {} if self._last_response is None else self._last_response.headers

    # External Agent integration --------------------------------------
    def get_agent_manifest(self) -> JSONDict:
        """Return the machine-readable boundary for an external Agent platform."""

        response = self._request("GET", "/v1/agent/manifest", expected={200})
        return self._json_object(response)

    def list_backends(self, *, available_only: bool = False) -> list[JSONDict]:
        """Return registered execution backends from the Agent manifest.

        ``available_only`` keeps only adapters whose package, license, model
        weights, and runtime conditions are ready in this service instance. A
        listed backend is not necessarily production eligible; callers must
        inspect ``production_eligible`` separately.
        """

        manifest = self.get_agent_manifest()
        raw_backends = manifest.get("backends")
        if not isinstance(raw_backends, list):
            raise ProtocolError("Agent manifest backends must be an array")
        backends: list[JSONDict] = []
        for index, raw_backend in enumerate(raw_backends):
            if not isinstance(raw_backend, Mapping):
                raise ProtocolError(f"Agent manifest backends[{index}] must be an object")
            backend = dict(raw_backend)
            _required_string(backend, "backend_id", f"Agent manifest backends[{index}]")
            available = backend.get("available")
            if not isinstance(available, bool):
                raise ProtocolError(f"Agent manifest backends[{index}].available must be a boolean")
            if not available_only or available:
                backends.append(backend)
        return backends

    def get_agent_tool_openapi(self) -> str:
        """Return the active OpenAPI YAML exposed for external Agent tools."""

        response = self._request("GET", "/v1/agent/tool-openapi.yaml", expected={200})
        return response.text

    def get_agent_context(
        self,
        run_id: str,
        *,
        output_limit: int = 20,
        if_none_match: str | None = None,
    ) -> JSONDict | None:
        """Read the bounded, non-row-level context exposed for one Run."""

        if (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or not 1 <= output_limit <= 100
        ):
            raise ValueError("output_limit must be an integer from 1 to 100")
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/agent-context",
            expected={200, 304},
            headers=headers,
            params={"output_limit": output_limit},
        )
        return None if response.status_code == 304 else self._json_object(response)

    def list_agent_actions(self, run_id: str) -> JSONDict:
        """List state-scoped references to the API's canonical command endpoints."""

        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/agent-actions",
            expected={200},
        )
        return self._json_object(response)

    # Dataset lifecycle -------------------------------------------------
    def create_dataset(
        self,
        request: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> JSONDict:
        body = _merge_payload(request, fields)
        response = self._request(
            "POST",
            "/v1/datasets",
            expected={201},
            headers=self._idempotency_headers(idempotency_key),
            json=body,
        )
        return self._json_object(response)

    create_dataset_upload = create_dataset

    def sign_upload_parts(
        self,
        dataset_version_id: str,
        *,
        upload_id: str,
        part_numbers: Sequence[int],
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/dataset-versions/{dataset_version_id}/upload-parts:sign",
            expected={200},
            headers=self._idempotency_headers(idempotency_key),
            json={"upload_id": upload_id, "part_numbers": list(part_numbers)},
        )
        return self._json_object(response)

    def finalize_dataset(
        self,
        dataset_version_id: str,
        request: Mapping[str, Any] | None = None,
        *,
        upload_id: str | None = None,
        parts: Sequence[Mapping[str, Any]] | None = None,
        sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        fields: JSONDict = {}
        if upload_id is not None:
            fields["upload_id"] = upload_id
        if parts is not None:
            fields["parts"] = [dict(part) for part in parts]
        if sha256 is not None:
            fields["sha256"] = sha256
        body = _merge_payload(request, fields)
        response = self._request(
            "POST",
            f"/v1/dataset-versions/{dataset_version_id}:finalize",
            expected={202},
            headers=self._idempotency_headers(idempotency_key),
            json=body,
        )
        return self._json_object(response)

    finalize_dataset_version = finalize_dataset

    def get_dataset_version(
        self,
        dataset_version_id: str,
        *,
        if_none_match: str | None = None,
    ) -> JSONDict | None:
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        response = self._request(
            "GET",
            f"/v1/dataset-versions/{dataset_version_id}",
            expected={200, 304},
            headers=headers,
        )
        return None if response.status_code == 304 else self._json_object(response)

    def upload_dataset_file(
        self,
        path: str | os.PathLike[str],
        name: str | None = None,
        media_type: str | None = None,
    ) -> JSONDict:
        """Upload one local CSV or Parquet file and finalize its dataset version.

        The initial implementation intentionally supports one upload part. File
        bytes and the SHA-256 digest are streamed from disk instead of buffered
        into memory.
        """

        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        size_bytes = source.stat().st_size
        if size_bytes < 1:
            raise ValueError("dataset file must not be empty")

        dataset_name = source.stem if name is None else name
        if not dataset_name:
            raise ValueError("name must not be empty")
        resolved_media_type = media_type or _DATASET_MEDIA_TYPES.get(source.suffix.lower())
        if resolved_media_type not in set(_DATASET_MEDIA_TYPES.values()):
            raise ValueError("media_type is required for CSV or Parquet dataset files")

        session = self.create_dataset(
            name=dataset_name,
            filename=source.name,
            media_type=resolved_media_type,
            size_bytes=size_bytes,
        )
        dataset_version_id = _required_string(
            session, "dataset_version_id", "dataset upload session"
        )
        upload_id = _required_string(session, "upload_id", "dataset upload session")
        parts = session.get("parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], Mapping):
            raise ProtocolError("dataset upload session must contain exactly one upload part")
        part = parts[0]
        part_number = _positive_int(part.get("part_number"), "upload part_number")
        upload_url = self._resolve_transfer_url(
            _required_string(part, "url", "dataset upload part")
        )
        upload_headers = self._transfer_headers(
            upload_url,
            _optional_string_headers(part, "required_headers", "dataset upload part"),
        )
        upload_headers.setdefault("Content-Type", resolved_media_type)
        upload_headers.setdefault("Content-Length", str(size_bytes))
        response = self._put_file(upload_url, source, headers=upload_headers)
        etag = response.headers.get("ETag")
        if not etag:
            raise ProtocolError("upload response is missing ETag")

        digest, hashed_size = _sha256_file(source)
        if hashed_size != size_bytes:
            raise ProtocolError("dataset file changed while it was being uploaded")
        return self.finalize_dataset(
            dataset_version_id,
            upload_id=upload_id,
            parts=[{"part_number": part_number, "etag": etag}],
            sha256=digest,
        )

    # Run snapshots and events -----------------------------------------
    def create_run(
        self,
        request: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> JSONDict:
        body = _merge_payload(request, fields)
        response = self._request(
            "POST",
            "/v1/runs",
            expected={202},
            headers=self._idempotency_headers(idempotency_key),
            json=body,
        )
        return self._json_object(response)

    def get_run(
        self,
        run_id: str,
        *,
        if_none_match: str | None = None,
    ) -> JSONDict | None:
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        response = self._request(
            "GET",
            f"/v1/runs/{run_id}",
            expected={200, 304},
            headers=headers,
        )
        return None if response.status_code == 304 else self._json_object(response)

    def list_runs(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        statuses: Sequence[str] | None = None,
    ) -> JSONDict:
        params = (
            {"cursor": cursor}
            if cursor is not None
            else _without_none({"limit": limit, "status": _csv(statuses)})
        )
        response = self._request("GET", "/v1/runs", expected={200}, params=params)
        return self._json_object(response)

    def get_run_stages(self, run_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/runs/{run_id}/stages", expected={200})
        return self._json_object(response)

    def list_run_experiments(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> JSONDict:
        """List experiment resources for a Run.

        Version 0.7 exposes this as a compatibility collection and currently
        returns an empty page.
        """

        params = {"cursor": cursor} if cursor is not None else _without_none({"limit": limit})
        response = self._request(
            "GET", f"/v1/runs/{run_id}/experiments", expected={200}, params=params
        )
        return self._json_object(response)

    def get_run_experiment(self, run_id: str, experiment_id: str) -> JSONDict:
        """Read one experiment resource when the service publishes one."""

        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/experiments/{experiment_id}",
            expected={200},
        )
        return self._json_object(response)

    list_experiments = list_run_experiments
    get_experiment = get_run_experiment

    def get_run_events(
        self,
        run_id: str,
        *,
        after_seq: int | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        types: Sequence[str] | None = None,
    ) -> JSONDict:
        """Read one JSON event page.

        ``after_seq`` and filters are used only for the first page. Once the
        server returns ``next_cursor``, send only that opaque cursor.
        """

        params: JSONDict
        if cursor is not None:
            params = {"cursor": cursor}
        else:
            params = _without_none({"after_seq": after_seq, "limit": limit, "types": _csv(types)})
        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/events",
            expected={200},
            headers={"Accept": "application/json"},
            params=_without_none(params),
        )
        return self._json_object(response)

    def iter_run_events(
        self,
        run_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
        types: Sequence[str] | None = None,
        seen_event_ids: MutableSet[str] | None = None,
        recover_expired: bool = False,
        on_recovery: Callable[[JSONDict], None] | None = None,
    ) -> Iterator[JSONDict]:
        """Iterate the currently available JSON backlog, de-duplicated by ID.

        This is the SDK's explicit SSE fallback. Event-history loss raises by
        default. Set ``recover_expired=True`` to resume from a fresh snapshot;
        ``on_recovery`` then receives that authoritative recovery boundary.
        """

        seen = seen_event_ids if seen_event_ids is not None else set()
        events, _, _ = self._collect_event_batch(
            run_id,
            after_seq=after_seq,
            limit=limit,
            types=types,
            seen_event_ids=seen,
            recover_expired=recover_expired,
            on_recovery=on_recovery,
        )
        yield from events

    iter_events = iter_run_events

    def stream_run_events(
        self,
        run_id: str,
        *,
        after_seq: int | None = None,
        types: Sequence[str] | None = None,
        seen_event_ids: MutableSet[str] | None = None,
        recover_expired: bool = False,
        on_recovery: Callable[[JSONDict], None] | None = None,
        max_reconnects: int | None = None,
        reconnect_delay: float = 0.25,
    ) -> Iterator[JSONDict]:
        """Consume SSE with Last-Event-ID resume and event-id de-duplication.

        When ``after_seq`` is omitted, a RunSnapshot establishes the initial
        continuous boundary before the stream is opened. Event-history loss
        raises unless the caller explicitly enables snapshot recovery.
        """

        if max_reconnects is not None and max_reconnects < 0:
            raise ValueError("max_reconnects must be non-negative or None")
        if reconnect_delay < 0:
            raise ValueError("reconnect_delay must be non-negative")
        seen = seen_event_ids if seen_event_ids is not None else set()
        if after_seq is None:
            snapshot = self._require_run(run_id)
            current_seq = _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq")
            if _is_terminal(snapshot):
                return
        else:
            if isinstance(after_seq, bool) or after_seq < 0:
                raise ValueError("after_seq must be a non-negative integer")
            current_seq = after_seq

        reconnects = 0
        while True:
            headers = self._request_headers()
            headers["Accept"] = "text/event-stream"
            headers["Last-Event-ID"] = str(current_seq)
            params = _without_none({"types": _csv(types)})
            try:
                with self._client.stream(
                    "GET",
                    f"{self._base_url}/v1/runs/{run_id}/events",
                    headers=headers,
                    params=params,
                ) as response:
                    self._last_response = response
                    if response.status_code != 200:
                        response.read()
                        self._raise_api_error(response)
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        raise ProtocolError("Event stream response is not text/event-stream")

                    event_id: str | None = None
                    event_name: str | None = None
                    data_lines: list[str] = []
                    for line in response.iter_lines():
                        if line == "":
                            if not data_lines:
                                event_id, event_name = None, None
                                continue
                            event = _decode_sse_event(event_id, event_name, data_lines)
                            event_id, event_name, data_lines = None, None, []
                            seq = _non_negative_int(event.get("seq"), "event seq")
                            if seq < 1:
                                raise ProtocolError("SSE event seq must be positive")
                            current_seq = max(current_seq, seq)
                            resource_id = _required_string(event, "event_id", "event")
                            if resource_id in seen:
                                continue
                            seen.add(resource_id)
                            yield event
                            if event.get("type") in _TERMINAL_EVENT_TYPES:
                                return
                            continue
                        if line.startswith(":"):
                            continue
                        field, separator, value = line.partition(":")
                        if separator and value.startswith(" "):
                            value = value[1:]
                        if field == "id":
                            event_id = value
                        elif field == "event":
                            event_name = value
                        elif field == "data":
                            data_lines.append(value)
            except EventCursorExpiredError:
                if not recover_expired:
                    raise
                snapshot = self._require_run(run_id)
                current_seq = _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq")
                if on_recovery is not None:
                    on_recovery(snapshot)
                if _is_terminal(snapshot):
                    return
            except httpx.TransportError as error:
                if max_reconnects is not None and reconnects >= max_reconnects:
                    raise TransportError("SSE reconnection budget was exhausted") from error

            snapshot = self._require_run(run_id)
            if _is_terminal(snapshot):
                return
            if max_reconnects is not None and reconnects >= max_reconnects:
                raise TransportError("SSE ended before the Run reached a terminal state")
            reconnects += 1
            self._sleep(reconnect_delay)

    stream_events = stream_run_events

    def replay_events(self, run_id: str, **kwargs: Any) -> list[JSONDict]:
        """Return the currently replayable JSON event backlog as a list."""

        return list(self.iter_run_events(run_id, **kwargs))

    replay_run_events = replay_events

    # Outputs -----------------------------------------------------------
    def list_outputs(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        types: Sequence[str] | None = None,
        phases: Sequence[str] | None = None,
        state: str | None = None,
    ) -> JSONDict:
        params = (
            {"cursor": cursor}
            if cursor is not None
            else _without_none(
                {
                    "limit": limit,
                    "type": _csv(types),
                    "phase": _csv(phases),
                    "state": state,
                }
            )
        )
        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/outputs",
            expected={200},
            params=params,
        )
        return self._json_object(response)

    def iter_outputs(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        types: Sequence[str] | None = None,
        phases: Sequence[str] | None = None,
        state: str | None = None,
        max_cursor_restarts: int = 3,
    ) -> Iterator[JSONDict]:
        """Iterate a stable output window and recover expired page cursors.

        Collection-cursor recovery restarts the original filtered query and
        suppresses already yielded ``output_id`` values.
        """

        if max_cursor_restarts < 0:
            raise ValueError("max_cursor_restarts must be non-negative")
        seen: set[str] = set()
        cursor: str | None = None
        restarts = 0
        prior_cursors: set[str] = set()
        while True:
            try:
                page = self.list_outputs(
                    run_id,
                    cursor=cursor,
                    limit=limit,
                    types=types,
                    phases=phases,
                    state=state,
                )
            except CollectionCursorExpiredError:
                if restarts >= max_cursor_restarts:
                    raise
                restarts += 1
                cursor = None
                prior_cursors.clear()
                continue

            for item in _page_items(page, "outputs"):
                output_id = _required_string(item, "output_id", "output")
                if output_id in seen:
                    continue
                seen.add(output_id)
                yield item

            page_meta = _required_mapping(page, "page", "output page")
            next_cursor = page_meta.get("next_cursor")
            has_more = bool(page_meta.get("has_more", next_cursor is not None))
            if not has_more or not next_cursor:
                return
            if not isinstance(next_cursor, str) or next_cursor in prior_cursors:
                raise ProtocolError("Output pagination returned a repeated or invalid cursor")
            prior_cursors.add(next_cursor)
            cursor = next_cursor

    def get_output(self, run_id: str, output_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/runs/{run_id}/outputs/{output_id}", expected={200})
        return self._json_object(response)

    # Human decisions ---------------------------------------------------
    def list_decision_packets(
        self,
        run_id: str,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> JSONDict:
        params = (
            {"cursor": cursor}
            if cursor is not None
            else _without_none({"status": status, "limit": limit})
        )
        response = self._request(
            "GET",
            f"/v1/runs/{run_id}/decision-packets",
            expected={200},
            params=params,
        )
        return self._json_object(response)

    def answer_decision_packet(
        self,
        run_id: str,
        wait_set_id: str,
        answers: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        wait_set_revision: int | str,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        headers = self._idempotency_headers(idempotency_key)
        headers["If-Match"] = _revision_etag(wait_set_revision)
        response = self._request(
            "POST",
            f"/v1/runs/{run_id}/decision-packets/{wait_set_id}:answer",
            expected={202},
            headers=headers,
            json={"answers": _normalize_answers(answers)},
        )
        return self._json_object(response)

    def pause_run(
        self,
        run_id: str,
        *,
        run_revision: int | str,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        headers = self._idempotency_headers(idempotency_key)
        headers["If-Match"] = _revision_etag(run_revision)
        response = self._request(
            "POST", f"/v1/runs/{run_id}:pause", expected={202}, headers=headers
        )
        return self._json_object(response)

    def resume_run(
        self,
        run_id: str,
        *,
        run_revision: int | str,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        headers = self._idempotency_headers(idempotency_key)
        headers["If-Match"] = _revision_etag(run_revision)
        response = self._request(
            "POST", f"/v1/runs/{run_id}:resume", expected={202}, headers=headers
        )
        return self._json_object(response)

    def cancel_run(
        self,
        run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/runs/{run_id}:cancel",
            expected={202},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    pause = pause_run
    resume = resume_run
    cancel = cancel_run

    # Production control plane ----------------------------------------
    def list_approvals(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> JSONDict:
        params = {"cursor": cursor} if cursor is not None else _without_none({"limit": limit})
        response = self._request(
            "GET", f"/v1/runs/{run_id}/approvals", expected={200}, params=params
        )
        return self._json_object(response)

    def decide_approval(
        self,
        run_id: str,
        approval_id: str,
        *,
        decision: str,
        reason: str,
        evidence_version: int | str,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        headers = self._idempotency_headers(idempotency_key)
        headers["If-Match"] = _revision_etag(evidence_version)
        evidence_version_number = _revision_int(evidence_version)
        response = self._request(
            "POST",
            f"/v1/runs/{run_id}/approvals/{approval_id}:decide",
            expected={202},
            headers=headers,
            json={
                "decision": decision,
                "reason": reason,
                "evidence_version": evidence_version_number,
            },
        )
        return self._json_object(response)

    def get_model_candidate(self, model_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/models/{model_id}", expected={200})
        return self._json_object(response)

    get_model = get_model_candidate

    def create_webhook_endpoint(
        self,
        *,
        url: str,
        event_types: Sequence[str],
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            "/v1/webhook-endpoints",
            expected={201},
            headers=self._idempotency_headers(idempotency_key),
            json=_without_none(
                {
                    "url": url,
                    "event_types": list(event_types),
                    "description": description,
                }
            ),
        )
        return self._json_object(response)

    def list_webhook_endpoints(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> JSONDict:
        params = {"cursor": cursor} if cursor is not None else _without_none({"limit": limit})
        response = self._request("GET", "/v1/webhook-endpoints", expected={200}, params=params)
        return self._json_object(response)

    def get_webhook_endpoint(self, webhook_endpoint_id: str) -> JSONDict:
        response = self._request(
            "GET", f"/v1/webhook-endpoints/{webhook_endpoint_id}", expected={200}
        )
        return self._json_object(response)

    def delete_webhook_endpoint(
        self,
        webhook_endpoint_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self._request(
            "DELETE",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}",
            expected={204},
            headers=self._idempotency_headers(idempotency_key),
        )

    def rotate_webhook_endpoint_secret(
        self,
        webhook_endpoint_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}:rotate-secret",
            expected={201},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    def enable_webhook_endpoint(
        self,
        webhook_endpoint_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}:enable",
            expected={200},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    def list_webhook_deliveries(
        self,
        webhook_endpoint_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        statuses: Sequence[str] | None = None,
    ) -> JSONDict:
        params = (
            {"cursor": cursor}
            if cursor is not None
            else _without_none({"limit": limit, "status": _csv(statuses)})
        )
        response = self._request(
            "GET",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries",
            expected={200},
            params=params,
        )
        return self._json_object(response)

    def get_webhook_delivery(self, webhook_endpoint_id: str, delivery_id: str) -> JSONDict:
        response = self._request(
            "GET",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}",
            expected={200},
        )
        return self._json_object(response)

    def redeliver_webhook_delivery(
        self,
        webhook_endpoint_id: str,
        delivery_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/webhook-endpoints/{webhook_endpoint_id}/deliveries/{delivery_id}:redeliver",
            expected={202},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    def delete_dataset(
        self,
        dataset_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "DELETE",
            f"/v1/datasets/{dataset_id}",
            expected={202},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    def get_deletion_job(self, deletion_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/deletions/{deletion_id}", expected={200})
        return self._json_object(response)

    get_deletion = get_deletion_job

    # Commands and terminal results ------------------------------------
    def get_command(self, command_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/commands/{command_id}", expected={200})
        return self._json_object(response)

    def wait_for_command(
        self,
        command_id: str,
        *,
        timeout: float | None = 300.0,
        poll_interval: float = 1.0,
    ) -> JSONDict:
        deadline = self._make_deadline(timeout)
        while True:
            command = self.get_command(command_id)
            status = command.get("status")
            if status == "SUCCEEDED":
                return command
            if status == "FAILED":
                raise CommandFailedError(command)
            if status not in {"ACCEPTED", "RUNNING"}:
                raise ProtocolError(f"Unknown command status: {status!r}")
            self._poll_sleep(deadline, poll_interval, "command", command_id, timeout)

    poll_command = wait_for_command

    def get_run_result(self, run_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/runs/{run_id}/result", expected={200})
        return self._json_object(response)

    get_result = get_run_result

    def get_artifact(self, artifact_id: str) -> JSONDict:
        response = self._request("GET", f"/v1/artifacts/{artifact_id}", expected={200})
        return self._json_object(response)

    def create_artifact_download_ticket(
        self,
        artifact_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> JSONDict:
        response = self._request(
            "POST",
            f"/v1/artifacts/{artifact_id}:download",
            expected={201},
            headers=self._idempotency_headers(idempotency_key),
        )
        return self._json_object(response)

    create_download_ticket = create_artifact_download_ticket

    def download_artifact_file(
        self,
        artifact_id: str,
        destination: str | os.PathLike[str],
        resume: bool = True,
    ) -> Path:
        """Download an artifact, verify it, and atomically replace ``destination``.

        Interrupted ranged transfers retain a sibling ``.part`` file when
        ``resume`` is enabled. Protocol or integrity failures remove that partial
        file and never modify an existing destination.
        """

        if not isinstance(resume, bool):
            raise ValueError("resume must be a boolean")
        target = Path(destination)
        partial = target.with_name(f"{target.name}.part")
        ticket = self.create_artifact_download_ticket(artifact_id)
        if _required_string(ticket, "artifact_id", "download ticket") != artifact_id:
            raise ProtocolError("download ticket references a different artifact")
        download_url = self._resolve_transfer_url(
            _required_string(ticket, "url", "download ticket")
        )
        expected_etag = _required_string(ticket, "etag", "download ticket")
        expected_sha256 = _required_string(ticket, "sha256", "download ticket")
        if not _SHA256.fullmatch(expected_sha256):
            raise ProtocolError("download ticket contains an invalid sha256")
        expected_size = _non_negative_int(ticket.get("size_bytes"), "download size_bytes")
        supports_range = ticket.get("supports_range")
        if not isinstance(supports_range, bool):
            raise ProtocolError("download ticket is missing boolean supports_range")
        required_headers = _optional_string_headers(ticket, "required_headers", "download ticket")

        if not resume:
            _unlink_if_exists(partial)
        elif partial.exists() and partial.stat().st_size > expected_size:
            _unlink_if_exists(partial)

        if partial.exists() and partial.stat().st_size == expected_size:
            digest, downloaded_size = _sha256_file(partial)
            if downloaded_size == expected_size and digest == expected_sha256:
                os.replace(partial, target)
                return target
            _unlink_if_exists(partial)

        try:
            self._download_to_partial(
                download_url,
                partial,
                required_headers=required_headers,
                expected_etag=expected_etag,
                expected_size=expected_size,
                supports_range=supports_range,
                resume=resume,
            )
            digest, downloaded_size = _sha256_file(partial)
            if downloaded_size != expected_size:
                raise ProtocolError(
                    f"downloaded artifact size {downloaded_size} does not match {expected_size}"
                )
            if digest != expected_sha256:
                raise ProtocolError("downloaded artifact SHA-256 does not match its ticket")
            os.replace(partial, target)
            return target
        except TransportError:
            if not resume:
                _unlink_if_exists(partial)
            raise
        except Exception:
            _unlink_if_exists(partial)
            raise

    # High-level managed workflow --------------------------------------
    def wait_for_question(
        self,
        run_id: str,
        *,
        timeout: float | None = 3600.0,
        poll_interval: float = 1.0,
    ) -> JSONDict:
        """Wait until a blocking OPEN DecisionPacket is available."""

        deadline = self._make_deadline(timeout)
        snapshot = self._require_run(run_id)
        after_seq = _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq")
        seen: set[str] = set()

        while True:
            packet = self._first_open_packet(run_id)
            if packet is not None:
                return packet
            if _is_terminal(snapshot):
                raise RunTerminalError(snapshot)

            _, high_watermark, recovered = self._collect_event_batch(
                run_id,
                after_seq=after_seq,
                seen_event_ids=seen,
                recover_expired=True,
            )
            if recovered is not None:
                snapshot = recovered
            else:
                snapshot = self._require_run(run_id)
            after_seq = max(
                high_watermark,
                _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq"),
            )

            packet = self._first_open_packet(run_id)
            if packet is not None:
                return packet
            if _is_terminal(snapshot):
                raise RunTerminalError(snapshot)
            self._poll_sleep(deadline, poll_interval, "question", run_id, timeout)

    def answer_and_wait(
        self,
        run_id: str,
        decision_packet: Mapping[str, Any] | str,
        answers: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        wait_set_revision: int | str | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = 300.0,
        poll_interval: float = 1.0,
    ) -> JSONDict:
        """Answer a wait-set and wait until the answer command is applied."""

        if isinstance(decision_packet, Mapping):
            wait_set_id = _required_string(decision_packet, "wait_set_id", "decision packet")
            packet_revision = decision_packet.get("wait_set_revision")
            if wait_set_revision is not None and str(wait_set_revision).strip('"') != str(
                packet_revision
            ).strip('"'):
                raise ValueError("wait_set_revision does not match the decision packet")
            revision = packet_revision if wait_set_revision is None else wait_set_revision
        else:
            wait_set_id = decision_packet
            revision = wait_set_revision
        if revision is None:
            raise ValueError("wait_set_revision is required when no packet revision is supplied")

        receipt = self.answer_decision_packet(
            run_id,
            wait_set_id,
            answers,
            wait_set_revision=revision,
            idempotency_key=idempotency_key,
        )
        command_id = _required_string(receipt, "command_id", "command receipt")
        if receipt.get("status") == "SUCCEEDED":
            return receipt
        if receipt.get("status") == "FAILED":
            raise CommandFailedError(receipt)
        return self.wait_for_command(command_id, timeout=timeout, poll_interval=poll_interval)

    def wait_for_result(
        self,
        run_id: str,
        *,
        timeout: float | None = 86400.0,
        poll_interval: float = 2.0,
    ) -> JSONDict:
        """Wait for any terminal Run outcome and return its RunResult."""

        deadline = self._make_deadline(timeout)
        snapshot = self._require_run(run_id)
        if _is_terminal(snapshot):
            return self.get_run_result(run_id)

        after_seq = _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq")
        seen: set[str] = set()
        while True:
            events, high_watermark, recovered = self._collect_event_batch(
                run_id,
                after_seq=after_seq,
                seen_event_ids=seen,
                recover_expired=True,
            )
            if recovered is not None:
                snapshot = recovered
            elif any(event.get("type") in _TERMINAL_EVENT_TYPES for event in events):
                snapshot = self._require_run(run_id)
            else:
                snapshot = self._require_run(run_id)
            after_seq = max(
                high_watermark,
                _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq"),
            )
            if _is_terminal(snapshot):
                return self.get_run_result(run_id)
            self._poll_sleep(deadline, poll_interval, "result", run_id, timeout)

    # Internal transport and replay ------------------------------------
    def _put_file(
        self,
        url: httpx.URL,
        source: Path,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        for attempt in range(self._max_transport_retries + 1):
            try:
                response = self._client.request(
                    "PUT",
                    url,
                    headers=headers,
                    content=_iter_file_chunks(source),
                )
                self._last_response = response
                break
            except httpx.TransportError as error:
                if attempt >= self._max_transport_retries:
                    raise TransportError(f"upload failed after {attempt + 1} attempts") from error
                self._sleep(min(0.25 * (2**attempt), 2.0))
        else:  # pragma: no cover - the loop always returns or raises
            raise AssertionError("unreachable upload retry state")

        if response.status_code not in {200, 201, 204}:
            self._raise_api_error(response)
        return response

    def _download_to_partial(
        self,
        url: httpx.URL,
        partial: Path,
        *,
        required_headers: Mapping[str, str],
        expected_etag: str,
        expected_size: int,
        supports_range: bool,
        resume: bool,
    ) -> None:
        for attempt in range(self._max_transport_retries + 1):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > expected_size:
                _unlink_if_exists(partial)
                offset = 0
            if offset == expected_size and partial.exists():
                return
            ranged = bool(offset and resume and supports_range)
            if offset and not ranged:
                _unlink_if_exists(partial)
                offset = 0

            headers = self._transfer_headers(url, required_headers)
            headers.setdefault("Accept", "application/octet-stream")
            if ranged:
                headers["Range"] = f"bytes={offset}-"

            try:
                with self._client.stream("GET", url, headers=headers) as response:
                    self._last_response = response
                    self._validate_download_response(
                        response,
                        ranged=ranged,
                        offset=offset,
                        expected_etag=expected_etag,
                        expected_size=expected_size,
                    )
                    downloaded_size = offset
                    with partial.open("ab" if ranged else "wb") as output:
                        for chunk in response.iter_bytes(chunk_size=_TRANSFER_CHUNK_SIZE):
                            if not chunk:
                                continue
                            downloaded_size += len(chunk)
                            if downloaded_size > expected_size:
                                raise ProtocolError("download response exceeds the ticket size")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if downloaded_size != expected_size:
                        raise ProtocolError(
                            "download response ended before the ticket size was reached"
                        )
                return
            except httpx.TransportError as error:
                if not (resume and supports_range):
                    _unlink_if_exists(partial)
                if attempt >= self._max_transport_retries:
                    raise TransportError(f"download failed after {attempt + 1} attempts") from error
                self._sleep(min(0.25 * (2**attempt), 2.0))

        raise AssertionError("unreachable download retry state")  # pragma: no cover

    def _validate_download_response(
        self,
        response: httpx.Response,
        *,
        ranged: bool,
        offset: int,
        expected_etag: str,
        expected_size: int,
    ) -> None:
        expected_status = 206 if ranged else 200
        if response.status_code != expected_status:
            response.read()
            if response.status_code >= 400:
                self._raise_api_error(response)
            raise ProtocolError(
                f"download response status {response.status_code} is not {expected_status}"
            )

        if response.headers.get("ETag") != expected_etag:
            raise ProtocolError("download response ETag does not match its ticket")
        raw_length = response.headers.get("Content-Length")
        expected_length = expected_size - offset
        if raw_length is None or not raw_length.isdigit() or int(raw_length) != expected_length:
            raise ProtocolError("download response Content-Length does not match its ticket")
        content_encoding = response.headers.get("Content-Encoding")
        if content_encoding and content_encoding.lower() != "identity":
            raise ProtocolError("encoded artifact downloads are not supported")

        if ranged:
            raw_range = response.headers.get("Content-Range", "")
            match = _CONTENT_RANGE.fullmatch(raw_range)
            expected_end = expected_size - 1
            if match is None or tuple(map(int, match.groups())) != (
                offset,
                expected_end,
                expected_size,
            ):
                raise ProtocolError("download response Content-Range does not match the request")

    def _resolve_transfer_url(self, value: str) -> httpx.URL:
        try:
            candidate = httpx.URL(value)
            if candidate.is_absolute_url or candidate.scheme:
                resolved = candidate
            else:
                resolved = httpx.URL(f"{self._base_url}/").join(candidate)
        except (httpx.InvalidURL, ValueError) as error:
            raise ProtocolError("transfer URL is invalid") from error
        if resolved.scheme not in {"http", "https"} or not resolved.host:
            raise ProtocolError("transfer URL must use HTTP or HTTPS")
        return resolved

    def _transfer_headers(
        self,
        url: httpx.URL,
        required_headers: Mapping[str, str],
    ) -> httpx.Headers:
        try:
            base = httpx.URL(self._base_url)
        except (httpx.InvalidURL, ValueError) as error:
            raise ProtocolError("base_url is invalid") from error
        same_origin = url.scheme == base.scheme and url.host == base.host and url.port == base.port
        base_headers = self._request_headers()
        headers = httpx.Headers(base_headers if same_origin else {})
        if not same_origin and "User-Agent" in base_headers:
            headers["User-Agent"] = base_headers["User-Agent"]
        headers.update(required_headers)
        return headers

    def _collect_event_batch(
        self,
        run_id: str,
        *,
        after_seq: int | None,
        limit: int | None = None,
        types: Sequence[str] | None = None,
        seen_event_ids: MutableSet[str],
        recover_expired: bool,
        on_recovery: Callable[[JSONDict], None] | None = None,
    ) -> tuple[list[JSONDict], int, JSONDict | None]:
        cursor: str | None = None
        prior_cursors: set[str] = set()
        events: list[JSONDict] = []
        high_watermark = after_seq or 0
        while True:
            try:
                page = self.get_run_events(
                    run_id,
                    after_seq=after_seq if cursor is None else None,
                    cursor=cursor,
                    limit=limit,
                    types=types if cursor is None else None,
                )
            except EventCursorExpiredError:
                if not recover_expired:
                    raise
                snapshot = self._require_run(run_id)
                high_watermark = _non_negative_int(snapshot.get("snapshot_seq"), "snapshot_seq")
                if on_recovery is not None:
                    on_recovery(snapshot)
                return events, high_watermark, snapshot

            page_high = _non_negative_int(page.get("high_watermark"), "high_watermark")
            high_watermark = max(high_watermark, page_high)
            for event in _page_items(page, "events"):
                event_id = _required_string(event, "event_id", "event")
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
                events.append(event)

            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                return events, high_watermark, None
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ProtocolError("Event page returned an invalid next_cursor")
            if next_cursor in prior_cursors:
                raise ProtocolError("Event pagination returned a repeated cursor")
            prior_cursors.add(next_cursor)
            cursor = next_cursor

    def _first_open_packet(self, run_id: str) -> JSONDict | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = self.list_decision_packets(
                run_id,
                status="OPEN" if cursor is None else None,
                cursor=cursor,
                limit=100 if cursor is None else None,
            )
            for packet in _page_items(page, "decision packets"):
                if packet.get("status") == "OPEN" and packet.get("blocking", True):
                    return packet
            page_meta = _required_mapping(page, "page", "decision packet page")
            next_cursor = page_meta.get("next_cursor")
            has_more = page_meta.get("has_more")
            if has_more is not True:
                if next_cursor is not None:
                    raise ProtocolError("DecisionPacket page returned a cursor without has_more")
                return None
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ProtocolError("DecisionPacket page is missing a continuation cursor")
            if next_cursor in seen_cursors:
                raise ProtocolError("DecisionPacket pagination returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _require_run(self, run_id: str) -> JSONDict:
        snapshot = self.get_run(run_id)
        if snapshot is None:
            raise ProtocolError("Unconditional GET /runs returned 304")
        return snapshot

    def _idempotency_headers(self, value: str | None) -> dict[str, str]:
        key = value or uuid.uuid4().hex
        if not 16 <= len(key) <= 128:
            raise ValueError("idempotency_key must contain 16 to 128 characters")
        if any(ord(character) < 33 or ord(character) > 126 for character in key):
            raise ValueError("idempotency_key must use visible ASCII characters")
        return {"Idempotency-Key": key}

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        request_headers = self._request_headers()
        if headers:
            request_headers.update(headers)
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_transport_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json,
                )
                self._last_response = response
                if not self._should_retry_response(method, response, request_headers, attempt):
                    break
                self._sleep(self._retry_delay(response, attempt))
            except httpx.TransportError as error:
                if attempt >= self._max_transport_retries:
                    raise TransportError(
                        f"{method} {path} failed after {attempt + 1} attempts"
                    ) from error
                self._sleep(min(0.25 * (2**attempt), 2.0))
        else:  # pragma: no cover - the loop always returns or raises
            raise AssertionError("unreachable transport retry state")

        if response.status_code not in expected:
            self._raise_api_error(response)
        return response

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._default_headers)
        if self._token_provider is not None:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise ValueError("token provider must return a non-empty string")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _should_retry_response(
        self,
        method: str,
        response: httpx.Response,
        headers: Mapping[str, str],
        attempt: int,
    ) -> bool:
        if attempt >= self._max_transport_retries:
            return False
        if response.status_code not in {429, 502, 503, 504}:
            return False
        normalized_method = method.upper()
        if normalized_method in {"GET", "HEAD", "OPTIONS"}:
            return True
        return "Idempotency-Key" in headers

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None and retry_after.isdigit():
            return min(float(retry_after), 30.0)
        return min(0.25 * (2**attempt), 2.0)

    def _raise_api_error(self, response: httpx.Response) -> None:
        try:
            raw_problem = response.json()
        except (ValueError, UnicodeDecodeError):
            raw_problem = {}
        problem = raw_problem if isinstance(raw_problem, Mapping) else {}
        code_value = problem.get("code")
        code = code_value if isinstance(code_value, str) else None
        detail = problem.get("detail")
        message = detail if isinstance(detail, str) and detail else f"HTTP {response.status_code}"

        if response.status_code == 410 and code == "cursor_expired":
            error_type: type[APIError] = EventCursorExpiredError
        elif response.status_code == 410 and code == "page_cursor_expired":
            error_type = CollectionCursorExpiredError
        else:
            error_type = {
                400: BadRequestError,
                401: AuthenticationError,
                403: AuthorizationError,
                404: NotFoundError,
                409: ConflictError,
                410: GoneError,
                412: PreconditionFailedError,
                422: ValidationError,
                429: RateLimitError,
            }.get(response.status_code, ServerError if response.status_code >= 500 else APIError)
        raise error_type(
            message,
            status_code=response.status_code,
            code=code,
            problem=problem,
            response=response,
        )

    @staticmethod
    def _json_object(response: httpx.Response) -> JSONDict:
        try:
            value = response.json()
        except (ValueError, UnicodeDecodeError) as error:
            raise ProtocolError("API response is not valid JSON") from error
        if not isinstance(value, dict):
            raise ProtocolError("API response must be a JSON object")
        return value

    def _make_deadline(self, timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        return self._clock() + timeout

    def _poll_sleep(
        self,
        deadline: float | None,
        poll_interval: float,
        operation: str,
        resource_id: str,
        timeout: float | None,
    ) -> None:
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if deadline is None:
            self._sleep(poll_interval)
            return
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise WaitTimeoutError(operation, resource_id, timeout)
        self._sleep(min(poll_interval, remaining))


def _iter_file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(_TRANSFER_CHUNK_SIZE):
            yield chunk


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_TRANSFER_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _merge_payload(request: Mapping[str, Any] | None, fields: Mapping[str, Any]) -> JSONDict:
    if request is None:
        if not fields:
            raise ValueError("request body must not be empty")
        return dict(fields)
    if fields:
        overlap = set(request).intersection(fields)
        if overlap:
            joined = ", ".join(sorted(overlap))
            raise ValueError(f"request body fields were supplied twice: {joined}")
        return {**request, **fields}
    return dict(request)


def _normalize_answers(
    answers: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[JSONDict]:
    if isinstance(answers, Mapping):
        normalized_mapping = [
            {"question_id": str(question_id), "value": value}
            for question_id, value in answers.items()
        ]
        if not normalized_mapping:
            raise ValueError("answers must not be empty")
        return normalized_mapping
    normalized: list[JSONDict] = []
    for answer in answers:
        if not isinstance(answer, Mapping):
            raise ValueError("each answer must be a mapping")
        if "question_id" not in answer or "value" not in answer:
            raise ValueError("each answer requires question_id and value")
        normalized.append(dict(answer))
    if not normalized:
        raise ValueError("answers must not be empty")
    return normalized


def _revision_etag(revision: int | str) -> str:
    if isinstance(revision, bool):
        raise ValueError("revision must be a positive integer")
    if isinstance(revision, int):
        if revision < 1:
            raise ValueError("revision must be a positive integer")
        return f'"{revision}"'
    value = revision.strip()
    if value.isdigit() and int(value) > 0:
        return f'"{value}"'
    if _REVISION_ETAG.fullmatch(value):
        return value
    raise ValueError('revision must be a positive integer or quoted ETag such as "3"')


def _revision_int(revision: int | str) -> int:
    return int(_revision_etag(revision).strip('"'))


def _without_none(values: Mapping[str, Any]) -> JSONDict:
    return {key: value for key, value in values.items() if value is not None}


def _csv(values: Sequence[str] | None) -> str | None:
    if values is None:
        return None
    if isinstance(values, str):
        if not values:
            raise ValueError("filter value must not be empty")
        return values
    joined = ",".join(values)
    return joined or None


def _required_mapping(value: Mapping[str, Any], key: str, context: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ProtocolError(f"{context} is missing object field {key!r}")
    return item


def _required_string(value: Mapping[str, Any], key: str, context: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ProtocolError(f"{context} is missing string field {key!r}")
    return item


def _optional_string_headers(value: Mapping[str, Any], key: str, context: str) -> dict[str, str]:
    item = value.get(key)
    if item is None:
        return {}
    if not isinstance(item, Mapping):
        raise ProtocolError(f"{context} field {key!r} must be an object")
    headers: dict[str, str] = {}
    for header_name, header_value in item.items():
        if not isinstance(header_name, str) or not header_name or not isinstance(header_value, str):
            raise ProtocolError(f"{context} field {key!r} must contain string headers")
        headers[header_name] = header_value
    return headers


def _page_items(page: Mapping[str, Any], context: str) -> list[JSONDict]:
    raw_items = page.get("items")
    if not isinstance(raw_items, list):
        raise ProtocolError(f"{context} page is missing an items array")
    items: list[JSONDict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ProtocolError(f"{context} page contains a non-object item")
        items.append(item)
    return items


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"API response contains invalid {field}")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProtocolError(f"API response contains invalid {field}")
    return value


def _is_terminal(snapshot: Mapping[str, Any]) -> bool:
    return snapshot.get("status") == "TERMINAL"


def _decode_sse_event(
    event_id: str | None, event_name: str | None, data_lines: Sequence[str]
) -> JSONDict:
    try:
        value = json.loads("\n".join(data_lines))
    except (ValueError, UnicodeDecodeError) as error:
        raise ProtocolError("SSE event data is not valid JSON") from error
    if not isinstance(value, dict):
        raise ProtocolError("SSE event data must be a JSON object")
    if event_id is not None:
        if not event_id.isdigit() or value.get("seq") != int(event_id):
            raise ProtocolError("SSE id does not match the event seq")
    if event_name is not None and value.get("type") != event_name:
        raise ProtocolError("SSE event name does not match the event type")
    return value
