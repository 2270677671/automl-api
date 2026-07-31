"""Minimal AutoML callback receiver with raw-body HMAC verification.

Run with:
    AUTOML_WEBHOOK_SIGNING_SECRET='<create endpoint response value>' \
      uvicorn webhook_receiver:app --app-dir examples/python --host 0.0.0.0 --port 9000

The in-memory delivery set is only for a single-process demo. Production receivers
must enforce delivery_id uniqueness in durable storage before returning 2xx.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response


app = FastAPI(title="Managed AutoML callback receiver example")
_processed_delivery_ids: set[str] = set()


def _signing_key() -> bytes:
    secret = os.environ.get("AUTOML_WEBHOOK_SIGNING_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "AUTOML_WEBHOOK_SIGNING_SECRET is not configured")
    try:
        key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except ValueError as error:
        raise HTTPException(503, "The callback signing secret is invalid") from error
    if len(key) != 32:
        raise HTTPException(503, "The callback signing secret must decode to 32 bytes")
    return key


@app.post("/automl/callback", status_code=204)
async def receive_callback(
    request: Request,
    delivery_id: Annotated[str, Header(alias="X-AutoML-Delivery-Id")],
    timestamp: Annotated[str, Header(alias="X-AutoML-Timestamp")],
    signature: Annotated[str, Header(alias="X-AutoML-Signature")],
) -> Response:
    raw_body = await request.body()
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise HTTPException(401, "Invalid callback timestamp") from error
    if abs(int(time.time()) - timestamp_value) > 300:
        raise HTTPException(401, "Callback timestamp is outside the replay window")

    digest = hmac.new(
        _signing_key(),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v1={digest}"
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid callback signature")

    try:
        envelope: dict[str, Any] = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(400, "Callback body is not valid JSON") from error
    if envelope.get("delivery_id") != delivery_id:
        raise HTTPException(400, "Delivery ID header and body do not match")
    if delivery_id in _processed_delivery_ids:
        return Response(status_code=204)

    # Persist the event and delivery ID atomically before acknowledging in production.
    event = envelope.get("event") or {}
    callback = envelope.get("callback") or {}
    print(
        json.dumps(
            {
                "delivery_id": delivery_id,
                "event_id": event.get("event_id"),
                "event_type": event.get("type"),
                "run_id": event.get("run_id"),
                "stage": callback.get("stage"),
                "states": callback.get("states"),
                "next_stage": callback.get("next_stage"),
                "reason": callback.get("reason"),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    _processed_delivery_ids.add(delivery_id)
    return Response(status_code=204)
