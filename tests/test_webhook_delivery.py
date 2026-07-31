from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from automl_api import webhooks
from automl_api.app import _stored_http_response
from automl_api.protocol import iso_now
from automl_api.store import InMemoryStore, StoredResponse
from automl_api.webhooks import WebhookDeliverySettings, WebhookDeliveryWorker

from .helpers import AUTH, create_ready_dataset, mutation_headers, run_request


def _dns_answer(address: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    socket_address = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", socket_address)


def test_settings_load_independent_redelivery_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOML_WEBHOOK_REDELIVERY_RETENTION_DAYS", "45")
    assert WebhookDeliverySettings.from_env().redelivery_retention_days == 45

    monkeypatch.setenv("AUTOML_WEBHOOK_REDELIVERY_RETENTION_DAYS", "0")
    with pytest.raises(ValueError, match="between 1 and 3650 days"):
        WebhookDeliverySettings.from_env()


def test_callback_url_must_match_the_selected_registered_endpoint(client) -> None:
    first = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("callback-endpoint-first-0001"),
        json={"url": "https://first.example.test/callback", "event_types": ["*"]},
    ).json()
    second = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("callback-endpoint-second-0001"),
        json={"url": "https://second.example.test/callback", "event_types": ["*"]},
    ).json()
    dataset = create_ready_dataset(client, "callback-mismatch-0001")
    request = run_request(dataset["dataset_version_id"])
    request["callback_url"] = first["url"]
    request["webhook_endpoint_ids"] = [second["webhook_endpoint_id"]]
    rejected = client.post(
        "/v1/runs",
        headers=mutation_headers("callback-mismatch-run-0001"),
        json=request,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "callback_endpoint_mismatch"

    unbound = run_request(dataset["dataset_version_id"])
    created = client.post(
        "/v1/runs",
        headers=mutation_headers("callback-unbound-run-0001"),
        json=unbound,
    )
    assert created.status_code == 202, created.text
    deliveries = client.get(
        f"/v1/webhook-endpoints/{first['webhook_endpoint_id']}/deliveries", headers=AUTH
    ).json()["items"]
    assert deliveries == []


def test_webhook_registration_rejects_unknown_event_types(client) -> None:
    response = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("callback-invalid-event-type-0001"),
        json={
            "url": "https://callback.example.test/events",
            "event_types": ["run.stage_complete.typo.v1"],
        },
    )

    assert response.status_code == 422


def test_delete_webhook_endpoint_returns_bodyless_204_on_replay(client) -> None:
    endpoint = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("callback-delete-endpoint-0001"),
        json={"url": "https://delete.example.test/callback", "event_types": ["*"]},
    ).json()
    headers = mutation_headers("callback-delete-command-0001")
    path = f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}"

    deleted = client.delete(path, headers=headers)
    replay = client.delete(path, headers=headers)

    for response in (deleted, replay):
        assert response.status_code == 204
        assert response.content == b""
        assert "content-type" not in response.headers
        assert "content-length" not in response.headers
    assert deleted.headers["x-correlation-id"] == replay.headers["x-correlation-id"]
    stored = client.get(path, headers=AUTH)
    assert stored.status_code == 200
    assert stored.json()["status"] == "DISABLED"

    historical = _stored_http_response(
        StoredResponse(status_code=304, body={"ignored": True}, headers={"ETag": '"saved"'})
    )
    assert historical.body == b""
    assert historical.headers["etag"] == '"saved"'
    assert "content-type" not in historical.headers
    assert "content-length" not in historical.headers


