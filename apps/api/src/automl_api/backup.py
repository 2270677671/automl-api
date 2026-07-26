from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4


_BACKUP_PREFIX = "automl-backup-"
_MANIFEST_NAME = "manifest.json"
_MANAGED_PATHS = ("automl.db", "objects", "ticket-secret")


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError("backup content must not contain symbolic links")
        if path.is_file() and path.name != _MANIFEST_NAME:
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise BackupError("managed state must not contain symbolic links")
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise BackupError("managed state must not contain symbolic links")
        if path.is_dir():
            target.mkdir(exist_ok=True)
            target.chmod(0o700)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o600)


def _sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError("automl.db is missing from the state directory")
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or str(result[0]) != "ok":
            raise BackupError("the copied SQLite database failed quick_check")
    finally:
        destination_connection.close()
        source_connection.close()
    destination.chmod(0o600)


def _database_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def _manifest(root: Path) -> dict[str, Any]:
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _regular_files(root)
    ]
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "service": "managed-automl-api",
        "files": files,
    }


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    root = Path(backup_dir).expanduser().resolve()
    manifest_path = root / _MANIFEST_NAME
    if not root.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError("backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is invalid") from error
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise BackupError("backup manifest schema is unsupported")

    expected: dict[str, tuple[int, str]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise BackupError("backup manifest contains an invalid file record")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or relative.startswith("/"):
            raise BackupError("backup manifest contains an unsafe path")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise BackupError("backup manifest path escapes the backup directory") from error
        expected[relative] = (int(item.get("size_bytes", -1)), str(item.get("sha256", "")))

    actual_files = _regular_files(root)
    actual_names = {path.relative_to(root).as_posix() for path in actual_files}
    if actual_names != set(expected):
        raise BackupError("backup file set does not match the manifest")
    for path in actual_files:
        relative = path.relative_to(root).as_posix()
        expected_size, expected_hash = expected[relative]
        if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
            raise BackupError(f"backup integrity check failed for {relative}")

    database = root / "automl.db"
    if not database.is_file():
        raise BackupError("backup does not contain automl.db")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if result is None or str(result[0]) != "ok":
        raise BackupError("backup SQLite database failed quick_check")
    return manifest


def _backup_directories(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and not path.is_symlink() and path.name.startswith(_BACKUP_PREFIX)
        ),
        key=lambda path: path.name,
        reverse=True,
    )


def _prune_backups(root: Path, retention_count: int) -> None:
    for expired in _backup_directories(root)[retention_count:]:
        shutil.rmtree(expired)


