from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from scripts.package_release import (
    ReleaseError,
    _archive,
    _default_archive_path,
    _read_version,
    _write_metadata,
)


def test_release_metadata_and_archive_are_verifiable(tmp_path: Path) -> None:
    bundle = tmp_path / "managed-automl-0.6.0"
    contract = bundle / "openapi" / "automl-api.yaml"
    wheel = bundle / "wheels" / "automl_sdk-0.6.0-py3-none-any.whl"
    contract.parent.mkdir(parents=True)
    wheel.parent.mkdir(parents=True)
    contract.write_text("openapi: 3.1.0\n", encoding="utf-8")
    wheel.write_bytes(b"wheel fixture")

    _write_metadata(bundle, version="0.6.0", docker=None)

    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    entries = {item["path"]: item for item in manifest["artifacts"]}
    assert manifest["service_version"] == "0.6.0"
    assert manifest["api_version"] == "v1"
    assert manifest["docker_image"] is None
    assert (
        entries["openapi/automl-api.yaml"]["sha256"]
        == hashlib.sha256(contract.read_bytes()).hexdigest()
    )
    assert entries["wheels/automl_sdk-0.6.0-py3-none-any.whl"]["size_bytes"] == len(
        b"wheel fixture"
    )

    checksum_lines = (bundle / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert any(line.endswith("  bundle-manifest.json") for line in checksum_lines)
    assert not any(line.endswith("  SHA256SUMS") for line in checksum_lines)

    archive = tmp_path / "release.tar.gz"
    _archive(bundle, archive)
    with tarfile.open(archive, "r:gz") as stream:
        names = set(stream.getnames())
    assert f"{bundle.name}/SHA256SUMS" in names
    assert f"{bundle.name}/openapi/automl-api.yaml" in names

    with pytest.raises(ReleaseError, match="outside the bundle"):
        _archive(bundle, bundle / "nested.tar.gz")


def test_project_version_is_read_from_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "example"\nversion = "1.2.3"\n', encoding="utf-8")
    assert _read_version(pyproject) == "1.2.3"


def test_default_archive_path_preserves_the_full_bundle_name(tmp_path: Path) -> None:
    bundle = tmp_path / "managed-automl-0.6.0-20260724T120000Z"
    assert _default_archive_path(bundle) == tmp_path / (
        "managed-automl-0.6.0-20260724T120000Z.tar.gz"
    )
