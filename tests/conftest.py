from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from automl_api.app import create_app
from automl_api.store import InMemoryStore


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(InMemoryStore())) as test_client:
        yield test_client
