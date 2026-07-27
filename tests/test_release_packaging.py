from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import scripts.package_release as release_packaging
from scripts.package_release import (
    ReleaseError,
    _archive,
    _copy_inputs,
    _default_archive_path,
    _normalize_platform,
    _read_version,
    _save_docker_images,
    _write_metadata,
)


def test_release_bundle_contains_public_documentation_and_examples(tmp_path: Path) -> None:
    api_wheel = tmp_path / "managed_automl_skeleton-0.8.0-py3-none-any.whl"
    sdk_wheel = tmp_path / "automl_sdk-0.8.0-py3-none-any.whl"
    api_wheel.write_bytes(b"api")
    sdk_wheel.write_bytes(b"sdk")
    bundle = tmp_path / "bundle"

    _copy_inputs(bundle, api_wheel, sdk_wheel)

    required_paths = (
        "docs/README.md",
        "docs/api-user-guide-with-examples.md",
        "docs/reproduction-guide.md",
        "docs/user-manual.md",
        "compose.dual-ip.yaml",
        "examples/README.md",
        "examples/python/sdk_guided_workflow.py",
        "examples/python/http_guided_workflow.py",
        "examples/data/customer_churn.csv",
        "examples/data/regression.csv",
        "examples/requests/sklearn-guided.json",
        "examples/requests/autogluon-binary.json",
        "examples/requests/tabpfn-regression.json",
    )
    for relative_path in required_paths:
        assert (bundle / relative_path).is_file(), relative_path


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
    assert manifest["schema_version"] == "2"
    assert manifest["docker_image"] is None
    assert manifest["docker_images"] == []
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


def test_multiple_docker_images_share_one_platform_checked_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inspected = {
        "registry.example.test/automl:0.8.0": {
            "reference": "registry.example.test/automl:0.8.0",
            "load_reference": "registry.example.test/automl:0.8.0",
            "id": "sha256:api",
            "repo_tags": ["registry.example.test/automl:0.8.0"],
            "repo_digests": ["registry.example.test/automl@sha256:manifest"],
            "os": "linux",
            "architecture": "amd64",
            "variant": None,
            "platform": "linux/amd64",
            "image_size_bytes": 100,
        },
        "registry.example.test/caddy@sha256:index": {
            "reference": "registry.example.test/caddy@sha256:index",
            "load_reference": "registry.example.test/caddy:2.10.2",
            "id": "sha256:caddy",
            "repo_tags": ["registry.example.test/caddy:2.10.2"],
            "repo_digests": ["registry.example.test/caddy@sha256:index"],
            "os": "linux",
            "architecture": "amd64",
            "variant": None,
            "platform": "linux/amd64",
            "image_size_bytes": 50,
        },
    }
    commands: list[list[str]] = []

    def fake_save(command: list[str], *, cwd: Path, env=None) -> None:
        commands.append(command)
        destination = Path(command[command.index("--output") + 1])
        destination.write_bytes(b"deduplicated docker archive")

    monkeypatch.setattr(
        release_packaging,
        "_inspect_docker_image",
        lambda image: dict(inspected[image]),
    )
    monkeypatch.setattr(release_packaging, "_run", fake_save)

    result = _save_docker_images(
        bundle,
        list(inspected),
        target_platform="linux/x86_64",
    )

    archive = bundle / "images" / "docker-images.tar"
    assert archive.is_file()
    assert len(commands) == 1
    assert commands[0][-2:] == [
        "registry.example.test/automl:0.8.0",
        "registry.example.test/caddy:2.10.2",
    ]
    assert {item["path"] for item in result} == {"images/docker-images.tar"}
    assert {item["platform"] for item in result} == {"linux/amd64"}
    assert {item["archive_size_bytes"] for item in result} == {archive.stat().st_size}
    assert {item["archive_sha256"] for item in result} == {
        hashlib.sha256(archive.read_bytes()).hexdigest()
    }


def test_digest_qualified_image_uses_a_loadable_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = "registry.example.test/caddy:2.10.2@sha256:index"
    inspection = [
        {
            "Id": "sha256:image",
            "RepoTags": ["registry.example.test/caddy:2.10.2"],
            "RepoDigests": ["registry.example.test/caddy@sha256:index"],
            "Os": "linux",
            "Architecture": "amd64",
            "Size": 123,
        }
    ]
    monkeypatch.setattr(
        release_packaging.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(inspection),
        ),
    )

    result = release_packaging._inspect_docker_image(image)

    assert result["reference"] == image
    assert result["load_reference"] == "registry.example.test/caddy:2.10.2"
    assert result["repo_digests"] == ["registry.example.test/caddy@sha256:index"]
    assert result["platform"] == "linux/amd64"


def test_docker_export_rejects_a_wrong_or_mixed_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    def inspected(image: str) -> dict[str, object]:
        architecture = "arm64" if image.endswith("arm") else "amd64"
        return {
            "reference": image,
            "load_reference": f"example.test/{image}:latest",
            "id": f"sha256:{image}",
            "repo_tags": [f"example.test/{image}:latest"],
            "repo_digests": [],
            "os": "linux",
            "architecture": architecture,
            "variant": None,
            "platform": f"linux/{architecture}",
            "image_size_bytes": 1,
        }

    monkeypatch.setattr(release_packaging, "_inspect_docker_image", inspected)

    with pytest.raises(ReleaseError, match="not requested linux/amd64"):
        _save_docker_images(bundle, ["api-arm"], target_platform="linux/amd64")
    with pytest.raises(ReleaseError, match="mixed platforms"):
        _save_docker_images(bundle, ["api-arm", "gateway-amd64"], target_platform=None)


def test_platform_validation_and_duplicate_images_fail_closed(tmp_path: Path) -> None:
    assert _normalize_platform("linux/x86_64") == "linux/amd64"
    assert _normalize_platform("linux/aarch64") == "linux/arm64"
    with pytest.raises(ReleaseError, match="os/architecture"):
        _normalize_platform("amd64")
    with pytest.raises(ReleaseError, match="duplicates"):
        _save_docker_images(
            tmp_path,
            ["example.test/api:latest", "example.test/api:latest"],
            target_platform=None,
        )


def test_target_platform_may_omit_an_image_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(
        release_packaging,
        "_inspect_docker_image",
        lambda image: {
            "reference": image,
            "load_reference": "example.test/api:arm64",
            "id": "sha256:arm64",
            "repo_tags": ["example.test/api:arm64"],
            "repo_digests": [],
            "os": "linux",
            "architecture": "arm64",
            "variant": "v8",
            "platform": "linux/arm64/v8",
            "image_size_bytes": 1,
        },
    )

    def fake_save(command: list[str], *, cwd: Path, env=None) -> None:
        Path(command[command.index("--output") + 1]).write_bytes(b"archive")

    monkeypatch.setattr(release_packaging, "_run", fake_save)

    result = _save_docker_images(
        bundle,
        ["example.test/api:arm64"],
        target_platform="linux/aarch64",
    )

    assert result[0]["platform"] == "linux/arm64/v8"