def create_backup(
    state_dir: str | Path,
    backup_root: str | Path,
    *,
    retention_count: int = 7,
) -> Path:
    if retention_count < 1:
        raise BackupError("retention_count must be positive")
    state = Path(state_dir).expanduser().resolve()
    destination_root = Path(backup_root).expanduser().resolve()
    if not state.is_dir():
        raise BackupError("state directory does not exist")
    try:
        destination_root.relative_to(state)
    except ValueError:
        pass
    else:
        raise BackupError("backup directory must be outside the live state directory")

    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root.chmod(0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{_BACKUP_PREFIX}{timestamp}-{uuid4().hex[:8]}"
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=destination_root))
    staging.chmod(0o700)
    destination = destination_root / name
    try:
        objects = state / "objects"
        for attempt in range(3):
            before = staging / ".automl-before.db"
            database = staging / "automl.db"
            copied_objects = staging / "objects"
            _sqlite_backup(state / "automl.db", before)
            if objects.exists():
                if not objects.is_dir():
                    raise BackupError("objects state path is not a directory")
                _copy_tree(objects, copied_objects)
            _sqlite_backup(state / "automl.db", database)
            if _database_content_hash(before) == _database_content_hash(database):
                before.unlink()
                break
            before.unlink(missing_ok=True)
            database.unlink(missing_ok=True)
            shutil.rmtree(copied_objects, ignore_errors=True)
            if attempt == 2:
                raise BackupError(
                    "state changed during three backup attempts; retry during a quieter window"
                )
        ticket_secret = state / "ticket-secret"
        if ticket_secret.exists():
            if ticket_secret.is_symlink() or not ticket_secret.is_file():
                raise BackupError("ticket-secret state path is invalid")
            shutil.copyfile(ticket_secret, staging / "ticket-secret")
            (staging / "ticket-secret").chmod(0o600)
        manifest = _manifest(staging)
        manifest_path = staging / _MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        verify_backup(staging)
        os.replace(staging, destination)
        _prune_backups(destination_root, retention_count)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_backup(
    backup_dir: str | Path,
    state_dir: str | Path,
    *,
    force: bool = False,
) -> Path | None:
    source = Path(backup_dir).expanduser().resolve()
    target = Path(state_dir).expanduser().resolve()
    verify_backup(source)
    if target == source or target in source.parents or source in target.parents:
        raise BackupError("restore target must not overlap the backup directory")
    if target.exists() and any(target.iterdir()) and not force:
        raise BackupError("restore target is not empty; pass --force after stopping the API")
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    staging = target / f".restore-{uuid4().hex}"
    rollback: Path | None = target / (
        f".pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    )
    staging.mkdir(exist_ok=False)
    staging.chmod(0o700)
    moved_old: list[str] = []
    installed: list[str] = []
    try:
        for name in _MANAGED_PATHS:
            item = source / name
            if not item.exists():
                continue
            destination = staging / name
            if item.is_dir():
                _copy_tree(item, destination)
            else:
                shutil.copyfile(item, destination)
                destination.chmod(0o600)

        current_managed = [name for name in _MANAGED_PATHS if (target / name).exists()]
        if current_managed:
            rollback.mkdir(mode=0o700)
            for name in current_managed:
                os.replace(target / name, rollback / name)
                moved_old.append(name)
        else:
            rollback = None

        for name in _MANAGED_PATHS:
            restored = staging / name
            if restored.exists():
                os.replace(restored, target / name)
                installed.append(name)
        staging.rmdir()
        return rollback
    except Exception:
        for name in installed:
            restored = target / name
            if restored.is_dir():
                shutil.rmtree(restored, ignore_errors=True)
            else:
                restored.unlink(missing_ok=True)
        if rollback is not None and rollback.exists():
            for name in moved_old:
                old = rollback / name
                if old.exists():
                    os.replace(old, target / name)
            try:
                rollback.rmdir()
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create, verify, or restore AutoML state backups.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create an online SQLite/object backup")
    create.add_argument("--state-dir", default=os.environ.get("AUTOML_STATE_DIR", ".automl-data"))
    create.add_argument("--backup-dir", default=os.environ.get("AUTOML_BACKUP_DIR", ""))
    create.add_argument(
        "--retention-count",
        type=_positive_int,
        default=_positive_int(os.environ.get("AUTOML_BACKUP_RETENTION_COUNT", "7")),
    )
    verify = subparsers.add_parser("verify", help="verify manifest, hashes, and SQLite integrity")
    verify.add_argument("backup")
    restore = subparsers.add_parser("restore", help="restore into a stopped service state path")
    restore.add_argument("backup")
    restore.add_argument("--state-dir", default=os.environ.get("AUTOML_STATE_DIR", ".automl-data"))
    restore.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            if not args.backup_dir:
                raise BackupError("--backup-dir or AUTOML_BACKUP_DIR is required")
            result: Any = {
                "status": "created",
                "backup": str(
                    create_backup(
                        args.state_dir,
                        args.backup_dir,
                        retention_count=args.retention_count,
                    )
                ),
            }
        elif args.command == "verify":
            manifest = verify_backup(args.backup)
            result = {"status": "verified", "backup": str(Path(args.backup).resolve()), **manifest}
        else:
            rollback = restore_backup(args.backup, args.state_dir, force=args.force)
            result = {
                "status": "restored",
                "state_dir": str(Path(args.state_dir).resolve()),
                "rollback_dir": None if rollback is None else str(rollback),
            }
    except BackupError as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BackupError", "create_backup", "restore_backup", "verify_backup"]
