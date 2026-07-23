"""Packaged API contract resources.

The source checkout keeps the canonical contracts in the repository-level
``openapi`` directory.  Built distributions map that directory into the
``automl_api._contracts`` namespace package, so consumers never need to know
the repository layout.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

_RESOURCE_PACKAGE = "automl_api._contracts"
_DEFAULT_CONTRACT = "automl-api.yaml"

__all__ = ["contract_path", "contract_resource"]


def _source_contract_path(name: str) -> Path:
    return Path(__file__).resolve().parents[4] / "openapi" / name


def contract_resource(name: str = _DEFAULT_CONTRACT) -> Any:
    """Return a traversable packaged contract, with a source-tree fallback."""

    if not name or Path(name).name != name or not name.endswith(".yaml"):
        raise ValueError("contract name must be a YAML filename")
    try:
        resource = files(_RESOURCE_PACKAGE).joinpath(name)
        if resource.is_file():
            return resource
    except (ModuleNotFoundError, TypeError):
        pass
    source_path = _source_contract_path(name)
    if not source_path.is_file():
        raise FileNotFoundError(f"API contract resource does not exist: {name}")
    return source_path


@contextmanager
def contract_path(name: str = _DEFAULT_CONTRACT) -> Iterator[Path]:
    """Yield a filesystem path for a contract for the lifetime of the context."""

    with as_file(contract_resource(name)) as path:
        yield path
