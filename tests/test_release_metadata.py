from __future__ import annotations

import re
import tomllib
from pathlib import Path

import automl_api
import automl_sdk
from automl_api.app import SERVICE_VERSION, app
from automl_api.models import AgentInterfaceManifest
from automl_api.resources import contract_resource


ROOT = Path(__file__).resolve().parents[1]


def _project_version(path: Path) -> str:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def _openapi_version(path: Path) -> str:
    head = path.read_text(encoding="utf-8").split("paths:", maxsplit=1)[0]
    match = re.search(r"^  version: ([^\s]+)$", head, re.MULTILINE)
    assert match is not None, f"{path} does not declare info.version"
    return match.group(1)


def test_release_versions_remain_in_lockstep() -> None:
    api_version = _project_version(ROOT / "pyproject.toml")
    sdk_version = _project_version(ROOT / "packages" / "python_sdk" / "pyproject.toml")
    canonical_version = _openapi_version(ROOT / "openapi" / "automl-api.yaml")
    agent_tools_version = _openapi_version(ROOT / "openapi" / "automl-agent-tools.yaml")

    assert {
        api_version,
        sdk_version,
        canonical_version,
        agent_tools_version,
        automl_api.__version__,
        automl_sdk.__version__,
        SERVICE_VERSION,
        app.version,
        AgentInterfaceManifest.model_fields["service_version"].default,
    } == {api_version}
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert f"AUTOML_IMAGE:-managed-automl-api:{api_version}" in compose
    gpu_compose = (ROOT / "compose.gpu.yaml").read_text(encoding="utf-8")
    assert f"AUTOML_GPU_IMAGE:-managed-automl-api:{api_version}-cuda" in gpu_compose
    direct_gpu_compose = (ROOT / "compose.gpu-direct.yaml").read_text(encoding="utf-8")
    assert f"AUTOML_GPU_IMAGE:-managed-automl-api:{api_version}-cuda" in direct_gpu_compose
    assert f"## {api_version} -" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_both_openapi_contracts_are_available_as_package_resources() -> None:
    for filename in ("automl-api.yaml", "automl-agent-tools.yaml"):
        expected = (ROOT / "openapi" / filename).read_bytes()
        assert contract_resource(filename).read_bytes() == expected


def test_sdk_distribution_declares_and_carries_partner_metadata() -> None:
    sdk_root = ROOT / "packages" / "python_sdk"
    document = tomllib.loads((sdk_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["readme"] == "README.md"
    assert document["project"]["license"] == "LicenseRef-Proprietary"
    assert document["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert (sdk_root / "README.md").is_file()
    assert (sdk_root / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (sdk_root / "NOTICE").is_file()
