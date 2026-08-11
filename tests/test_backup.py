from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rss_zen.backup import backup_database
from rss_zen.db import Database, FeedInput


def test_backup_is_readable_and_retention_is_bounded(tmp_path: Path) -> None:
    database_path = tmp_path / "rss-zen.sqlite3"
    database = Database(database_path)
    database.initialize()
    database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    backup_directory = tmp_path / "backups"

    first = backup_database(
        database_path,
        backup_directory,
        retention_days=2,
        now=datetime(2026, 8, 10, tzinfo=UTC),
    )
    second = backup_database(
        database_path,
        backup_directory,
        retention_days=2,
        now=datetime(2026, 8, 11, tzinfo=UTC),
    )
    third = backup_database(
        database_path,
        backup_directory,
        retention_days=2,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    with sqlite3.connect(third) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 1
    assert not first.exists()
    assert second.exists()
    assert third.exists()
    assert len(list(backup_directory.glob("rss-zen-*.sqlite3"))) == 2


def test_backup_leaves_unrelated_files_untouched(tmp_path: Path) -> None:
    database_path = tmp_path / "rss-zen.sqlite3"
    Database(database_path).initialize()
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    unrelated = backup_directory / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    backup_database(database_path, backup_directory, now=datetime.now(UTC) + timedelta(days=1))

    assert unrelated.read_text(encoding="utf-8") == "keep"
