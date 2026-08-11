from __future__ import annotations

from pathlib import Path

import pytest

from rss_zen.db import Database, FeedInput
from rss_zen.errors import AppError
from rss_zen.feeds import import_opml_file, reconcile_config_feeds
from rss_zen.models import FeedConfig
from rss_zen.network import FeedUrlPolicy


def test_import_opml_preserves_nested_groups_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    opml = Path(__file__).parent / "fixtures" / "subscriptions.opml"

    first = import_opml_file(database, opml)
    second = import_opml_file(database, opml)

    assert first.imported == 2
    assert first.skipped == 1
    assert second.imported == 0
    assert second.updated == 2
    feeds = {feed.name: feed for feed in database.list_feeds()}
    assert feeds["Python Weekly"].categories == ("Technology",)
    assert feeds["AI Digest"].categories == ("Technology", "Nested")


def test_configured_feed_overrides_imported_feed_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    database.upsert_feed(
        FeedInput(
            name="OPML name",
            url="https://example.test/feed.xml",
            categories=("OPML",),
            origin="opml",
        )
    )

    result = reconcile_config_feeds(
        database,
        [
            FeedConfig(
                name="Configured name",
                url="https://example.test/feed.xml",
                categories=["Configured"],
                language="en",
                poll_interval_minutes=10,
            )
        ],
    )

    feed = database.get_feed_by_url("https://example.test/feed.xml")
    assert result.updated == 1
    assert feed is not None
    assert feed.name == "Configured name"
    assert feed.categories == ("Configured",)
    assert feed.language == "en"
    assert feed.origin == "config"


def test_malformed_opml_raises_safe_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    opml = tmp_path / "broken.opml"
    opml.write_text("<opml><body>", encoding="utf-8")

    with pytest.raises(AppError, match="OPML"):
        import_opml_file(database, opml)


def test_feed_url_policy_rejects_non_public_or_insecure_targets() -> None:
    policy = FeedUrlPolicy(resolver=lambda _host, _port: ("127.0.0.1",))

    with pytest.raises(AppError, match="HTTPS"):
        policy.validate("http://example.test/feed.xml")
    with pytest.raises(AppError, match="credentials"):
        policy.validate("https://user:pass@example.test/feed.xml")
    with pytest.raises(AppError, match="non-public"):
        policy.validate("https://example.test/feed.xml")


def test_feed_url_policy_accepts_global_resolved_target() -> None:
    policy = FeedUrlPolicy(resolver=lambda _host, _port: ("93.184.216.34",))

    assert policy.validate("https://EXAMPLE.test/feed.xml#fragment") == "https://example.test/feed.xml"
