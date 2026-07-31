"""Durable webhook outbox delivery for the single-node service profile."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import socket
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from .models import StageCallbackPayload
from .protocol import iso_now, utcnow
from .store import InMemoryStore


LOGGER = logging.getLogger(__name__)
_UNFINISHED = {"PENDING", "DELIVERING", "RETRYING"}
_CALLBACK_STAGE_NAMES = {
    "INGEST": "data_read",
    "PROFILE": "data_analysis",
    "PLAN": "task_recognition",
    "TRAIN": "model_training",
    "EVALUATE": "model_evaluation",
    "PACKAGE": "result_package",
}


def _callback_reason(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def stage_callback_for_event(
    event: dict[str, Any],
    *,
    fallback_phase: str | None = None,
) -> dict[str, Any] | None:
    """Project terminal stage events into the compact Agent callback contract."""

    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None

    phase = str(payload.get("phase") or fallback_phase or "")
    stage = _CALLBACK_STAGE_NAMES.get(phase)
    if stage is None:
        return None

    next_phase: str | None = None
    if event_type == "run.stage_completed.v1":
        raw_next_phase = payload.get("next_phase")
        next_phase = str(raw_next_phase) if raw_next_phase is not None else None
        readiness = payload.get("next_stage_ready")
        next_stage = next_phase is not None if readiness is None else bool(readiness)
        states = "success"
        reason = _callback_reason(payload.get("reason"))
        if not next_stage and reason is None:
            reason = "workflow_completed" if next_phase is None else "next_stage_blocked"
    elif event_type == "run.failed.v1":
        next_stage = False
        states = "failed"
        reason = _callback_reason(payload.get("failure_code")) or "stage_failed"
    elif event_type == "run.canceled.v1":
        next_stage = False
        states = "canceled"
        reason = "user_canceled"
    else:
        return None

    callback = StageCallbackPayload.model_validate(
        {
            "schema_version": "1.0",
            "toolname": "automl",
            "run_id": event["run_id"],
            "event_id": event["event_id"],
            "seq": event["seq"],
            "occurred_at": event["occurred_at"],
            "stage": stage,
            "states": states,
            "next_stage": next_stage,
            "next_stage_name": _CALLBACK_STAGE_NAMES.get(next_phase or ""),
            "reason": reason,
        }
    )
    return callback.model_dump(mode="json")


class _WebhookHTTPError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"CALLBACK_HTTP_{status_code}")


@dataclass(frozen=True)
class WebhookDeliverySettings:
    enabled: bool
    require_https: bool
    allowed_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    timeout_seconds: float = 10.0
    max_attempts: int = 12
    max_age_hours: int = 72
    redelivery_retention_days: int = 30
    response_limit_bytes: int = 65536
    poll_interval_seconds: float = 0.5

    def __post_init__(self) -> None:
        if not 1 <= self.redelivery_retention_days <= 3650:
            raise ValueError("Webhook redelivery retention must be between 1 and 3650 days.")

    @classmethod
    def from_env(cls) -> WebhookDeliverySettings:
        mode = os.environ.get("AUTOML_WEBHOOK_DISPATCH_MODE", "").strip().lower()
        require_https = os.environ.get("AUTOML_WEBHOOK_REQUIRE_HTTPS", "true").strip().lower()
        networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in os.environ.get("AUTOML_WEBHOOK_ALLOWED_CIDRS", "").split(",")
            if item.strip()
        )
        return cls(
            enabled=mode in {"builtin", "http"},
            require_https=require_https not in {"0", "false", "no", "off"},
            allowed_cidrs=networks,
            timeout_seconds=float(os.environ.get("AUTOML_WEBHOOK_TIMEOUT_SECONDS", "10")),
            max_attempts=int(os.environ.get("AUTOML_WEBHOOK_MAX_ATTEMPTS", "12")),
            redelivery_retention_days=int(
                os.environ.get("AUTOML_WEBHOOK_REDELIVERY_RETENTION_DAYS", "30")
            ),
        )


@dataclass(frozen=True)
class _ValidatedWebhookDestination:
    request_url: httpx.URL
    host_header: str
    sni_hostname: str


def validate_webhook_registration_url(url: str, *, require_https: bool) -> None:
    parsed = urlsplit(url)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("Webhook URL must use HTTPS in this service profile.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Webhook URL must have a host and cannot contain user information.")
    if parsed.fragment:
        raise ValueError("Webhook URL cannot contain a fragment.")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    allowed_cidrs = WebhookDeliverySettings.from_env().allowed_cidrs
    if not literal.is_global and not any(literal in network for network in allowed_cidrs):
        raise ValueError(
            "Webhook URL uses a non-public IP; configure AUTOML_WEBHOOK_ALLOWED_CIDRS first."
        )


async def _validate_delivery_destination(
    url: str,
    settings: WebhookDeliverySettings,
) -> _ValidatedWebhookDestination:
    parsed = urlsplit(url)
    allowed_schemes = {"https"} if settings.require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("WEBHOOK_SCHEME_NOT_ALLOWED")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("WEBHOOK_AUTHORITY_INVALID")
    if parsed.fragment:
        raise ValueError("WEBHOOK_FRAGMENT_NOT_ALLOWED")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)

    def resolve() -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return {
            ipaddress.ip_address(item[4][0].split("%", 1)[0])
            for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        }

    try:
        with anyio.fail_after(settings.timeout_seconds):
            addresses = await anyio.to_thread.run_sync(resolve, abandon_on_cancel=True)
    except TimeoutError as error:
        raise TimeoutError("WEBHOOK_DNS_TIMEOUT") from error
    if not addresses:
        raise ValueError("WEBHOOK_DNS_EMPTY")
    for address in addresses:
        if any(address in network for network in settings.allowed_cidrs):
            continue
        if not address.is_global:
            raise ValueError("WEBHOOK_DESTINATION_BLOCKED")
    selected_address = min(addresses, key=lambda address: (address.version, int(address)))
    original_url = httpx.URL(url)
    return _ValidatedWebhookDestination(
        request_url=original_url.copy_with(host=str(selected_address)),
        host_header=original_url.netloc.decode("ascii"),
        sni_hostname=original_url.host,
    )


def _problem(delivery: dict[str, Any], code: str, detail: str) -> dict[str, Any]:
    return {
        "type": "about:blank",
        "title": "Webhook delivery failed",
        "status": 502,
        "code": code,
        "detail": detail[:500],
        "retriable": True,
        "correlation_id": str(delivery["delivery_id"]),
        "run_id": delivery.get("run_id"),
        "errors": [],
    }


class WebhookDeliveryWorker:
    """One in-process dispatcher sharing the durable Store checkpoint."""

    def __init__(
        self,
        store: InMemoryStore,
        settings: WebhookDeliverySettings | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or WebhookDeliverySettings.from_env()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.settings.enabled or self.is_running:
            return
        self._stop.clear()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        await self._recover_interrupted_deliveries()
        self._task = asyncio.create_task(self._run(), name="automl-webhook-delivery")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _recover_interrupted_deliveries(self) -> None:
        for endpoint in await self.store.list_webhook_endpoints():
            endpoint_id = str(endpoint["webhook_endpoint_id"])
            for delivery in await self.store.list_webhook_deliveries(endpoint_id):
                if delivery.get("status") == "DELIVERING":
                    await self.store.update_webhook_delivery(
                        endpoint_id,
                        str(delivery["delivery_id"]),
                        {"status": "RETRYING", "next_attempt_at": iso_now()},
                    )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delivered = await self._dispatch_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Webhook dispatcher iteration failed")
                delivered = False
            if not delivered:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    async def _dispatch_one(self) -> bool:
        now = iso_now()
        for endpoint in sorted(
            await self.store.list_webhook_endpoints(),
            key=lambda item: str(item["webhook_endpoint_id"]),
        ):
            if endpoint.get("status") != "ACTIVE":
                continue
            deliveries = sorted(
                await self.store.list_webhook_deliveries(str(endpoint["webhook_endpoint_id"])),
                key=lambda item: (str(item.get("created_at")), str(item["delivery_id"])),
            )
            blocked_runs: set[str] = set()
            for delivery in deliveries:
                run_id = str(delivery["run_id"])
                if delivery.get("status") not in _UNFINISHED:
                    continue
                if run_id in blocked_runs:
                    continue
                blocked_runs.add(run_id)
                if delivery.get("status") == "DELIVERING":
                    continue
                next_attempt = delivery.get("next_attempt_at")
                if isinstance(next_attempt, str) and next_attempt > now:
                    continue
                await self._deliver(endpoint, delivery)
                return True
        return False

    async def _deliver(self, endpoint: dict[str, Any], delivery: dict[str, Any]) -> None:
        endpoint_id = str(endpoint["webhook_endpoint_id"])
        delivery_id = str(delivery["delivery_id"])
        attempt = int(delivery.get("attempt_count") or 0) + 1
        first_attempt_at = delivery.get("first_attempt_at") or iso_now()
        delivery = await self.store.update_webhook_delivery(
            endpoint_id,
            delivery_id,
            {
                "status": "DELIVERING",
                "attempt_count": attempt,
                "first_attempt_at": first_attempt_at,
                "next_attempt_at": None,
            },
        )
        try:
            event = await self._event_for_delivery(delivery)
            run = await self.store.get_run(str(delivery["run_id"]))
            callback = stage_callback_for_event(
                event,
                fallback_phase=str(run.get("phase")) if run is not None else None,
            )
            envelope: dict[str, Any] = {
                "delivery_id": delivery_id,
                "webhook_endpoint_id": endpoint_id,
                "attempt": attempt,
                "event": event,
            }
            if callback is not None:
                envelope["callback"] = callback
            body = json.dumps(
                envelope,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            timestamp = str(int(utcnow().timestamp()))
            secret = str(endpoint["signing_secret"])
            signing_key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
            signature = hmac.new(
                signing_key,
                timestamp.encode("ascii") + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            destination = await _validate_delivery_destination(str(endpoint["url"]), self.settings)
            assert self._client is not None
            async with self._client.stream(
                "POST",
                destination.request_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Host": destination.host_header,
                    "User-Agent": "managed-automl-webhook/1.0",
                    "X-AutoML-Delivery-Id": delivery_id,
                    "X-AutoML-Timestamp": timestamp,
                    "X-AutoML-Signature": f"v1={signature}",
                },
                extensions={"sni_hostname": destination.sni_hostname},
            ) as response:
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.settings.response_limit_bytes:
                        break
                status = response.status_code
            if 200 <= status < 300:
                await self.store.update_webhook_delivery(
                    endpoint_id,
                    delivery_id,
                    {
                        "status": "SUCCEEDED",
                        "last_response_status": status,
                        "last_problem": None,
                        "delivered_at": iso_now(),
                        "next_attempt_at": None,
                    },
                )
                return
            raise _WebhookHTTPError(status)
        except asyncio.CancelledError:
            await self.store.update_webhook_delivery(
                endpoint_id,
                delivery_id,
                {"status": "RETRYING", "next_attempt_at": iso_now()},
            )
            raise
        except Exception as error:
            await self._retry_or_exhaust(endpoint, delivery, error)

    async def _event_for_delivery(self, delivery: dict[str, Any]) -> dict[str, Any]:
        events = await self.store.get_events(str(delivery["run_id"]))
        event = next(
            (item for item in events if item.get("event_id") == delivery.get("event_id")),
            None,
        )
        if event is None:
            raise RuntimeError("WEBHOOK_EVENT_NOT_FOUND")
        return event

    async def _retry_or_exhaust(
        self,
        endpoint: dict[str, Any],
        delivery: dict[str, Any],
        error: Exception,
    ) -> None:
        endpoint_id = str(endpoint["webhook_endpoint_id"])
        delivery_id = str(delivery["delivery_id"])
        attempt = int(delivery.get("attempt_count") or 0)
        first_attempt = str(delivery.get("first_attempt_at") or iso_now())
        age_deadline = (
            (utcnow() - timedelta(hours=self.settings.max_age_hours))
            .isoformat()
            .replace("+00:00", "Z")
        )
        exhausted = attempt >= self.settings.max_attempts or first_attempt < age_deadline
        error_text = str(error)
        code = (
            error_text if error_text.startswith(("CALLBACK_", "WEBHOOK_")) else type(error).__name__
        )
        last_response_status = getattr(error, "status_code", None)
        if exhausted:
            exhausted_at = utcnow()
            await self.store.update_webhook_delivery(
                endpoint_id,
                delivery_id,
                {
                    "status": "EXHAUSTED",
                    "next_attempt_at": None,
                    "last_problem": _problem(delivery, code, str(error)),
                    "last_response_status": last_response_status,
                    "exhausted_at": exhausted_at.isoformat().replace("+00:00", "Z"),
                    "redeliver_until": (
                        exhausted_at + timedelta(days=self.settings.redelivery_retention_days)
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            await self.store.update_webhook_endpoint(
                endpoint_id,
                {
                    "status": "PAUSED_DELIVERY_FAILURES",
                    "status_reason": "DELIVERY_RETRIES_EXHAUSTED",
                    "paused_at": iso_now(),
                },
            )
            return
        delay_cap = min(21600.0, 5.0 * (2.0 ** max(attempt - 1, 0)))
        delay = random.uniform(0.0, delay_cap)
        await self.store.update_webhook_delivery(
            endpoint_id,
            delivery_id,
            {
                "status": "RETRYING",
                "next_attempt_at": (utcnow() + timedelta(seconds=delay))
                .isoformat()
                .replace("+00:00", "Z"),
                "last_problem": _problem(delivery, code, str(error)),
                "last_response_status": last_response_status,
            },
        )


__all__ = [
    "WebhookDeliverySettings",
    "WebhookDeliveryWorker",
    "stage_callback_for_event",
    "validate_webhook_registration_url",
]
