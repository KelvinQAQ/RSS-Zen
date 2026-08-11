"""Verified local SQLite backups with bounded retention."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from rss_zen.errors import AppError


def backup_database(
    database_path: Path,
    backup_directory: Path,
    *,
    retention_days: int = 30,
    now: datetime | None = None,
) -> Path:
    """Create, verify, and retain a consistent SQLite backup."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least one")
    if not database_path.is_file():
        raise AppError("database_not_found", f"database does not exist: {database_path}")

    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = backup_directory / f"rss-zen-{timestamp}.sqlite3"
    temporary = target.with_suffix(".sqlite3.tmp")
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
        _prune_backups(backup_directory, retention_days)
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


def _prune_backups(directory: Path, retention_days: int) -> None:
    backups = sorted(directory.glob("rss-zen-*.sqlite3"), key=lambda item: item.name, reverse=True)
    for path in backups[retention_days:]:
        path.unlink()
