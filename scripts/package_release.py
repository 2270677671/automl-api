#!/usr/bin/env python3
"""Build a verifiable delivery bundle for an external Agent platform.

The bundle contains the API and SDK wheels, the two OpenAPI contracts, the
container deployment files, integration documentation, runnable examples, and
SHA-256 metadata. It can optionally include Docker images exported with
``docker save``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.-]+-[0-9][A-Za-z0-9_.-]*-py3-none-any\.whl$")


class ReleaseError(RuntimeError):
    """Raised when the release inputs are inconsistent or incomplete."""


def _read_version(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            project = tomllib.load(stream)["project"]
        version = project["version"]
    except (KeyError, OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"Cannot read project version from {path}") from error
    if not isinstance(version, str) or not version:
        raise ReleaseError(f"Project version in {path} is invalid")
    return version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, env=env)
    except FileNotFoundError as error:
        raise ReleaseError(f"Required command is not available: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        rendered = " ".join(command)
        raise ReleaseError(f"Command failed ({error.returncode}): {rendered}") from error


def _verify_contract_generation() -> None:
    _run(
        [sys.executable, str(ROOT / "scripts" / "generate_agent_openapi.py"), "--check"],
        cwd=ROOT,
    )


def _verify_versions(version: str) -> None:
    sdk_version = _read_version(ROOT / "packages" / "python_sdk" / "pyproject.toml")
    if sdk_version != version:
        raise ReleaseError(f"API and SDK versions differ: {version!r} vs {sdk_version!r}")

    checks = {
        ROOT / "apps" / "api" / "src" / "automl_api" / "version.py": [
            f'__version__ = "{version}"',
        ],
        ROOT / "apps" / "api" / "src" / "automl_api" / "app.py": [
            "version=SERVICE_VERSION",
        ],
        ROOT / "packages" / "python_sdk" / "src" / "automl_sdk" / "__init__.py": [
            f'__version__ = "{version}"',
        ],
        ROOT / "packages" / "python_sdk" / "src" / "automl_sdk" / "client.py": [
            f'User-Agent": "automl-python-sdk/{version}"',
        ],
        ROOT / "compose.yaml": [f"AUTOML_IMAGE:-managed-automl-api:{version}"],
        ROOT / "compose.gpu.yaml": [f"AUTOML_GPU_IMAGE:-managed-automl-api:{version}-cuda"],
        ROOT / "compose.gpu-direct.yaml": [f"AUTOML_GPU_IMAGE:-managed-automl-api:{version}-cuda"],
        ROOT / "openapi" / "automl-api.yaml": [f"  version: {version}"],
        ROOT / "openapi" / "automl-agent-tools.yaml": [f"  version: {version}"],
        ROOT / "CHANGELOG.md": [f"## {version} - "],
    }
    for path, expected_fragments in checks.items():
        content = path.read_text(encoding="utf-8")
        missing = [fragment for fragment in expected_fragments if fragment not in content]
        if missing:
            raise ReleaseError(f"Version {version} is not synchronized in {path}: {missing[0]!r}")


def _wheel_candidates(directory: Path, prefix: str, version: str) -> list[Path]:
    normalized_version = version.replace("-", "_")
    return sorted(
        path
        for path in directory.glob(f"{prefix}-{normalized_version}-*.whl")
        if path.is_file() and _WHEEL_NAME.fullmatch(path.name)
    )


def _build_wheels(python: str, version: str, destination: Path) -> tuple[Path, Path]:
    api_output = destination / "api-build"
    sdk_output = destination / "sdk-build"
    api_output.mkdir(parents=True)
    sdk_output.mkdir(parents=True)
    build_environment = os.environ.copy()
    build_environment.setdefault(
        "PIP_INDEX_URL",
        build_environment.get("AUTOML_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    )
    _run(
        [python, "-m", "build", "--wheel", "--outdir", str(api_output), "."],
        cwd=ROOT,
        env=build_environment,
    )
    _run(
        [python, "-m", "build", "--wheel", "--outdir", str(sdk_output), "."],
        cwd=ROOT / "packages" / "python_sdk",
        env=build_environment,
    )
    api = _wheel_candidates(api_output, "managed_automl_skeleton", version)
    sdk = _wheel_candidates(sdk_output, "automl_sdk", version)
    if len(api) != 1 or len(sdk) != 1:
        raise ReleaseError("Wheel build did not produce exactly one API and one SDK wheel")
    return api[0], sdk[0]


def _existing_wheels(version: str) -> tuple[Path, Path]:
    api = _wheel_candidates(ROOT / "dist", "managed_automl_skeleton", version)
    sdk = _wheel_candidates(ROOT / "packages" / "python_sdk" / "dist", "automl_sdk", version)
    if len(api) != 1 or len(sdk) != 1:
        raise ReleaseError(
            "--skip-build requires one API wheel in dist/ and one SDK wheel in "
            "packages/python_sdk/dist/"
        )
    return api[0], sdk[0]


def _copy_inputs(bundle: Path, api_wheel: Path, sdk_wheel: Path) -> None:
    files = {
        "README.md": "README.md",
        "CHANGELOG.md": "CHANGELOG.md",
        "LICENSE": "LICENSE",
        "NOTICE": "NOTICE",
        "pyproject.toml": "pyproject.toml",
        "uv.lock": "uv.lock",
        "requirements.production.lock": "requirements.production.lock",
        "Dockerfile": "Dockerfile",
        "compose.yaml": "compose.yaml",
        "compose.gpu.yaml": "compose.gpu.yaml",
        "compose.gpu-direct.yaml": "compose.gpu-direct.yaml",
        "compose.dual-ip.yaml": "compose.dual-ip.yaml",
        "compose.production-single.yaml": "compose.production-single.yaml",
        ".dockerignore": ".dockerignore",
        ".env.example": ".env.example",
        ".env.gpu.example": ".env.gpu.example",
        ".env.production.example": ".env.production.example",
        ".env.production-single.example": ".env.production-single.example",
        ".github/workflows/ci.yml": ".github/workflows/ci.yml",
        "docs/README.md": "docs/README.md",
        "docs/external-agent-integration.md": "docs/external-agent-integration.md",
        "docs/api-usage.md": "docs/api-usage.md",
        "docs/api-route-reference.md": "docs/api-route-reference.md",
        "docs/complete-api-design.md": "docs/complete-api-design.md",
        "docs/framework-backends.md": "docs/framework-backends.md",
        "docs/production-delivery.md": "docs/production-delivery.md",
        "docs/reproduction-guide.md": "docs/reproduction-guide.md",
        "docs/single-node-production.md": "docs/single-node-production.md",
        "docs/test-report-0.8.0.md": "docs/test-report-0.8.0.md",
        "docs/user-manual.md": "docs/user-manual.md",
    }
    for source_name, destination_name in files.items():
        source = ROOT / source_name
        if not source.is_file():
            raise ReleaseError(f"Required release input is missing: {source}")
        destination = bundle / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for source_name in (
        "scripts/init_single_node_production.sh",
        "scripts/issue_hs256_token.py",
    ):
        source = ROOT / source_name
        if not source.is_file():
            raise ReleaseError(f"Required release input is missing: {source}")
        destination = bundle / source_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for source_name in ("apps/api/src", "openapi", "deploy", "examples"):
        source = ROOT / source_name
        if not source.is_dir():
            raise ReleaseError(f"Required release input is missing: {source}")
        shutil.copytree(
            source,
            bundle / source_name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    wheel_dir = bundle / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(api_wheel, wheel_dir / api_wheel.name)
    shutil.copy2(sdk_wheel, wheel_dir / sdk_wheel.name)


def _inspect_docker_image(image: str) -> dict[str, Any]:
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(inspect.stdout)
        metadata = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else None
    except FileNotFoundError as error:
        raise ReleaseError("--docker-image requires the docker CLI") from error
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"Docker image is not available: {image}") from error
    except json.JSONDecodeError as error:
        raise ReleaseError(f"Docker returned invalid inspection data for {image}") from error
    if not isinstance(metadata, dict) or not metadata.get("Id"):
        raise ReleaseError(f"Docker returned incomplete inspection data for {image}")
    operating_system = str(metadata.get("Os", "unknown"))
    architecture = str(metadata.get("Architecture", "unknown"))
    variant = str(metadata.get("Variant") or "")
    platform = f"{operating_system}/{architecture}"
    if variant:
        platform = f"{platform}/{variant}"
    repo_tags = [str(value) for value in metadata.get("RepoTags") or []]
    requested_named_reference = image.partition("@")[0]
    separator = requested_named_reference.rfind(":")
    requested_repository = (
        requested_named_reference[:separator]
        if separator > requested_named_reference.rfind("/")
        else requested_named_reference
    )
    matching_tags = [
        value for value in repo_tags if value.rsplit(":", 1)[0] == requested_repository
    ]
    load_reference = image if "@" not in image and not image.startswith("sha256:") else ""
    if not load_reference:
        load_reference = (
            requested_named_reference
            if requested_named_reference in matching_tags
            else (matching_tags[0] if matching_tags else "")
        )
    if not load_reference:
        raise ReleaseError(
            f"Docker image {image} has no tagged reference that docker load can restore; "
            "tag the inspected image before packaging"
        )
    return {
        "reference": image,
        "load_reference": load_reference,
        "id": str(metadata["Id"]),
        "repo_tags": repo_tags,
        "repo_digests": [str(value) for value in metadata.get("RepoDigests") or []],
        "os": operating_system,
        "architecture": architecture,
        "variant": variant or None,
        "platform": platform,
        "image_size_bytes": int(metadata.get("Size", 0)),
    }


def _normalize_platform(value: str) -> str:
    aliases = {"aarch64": "arm64", "x86_64": "amd64"}
    parts = [part.strip().lower() for part in value.split("/")]
    if len(parts) not in {2, 3} or any(
        not part or re.fullmatch(r"[a-z0-9_.-]+", part) is None for part in parts
    ):
        raise ReleaseError("--target-platform must use os/architecture[/variant]")
    parts[1] = aliases.get(parts[1], parts[1])
    return "/".join(parts)


def _save_docker_images(
    bundle: Path,
    images: list[str],
    *,
    target_platform: str | None,
) -> list[dict[str, Any]]:
    if not images:
        return []
    if len(set(images)) != len(images):
        raise ReleaseError("--docker-image values must not contain duplicates")

    metadata = [_inspect_docker_image(image) for image in images]
    platforms = {str(item["platform"]) for item in metadata}
    if len(platforms) != 1:
        rendered = ", ".join(sorted(platforms))
        raise ReleaseError(f"Docker images target mixed platforms: {rendered}")
    if target_platform is not None:
        expected_platform = _normalize_platform(target_platform)
        actual_platform = next(iter(platforms))
        expected_has_variant = expected_platform.count("/") == 2
        platform_matches = actual_platform == expected_platform or (
            not expected_has_variant and actual_platform.startswith(f"{expected_platform}/")
        )
        if not platform_matches:
            raise ReleaseError(
                f"Docker image platform is {actual_platform}, not requested {expected_platform}"
            )

    load_references = [str(item["load_reference"]) for item in metadata]
    if len(set(load_references)) != len(load_references):
        raise ReleaseError("Docker images resolve to duplicate loadable tags")
    destination = bundle / "images" / "docker-images.tar"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["docker", "save", "--output", str(destination), *load_references],
        cwd=ROOT,
    )
    if not destination.is_file():
        raise ReleaseError("docker save did not create the requested image archive")
    archive_path = destination.relative_to(bundle).as_posix()
    archive_size = destination.stat().st_size
    archive_sha256 = _sha256(destination)
    for item in metadata:
        item.update(
            {
                "path": archive_path,
                "archive_size_bytes": archive_size,
                "archive_sha256": archive_sha256,
            }
        )
    return metadata


def _write_metadata(
    bundle: Path,
    *,
    version: str,
    docker: list[dict[str, Any]] | None,
) -> None:
    content_files = [
        path for path in _files(bundle) if path.name not in {"bundle-manifest.json", "SHA256SUMS"}
    ]
    entries = [
        {
            "path": path.relative_to(bundle).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in content_files
    ]
    manifest: dict[str, Any] = {
        "schema_version": "2",
        "service_version": version,
        "api_version": "v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "artifacts": entries,
        "docker_image": docker[0] if docker and len(docker) == 1 else None,
        "docker_images": docker or [],
    }
    manifest_path = bundle / "bundle-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    checksum_files = sorted(path for path in _files(bundle) if path.name != "SHA256SUMS")
    checksum_lines = [
        f"{_sha256(path)}  {path.relative_to(bundle).as_posix()}" for path in checksum_files
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="ascii")


def _archive(bundle: Path, archive: Path) -> None:
    if archive.exists():
        raise ReleaseError(f"Archive already exists: {archive}; choose another --archive path")
    if archive.resolve().is_relative_to(bundle.resolve()):
        raise ReleaseError("Archive path must be outside the bundle directory")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as stream:
        for path in sorted(_files(bundle)):
            stream.add(path, arcname=Path(bundle.name) / path.relative_to(bundle))


def _default_archive_path(bundle: Path) -> Path:
    """Keep the complete version/timestamp in the sibling archive name."""

    return bundle.parent / f"{bundle.name}.tar.gz"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="bundle directory (default: dist/releases/<service>-<version>-<UTC timestamp>)",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse wheels already present in dist/ and packages/python_sdk/dist/",
    )
    parser.add_argument(
        "--docker-image",
        action="append",
        default=[],
        help="export a local image with docker save; repeat for API, GPU, and gateway images",
    )
    parser.add_argument(
        "--target-platform",
        help="require every exported image to match os/architecture[/variant]",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="optional .tar.gz path; defaults to a sibling archive unless --no-archive is set",
    )
    parser.add_argument("--no-archive", action="store_true", help="do not create a tar.gz archive")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used for wheel builds (default: current interpreter)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        version = _read_version(ROOT / "pyproject.toml")
        _verify_versions(version)
        _verify_contract_generation()
        if args.output is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = ROOT / "dist" / "releases" / f"managed-automl-{version}-{stamp}"
        else:
            output = args.output if args.output.is_absolute() else ROOT / args.output
        output = output.resolve()
        if output.exists():
            raise ReleaseError(f"Bundle directory already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix=".automl-release-", dir=output.parent) as temporary:
            staging_root = Path(temporary)
            staged_bundle = staging_root / "bundle"
            staged_bundle.mkdir()
            wheel_paths = (
                _existing_wheels(version)
                if args.skip_build
                else _build_wheels(args.python, version, staging_root / "wheel-build")
            )
            _copy_inputs(staged_bundle, *wheel_paths)
            if args.target_platform and not args.docker_image:
                raise ReleaseError("--target-platform requires at least one --docker-image")
            docker_metadata = _save_docker_images(
                staged_bundle,
                args.docker_image,
                target_platform=args.target_platform,
            )
            _write_metadata(staged_bundle, version=version, docker=docker_metadata)
            if output.exists():
                raise ReleaseError(f"Bundle directory was created concurrently: {output}")
            staged_bundle.rename(output)

        if not args.no_archive:
            archive = args.archive
            if archive is None:
                archive = _default_archive_path(output)
            elif not archive.is_absolute():
                archive = ROOT / archive
            _archive(output, archive.resolve())
        print(f"release bundle: {output}")
        if not args.no_archive:
            print(f"release archive: {archive.resolve()}")
        return 0
    except ReleaseError as error:
        print(f"release packaging failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