def test_worker_sends_exact_signed_envelope_and_marks_success() -> None:
    async def exercise() -> None:
        state = InMemoryStore()
        signing_key = bytes(range(32))
        secret = base64.urlsafe_b64encode(signing_key).rstrip(b"=").decode("ascii")
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "http://127.0.0.1/callback",
                "event_types": ["run.stage_completed.v1"],
                "status": "ACTIVE",
                "signing_secret": secret,
                "created_at": iso_now(),
            }
        )
        run = await state.create_run(
            {
                "run_id": state.new_id("run"),
                "tenant_id": "tenant_1",
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        await state.append_event(
            run["run_id"],
            {
                "event_id": state.new_id("event"),
                "schema_version": "1.0",
                "occurred_at": iso_now(),
                "type": "run.stage_completed.v1",
                "payload": {"phase": "INGEST"},
                "links": {"run": f"/v1/runs/{run['run_id']}"},
            },
        )
        delivery = (await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"]))[0]

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content
            timestamp = request.headers["X-AutoML-Timestamp"]
            expected = (
                "v1="
                + hmac.new(
                    signing_key,
                    timestamp.encode("ascii") + b"." + body,
                    hashlib.sha256,
                ).hexdigest()
            )
            assert hmac.compare_digest(request.headers["X-AutoML-Signature"], expected)
            envelope = json.loads(body)
            assert envelope["delivery_id"] == delivery["delivery_id"]
            assert envelope["attempt"] == 1
            assert envelope["event"]["payload"]["phase"] == "INGEST"
            return httpx.Response(204)

        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(
                enabled=True,
                require_https=False,
                allowed_cidrs=(ipaddress.ip_network("127.0.0.0/8"),),
            ),
        )
        worker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await worker._deliver(endpoint, delivery)
        finally:
            await worker._client.aclose()
        stored = await state.get_webhook_delivery(
            endpoint["webhook_endpoint_id"], delivery["delivery_id"]
        )
        assert stored is not None
        assert stored["status"] == "SUCCEEDED"
        assert stored["attempt_count"] == 1
        assert stored["last_response_status"] == 204

    asyncio.run(exercise())


def test_exhausted_delivery_does_not_block_later_pending_delivery() -> None:
    async def exercise() -> None:
        state = InMemoryStore()
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["*"],
                "status": "PAUSED_DELIVERY_FAILURES",
                "signing_secret": "A" * 43,
                "created_at": iso_now(),
            }
        )
        run = await state.create_run(
            {
                "run_id": state.new_id("run"),
                "tenant_id": "tenant_1",
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        for phase in ("INGEST", "PROFILE"):
            await state.append_event(
                run["run_id"],
                {
                    "event_id": state.new_id("event"),
                    "schema_version": "1.0",
                    "occurred_at": iso_now(),
                    "type": "run.stage_completed.v1",
                    "payload": {"phase": phase},
                },
            )
        deliveries = await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
        assert len(deliveries) == 2
        await state.update_webhook_delivery(
            endpoint["webhook_endpoint_id"],
            deliveries[0]["delivery_id"],
            {"status": "EXHAUSTED", "next_attempt_at": None},
        )
        endpoint = await state.update_webhook_endpoint(
            endpoint["webhook_endpoint_id"],
            {"status": "ACTIVE", "status_reason": None, "paused_at": None},
        )

        dispatched: list[str] = []

        async def record_delivery(_endpoint: dict, delivery: dict) -> None:
            dispatched.append(str(delivery["delivery_id"]))

        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(enabled=True, require_https=True, allowed_cidrs=()),
        )
        worker._deliver = record_delivery  # type: ignore[method-assign]
        assert await worker._dispatch_one() is True
        assert dispatched == [deliveries[1]["delivery_id"]]
        assert endpoint["status"] == "ACTIVE"

    asyncio.run(exercise())


def test_oldest_unfinished_delivery_preserves_run_order() -> None:
    async def exercise() -> None:
        state = InMemoryStore()
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["*"],
                "status": "ACTIVE",
                "signing_secret": "A" * 43,
                "created_at": iso_now(),
            }
        )
        run = await state.create_run(
            {
                "run_id": state.new_id("run"),
                "tenant_id": "tenant_1",
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        for phase in ("INGEST", "PROFILE"):
            await state.append_event(
                run["run_id"],
                {
                    "event_id": state.new_id("event"),
                    "schema_version": "1.0",
                    "occurred_at": iso_now(),
                    "type": "run.stage_completed.v1",
                    "payload": {"phase": phase},
                },
            )
        deliveries = await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"])
        await state.update_webhook_delivery(
            endpoint["webhook_endpoint_id"],
            deliveries[0]["delivery_id"],
            {
                "status": "RETRYING",
                "next_attempt_at": (datetime.now(timezone.utc) + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )

        dispatched: list[str] = []

        async def record_delivery(_endpoint: dict, delivery: dict) -> None:
            dispatched.append(str(delivery["delivery_id"]))

        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(enabled=True, require_https=True, allowed_cidrs=()),
        )
        worker._deliver = record_delivery  # type: ignore[method-assign]
        assert await worker._dispatch_one() is False
        assert dispatched == []

    asyncio.run(exercise())


def test_worker_pins_validated_ip_and_preserves_host_and_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls: list[tuple[str, int]] = []

    def resolve(host: str, port: int, **_kwargs: object) -> list[tuple]:
        resolver_calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    async def exercise() -> None:
        state = InMemoryStore()
        signing_key = bytes(range(32))
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://hooks.example.test:8443/automl?source=agent",
                "event_types": ["*"],
                "status": "ACTIVE",
                "signing_secret": base64.urlsafe_b64encode(signing_key)
                .rstrip(b"=")
                .decode("ascii"),
                "created_at": iso_now(),
            }
        )
        run = await state.create_run(
            {
                "run_id": state.new_id("run"),
                "tenant_id": "tenant_1",
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        await state.append_event(
            run["run_id"],
            {
                "event_id": state.new_id("event"),
                "schema_version": "1.0",
                "occurred_at": iso_now(),
                "type": "run.stage_completed.v1",
                "payload": {"phase": "INGEST"},
            },
        )
        delivery = (await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"]))[0]

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://93.184.216.34:8443/automl?source=agent"
            assert request.headers["Host"] == "hooks.example.test:8443"
            assert request.extensions["sni_hostname"] == "hooks.example.test"
            return httpx.Response(204)

        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(enabled=True, require_https=True, allowed_cidrs=()),
        )
        worker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await worker._deliver(endpoint, delivery)
        finally:
            await worker._client.aclose()

        stored = await state.get_webhook_delivery(
            endpoint["webhook_endpoint_id"], delivery["delivery_id"]
        )
        assert stored is not None and stored["status"] == "SUCCEEDED"

    asyncio.run(exercise())
    assert resolver_calls == [("hooks.example.test", 8443)]


@pytest.mark.parametrize(
    "private_address",
    ["10.23.45.67", "172.20.1.2", "192.168.50.10"],
)
def test_delivery_destination_rejects_rfc1918_dns_answers(
    private_address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(_host: str, port: int, **_kwargs: object) -> list[tuple]:
        return [_dns_answer(private_address, port)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    settings = WebhookDeliverySettings(
        enabled=True,
        require_https=True,
        allowed_cidrs=(),
    )
    with pytest.raises(ValueError, match="^WEBHOOK_DESTINATION_BLOCKED$"):
        asyncio.run(
            webhooks._validate_delivery_destination(
                "https://hooks.example.test/events",
                settings,
            )
        )


@pytest.mark.parametrize("blocked_address", ["169.254.169.254", "fe80::1"])
def test_delivery_destination_rejects_link_local_and_metadata_dns_answers(
    blocked_address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(_host: str, port: int, **_kwargs: object) -> list[tuple]:
        return [_dns_answer(blocked_address, port)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    settings = WebhookDeliverySettings(
        enabled=True,
        require_https=True,
        allowed_cidrs=(),
    )
    with pytest.raises(ValueError, match="^WEBHOOK_DESTINATION_BLOCKED$"):
        asyncio.run(
            webhooks._validate_delivery_destination(
                "https://hooks.example.test/events",
                settings,
            )
        )


def test_delivery_destination_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(_host: str, port: int, **_kwargs: object) -> list[tuple]:
        return [
            _dns_answer("93.184.216.34", port),
            _dns_answer("10.0.0.7", port),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    settings = WebhookDeliverySettings(
        enabled=True,
        require_https=True,
        allowed_cidrs=(),
    )
    with pytest.raises(ValueError, match="^WEBHOOK_DESTINATION_BLOCKED$"):
        asyncio.run(
            webhooks._validate_delivery_destination(
                "https://hooks.example.test/events",
                settings,
            )
        )


def test_dns_resolution_timeout_does_not_block_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    def resolve(_host: str, port: int, **_kwargs: object) -> list[tuple]:
        resolver_started.set()
        release_resolver.wait(timeout=5)
        return [_dns_answer("93.184.216.34", port)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    settings = WebhookDeliverySettings(
        enabled=True,
        require_https=True,
        allowed_cidrs=(),
        timeout_seconds=0.05,
    )
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="^WEBHOOK_DNS_TIMEOUT$"):
            asyncio.run(
                webhooks._validate_delivery_destination(
                    "https://hooks.example.test/events",
                    settings,
                )
            )
        assert resolver_started.is_set()
        assert time.monotonic() - started_at < 0.5
    finally:
        release_resolver.set()


def test_pinned_https_connection_verifies_certificate_for_original_hostname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hooks.example.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("hooks.example.test")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = tmp_path / "callback-cert.pem"
    key_path = tmp_path / "callback-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    async def exercise() -> None:
        seen_sni: list[str | None] = []
        received_requests: list[bytes] = []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        server_context.set_servername_callback(
            lambda _socket, server_name, _context: seen_sni.append(server_name)
        )

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(
            receive,
            host="127.0.0.1",
            port=0,
            ssl=server_context,
        )
        port = int(server.sockets[0].getsockname()[1])
        original_getaddrinfo = socket.getaddrinfo

        def resolve(host: str, requested_port: int, **kwargs: object) -> list[tuple]:
            if host == "hooks.example.test":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", requested_port),
                    )
                ]
            return original_getaddrinfo(host, requested_port, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        state = InMemoryStore()
        signing_key = bytes(range(32))
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": f"https://hooks.example.test:{port}/automl",
                "event_types": ["*"],
                "status": "ACTIVE",
                "signing_secret": base64.urlsafe_b64encode(signing_key)
                .rstrip(b"=")
                .decode("ascii"),
                "created_at": iso_now(),
            }
        )
        run = await state.create_run(
            {
                "run_id": state.new_id("run"),
                "tenant_id": "tenant_1",
                "webhook_endpoint_ids": [endpoint["webhook_endpoint_id"]],
            }
        )
        await state.append_event(
            run["run_id"],
            {
                "event_id": state.new_id("event"),
                "schema_version": "1.0",
                "occurred_at": iso_now(),
                "type": "run.stage_completed.v1",
                "payload": {"phase": "INGEST"},
            },
        )
        delivery = (await state.list_webhook_deliveries(endpoint["webhook_endpoint_id"]))[0]
        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(
                enabled=True,
                require_https=True,
                allowed_cidrs=(ipaddress.ip_network("127.0.0.0/8"),),
            ),
        )
        client_context = ssl.create_default_context(cafile=certificate_path)
        worker._client = httpx.AsyncClient(
            verify=client_context,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        try:
            await worker._deliver(endpoint, delivery)
        finally:
            await worker._client.aclose()
            server.close()
            await server.wait_closed()

        stored = await state.get_webhook_delivery(
            endpoint["webhook_endpoint_id"], delivery["delivery_id"]
        )
        assert stored is not None and stored["status"] == "SUCCEEDED"
        assert seen_sni == ["hooks.example.test"]
        assert received_requests
        assert f"Host: hooks.example.test:{port}\r\n".encode() in received_requests[0]

    asyncio.run(exercise())


def test_exhaustion_uses_independent_redelivery_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(webhooks, "utcnow", lambda: fixed_now)

    async def exercise() -> None:
        state = InMemoryStore()
        endpoint = await state.create_webhook_endpoint(
            {
                "tenant_id": "tenant_1",
                "url": "https://callback.example.test/events",
                "event_types": ["*"],
                "status": "ACTIVE",
                "signing_secret": "A" * 43,
                "created_at": iso_now(),
            }
        )
        delivery = await state.create_webhook_delivery(
            endpoint["webhook_endpoint_id"],
            {
                "tenant_id": "tenant_1",
                "event_id": "evt_1",
                "event_type": "run.completed.v1",
                "run_id": "run_1",
                "status": "DELIVERING",
                "attempt_count": 1,
                "first_attempt_at": fixed_now.isoformat().replace("+00:00", "Z"),
                "created_at": fixed_now.isoformat().replace("+00:00", "Z"),
            },
        )
        worker = WebhookDeliveryWorker(
            state,
            WebhookDeliverySettings(
                enabled=True,
                require_https=True,
                allowed_cidrs=(),
                max_attempts=1,
                max_age_hours=1,
                redelivery_retention_days=30,
            ),
        )
        await worker._retry_or_exhaust(endpoint, delivery, RuntimeError("unavailable"))
        stored = await state.get_webhook_delivery(
            endpoint["webhook_endpoint_id"], delivery["delivery_id"]
        )
        assert stored is not None
        assert stored["status"] == "EXHAUSTED"
        assert stored["exhausted_at"] == "2026-07-31T12:00:00Z"
        assert stored["redeliver_until"] == "2026-08-30T12:00:00Z"

    asyncio.run(exercise())


def test_redelivery_route_rejects_expired_exhausted_delivery(client) -> None:
    endpoint = client.post(
        "/v1/webhook-endpoints",
        headers=mutation_headers("expired-redelivery-endpoint-0001"),
        json={"url": "https://callback.example.test/events", "event_types": ["*"]},
    ).json()
    dataset = create_ready_dataset(client, "expired-redelivery-0001")
    request = run_request(dataset["dataset_version_id"])
    request["callback_url"] = endpoint["url"]
    request["webhook_endpoint_ids"] = [endpoint["webhook_endpoint_id"]]
    created = client.post(
        "/v1/runs",
        headers=mutation_headers("expired-redelivery-run-0001"),
        json=request,
    )
    assert created.status_code == 202, created.text
    deliveries = client.get(
        f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}/deliveries",
        headers=AUTH,
    ).json()["items"]
    delivery = deliveries[0]
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(
        client.app.state.store.update_webhook_delivery(
            endpoint["webhook_endpoint_id"],
            delivery["delivery_id"],
            {
                "status": "EXHAUSTED",
                "next_attempt_at": None,
                "exhausted_at": expired_at,
                "redeliver_until": expired_at,
            },
        )
    )

    rejected = client.post(
        f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}"
        f"/deliveries/{delivery['delivery_id']}:redeliver",
        headers=mutation_headers("expired-redelivery-request-0001"),
    )
    assert rejected.status_code == 410, rejected.text
    assert rejected.json()["code"] == "webhook_redelivery_expired"

    retained_until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    asyncio.run(
        client.app.state.store.update_webhook_delivery(
            endpoint["webhook_endpoint_id"],
            delivery["delivery_id"],
            {"redeliver_until": retained_until},
        )
    )
    accepted = client.post(
        f"/v1/webhook-endpoints/{endpoint['webhook_endpoint_id']}"
        f"/deliveries/{delivery['delivery_id']}:redeliver",
        headers=mutation_headers("retained-redelivery-request-0001"),
    )
    assert accepted.status_code == 202, accepted.text
