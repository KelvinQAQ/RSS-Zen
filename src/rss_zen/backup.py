"""Verified local SQLite backups with bounded retention."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from rss_zen.errors import AppError


def backup_database(
    database_path: Path,
    backup_directory: Path,
    *,
    retention_days: int = 30,
    retention_count: int = 30,
    now: datetime | None = None,
) -> Path:
    """Create, verify, and retain a consistent SQLite backup.

    A backup is retained only while it is within both the UTC-day retention
    window and the newest ``retention_count`` snapshots. The just-created
    snapshot is always retained.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be at least one")
    if retention_count < 1:
        raise ValueError("retention_count must be at least one")
    if not database_path.is_file():
        raise AppError("database_not_found", f"database does not exist: {database_path}")

    backup_directory.mkdir(parents=True, exist_ok=True)
    created_at = now or datetime.now(UTC)
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    target = backup_directory / f"rss-zen-{timestamp}.sqlite3"
    try:
        create_verified_sqlite_snapshot(database_path, target)
        _prune_backups(
            backup_directory,
            retention_days=retention_days,
            retention_count=retention_count,
            now=created_at,
            keep=target,
        )
    except AppError:
        raise
    except OSError as error:
        raise AppError(
            "backup_failed", "unable to create a verified database backup", cause=error
        ) from error
    return target


def create_verified_sqlite_snapshot(database_path: Path, target: Path) -> Path:
    """Atomically create an integrity-checked SQLite online-backup snapshot.

    SQLite's backup API includes committed WAL content, unlike copying only the
    main database file. ``target`` is not published until integrity checking
    succeeds.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    try:
        source = sqlite3.connect(database_path)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        _verify_integrity(temporary)
        temporary.replace(target)
    except (OSError, sqlite3.Error) as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise AppError(
            "backup_failed", "unable to create a verified database backup", cause=error
        ) from error
    return target


def _verify_integrity(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if row is None or row[0] != "ok":
        raise AppError("backup_integrity_failed", "database backup failed integrity verification")


def _prune_backups(
    directory: Path,
    *,
    retention_days: int,
    retention_count: int,
    now: datetime,
    keep: Path,
) -> None:
    backups = sorted(directory.glob("rss-*.sqlite3"), key=lambda item: item.name, reverse=True)
    cutoff = now.astimezone(UTC).date() - timedelta(days=retention_days)
    eligible = [path for path in backups if path == keep or _backup_date(path) >= cutoff]
    retained = [keep, *[path for path in eligible if path != keep][: retention_count - 1]]
    for path in backups:
        if path not in retained:
            path.unlink()


def _backup_date(path: Path) -> date:
    """Read the UTC creation date from a project backup filename or mtime."""
    try:
        timestamp = path.stem.rsplit("-", 1)[-1]
        return datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").date()
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).date()
