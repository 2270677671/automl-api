from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


_CORRELATION_ID: ContextVar[str | None] = ContextVar("automl_correlation_id", default=None)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def current_correlation_id() -> str:
    return _CORRELATION_ID.get() or f"corr_{uuid4().hex}"


@dataclass(slots=True)
class APIProblem(Exception):
    status: int
    code: str
    title: str
    detail: str
    retriable: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {
            "type": f"/problems/{self.code}",
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "retriable": self.retriable,
            "correlation_id": current_correlation_id(),
            **self.extras,
        }


def install_problem_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlate_request(request: Request, call_next: Any) -> JSONResponse:
        requested = request.headers.get("X-Request-ID", "")
        correlation_id = (
            requested if _REQUEST_ID_PATTERN.fullmatch(requested) else f"corr_{uuid4().hex}"
        )
        context_token = _CORRELATION_ID.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers.setdefault("X-Correlation-ID", correlation_id)
            return response
        finally:
            _CORRELATION_ID.reset(context_token)

    @app.exception_handler(APIProblem)
    async def handle_api_problem(_request: Request, exc: APIProblem) -> JSONResponse:
        headers = dict(exc.headers)
        if exc.status == 401:
            headers.setdefault("WWW-Authenticate", "Bearer")
        return JSONResponse(
            status_code=exc.status,
            content=exc.body(),
            media_type="application/problem+json",
            headers=headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        problem = APIProblem(
            status=exc.status_code,
            code=code,
            title="Resource not found" if exc.status_code == 404 else "HTTP request failed",
            detail=str(exc.detail),
        )
        headers = dict(exc.headers or {})
        if exc.status_code == 401:
            headers.setdefault("WWW-Authenticate", "Bearer")
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.body(),
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        problem = APIProblem(
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="The service could not complete the request.",
            retriable=True,
        )
        return JSONResponse(
            status_code=500,
            content=problem.body(),
            media_type="application/problem+json",
        )
