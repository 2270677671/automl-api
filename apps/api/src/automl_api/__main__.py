from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn

from . import app as app_module
from .resources import contract_path


_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535")
    return port


def _log_level(value: str) -> str:
    value = value.strip().lower()
    if value not in _LOG_LEVELS:
        allowed = ", ".join(sorted(_LOG_LEVELS))
        raise argparse.ArgumentTypeError(f"log level must be one of: {allowed}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Managed AutoML API server.")
    parser.add_argument(
        "--host",
        default=os.environ.get("AUTOML_HOST", "127.0.0.1"),
        help="bind host (default: AUTOML_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=_port(os.environ.get("AUTOML_PORT", "8000")),
        help="bind port (default: AUTOML_PORT or 8000)",
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=_log_level(os.environ.get("AUTOML_LOG_LEVEL", "info")),
        metavar="LEVEL",
        help="uvicorn log level (default: AUTOML_LOG_LEVEL or info)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    host = args.host.strip()
    if not host:
        raise SystemExit("host must not be empty")
    with contract_path("automl-api.yaml") as packaged_contract:
        # The route is defined in app.py, but its response path is intentionally
        # replaced here so the supported console entry point works from a wheel.
        app_module.OPENAPI_PATH = packaged_contract
        uvicorn.run(
            app_module.app,
            host=host,
            port=args.port,
            log_level=args.log_level,
            reload=False,
        )


if __name__ == "__main__":
    main()
