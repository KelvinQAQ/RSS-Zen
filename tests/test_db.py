from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import rss_zen.db as db_module
from rss_zen.db import (
    ArticleInput,
    Database,
    ExtractionInput,
    FeedInput,
    TranslationInput,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "rss-zen.sqlite3")
    instance.initialize()
    return instance


def _feed() -> FeedInput:
    return FeedInput(
        name="Example feed",
        url="https://example.test/feed.xml",
        categories=("technology",),
        language="en",
        poll_interval_minutes=15,
    )


def _article(
    *,
    content: str = "Original body",
    guid: str | None = "article-1",
    canonical_url: str = "https://example.test/articles/one",
    published_at: str = "2026-08-11T10:00:00+00:00",
) -> ArticleInput:
    return ArticleInput(
        guid=guid,
        canonical_url=canonical_url,
        title="O'Reilly article",
        summary="Original summary",
        content=content,
        author="Author",
        categories=("technology",),
        published_at=published_at,
    )


def test_initialization_creates_current_schema(database: Database) -> None:
    assert database.schema_version() == 4
    assert database.table_names() >= {
        "feeds",
        "articles",
        "translations",
        "extractions",
        "export_runs",
        "sync_runs",
    }


def test_migration_snapshot_includes_uncheckpointed_wal_data(tmp_path: Path) -> None:
    """Pre-migration snapshots must include committed records still held in WAL."""
    database_path = tmp_path / "rss-zen.sqlite3"
    writer = sqlite3.connect(database_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.executescript(db_module._MIGRATION_1)
        writer.executescript(db_module._MIGRATION_2)
        writer.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        writer.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, "2026-08-11T00:00:00+00:00"), (2, "2026-08-11T00:00:01+00:00")],
        )
        writer.commit()
        writer.execute(
            """
            INSERT INTO feeds(name, url, categories_json, enabled, origin, created_at, updated_at)
            VALUES (?, ?, '[]', 1, 'config', ?, ?)
            """,
            (
                "Stored in WAL",
                "https://example.test/wal.xml",
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
            ),
        )
        writer.commit()

        Database(database_path).initialize()
    finally:
        writer.close()

    snapshots = list((tmp_path / "backups" / "pre-migration").glob("*.sqlite3"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("SELECT name FROM feeds").fetchone()[0] == "Stored in WAL"
    assert Database(database_path).schema_version() == 4


def test_batch_run_materializes_ordered_article_selection(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two"),
    ).article

    run = database.create_batch_run(
        command="translate",
        article_ids=(second.id, first.id),
        selector={"article_ids": [second.id, first.id]},
        limits={"provider_requests": 2},
    )

    assert run.command == "translate"
    assert database.batch_run_pending_article_ids(run.id) == (second.id, first.id)


def test_batch_run_tracks_item_and_run_lifecycle(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two"),
    ).article
    run = database.create_batch_run(
        command="translate",
        article_ids=(first.id, second.id),
        selector={"source": "Example feed"},
        limits={"provider_requests": 2},
    )

    database.complete_batch_run_item(run.id, first.id, status="succeeded")
    database.complete_batch_run_item(
        run.id, second.id, status="skipped", error_code="provider_budget_exhausted"
    )
    database.update_batch_run_status(run.id, status="interrupted")

    loaded = database.get_batch_run(run.id)
    assert loaded.status == "interrupted"
    assert loaded.selector == {"source": "Example feed"}
    assert database.batch_run_resumable_article_ids(run.id) == (second.id,)


def test_batch_run_rejects_unknown_id_and_wrong_command(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article
    run = database.create_batch_run(
        command="extract",
        article_ids=(article.id,),
        selector={},
        limits={},
    )

    with pytest.raises(KeyError):
        database.get_batch_run(999)
    with pytest.raises(ValueError, match="command"):
        database.require_batch_run_command(run.id, "translate")


def test_upsert_feed_updates_existing_record(database: Database) -> None:
    original = database.upsert_feed(_feed())
    updated = database.upsert_feed(
        FeedInput(
            name="Renamed feed",
            url="https://example.test/feed.xml",
            categories=("technology", "python"),
            language="en",
            poll_interval_minutes=30,
        )
    )

    assert updated.id == original.id
    assert updated.name == "Renamed feed"
    assert updated.categories == ("technology", "python")
    assert updated.poll_interval_minutes == 30


def test_reconcile_article_by_guid_or_link_updates_changed_content(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article())
    changed = database.reconcile_article(feed.id, _article(content="Changed body"))

    assert first.created is True
    assert first.content_changed is True
    assert changed.created is False
    assert changed.content_changed is True
    assert changed.article.id == first.article.id
    assert changed.article.content == "Changed body"

    same_link_without_guid = database.reconcile_article(
        feed.id,
        _article(content="Changed again", guid=None),
    )
    assert same_link_without_guid.article.id == first.article.id
    assert same_link_without_guid.content_changed is True


def test_list_articles_overview_filters_by_source_and_status(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    reconciled = database.reconcile_article(feed.id, _article())
    article = reconciled.article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="中文标题",
            summary=None,
            content=None,
            provider_name="google",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )

    overviews = database.list_articles_overview(target_language="zh-CN")
    assert len(overviews) == 1
    row = overviews[0]
    assert row.feed_name == "Example feed"
    assert row.translation_status == "succeeded"
    assert row.translation_provider == "google"
    assert row.extraction_status is None

    # published_after excludes an earlier article timestamp.
    late = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two",
                 published_at="2026-08-12T10:00:00+00:00"),
    )
    overviews = database.list_articles_overview(
        target_language="zh-CN", published_after="2026-08-12T00:00:00+00:00"
    )
    assert [row.article.id for row in overviews] == [late.article.id]


def test_processing_results_and_export_run_are_persisted(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article

    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="中文标题",
            summary="中文摘要",
            content="中文正文",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )
    extraction = database.record_extraction(
        ExtractionInput(
            article_id=article.id,
            provider_name="anysearch",
            source_url=article.canonical_url,
            content="Extracted content",
            status="succeeded",
            request_id="request-1",
        )
    )
    export_run = database.record_export_run(
        profile_name="daily",
        output_path=Path("exports/daily.md"),
        filters={"source": "Example feed"},
        article_count=1,
        status="succeeded",
    )

    translation = database.latest_translation(article.id, "zh-CN")
    assert translation is not None
    assert translation.title == "中文标题"
    assert translation.provider_name == "free"
    assert extraction.content == "Extracted content"
    assert export_run.article_count == 1


def test_list_articles_by_translation_status_returns_distinct_articles(
    database: Database,
) -> None:
    """Status selection returns one row per article, latest translation wins."""
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id, _article(guid="article-2", canonical_url="https://example.test/two")
    ).article
    database.save_translation(
        TranslationInput(
            article_id=first.id,
            target_language="zh-CN",
            title=None,
            summary=None,
            content=None,
            provider_name="free",
            provider_model=None,
            status="failed",
            source_hash="hash-1",
            error_code="translation_provider_error",
            error_message="boom",
            attempt_count=2,
            terminal=True,
        )
    )
    database.save_translation(
        TranslationInput(
            article_id=second.id,
            target_language="zh-CN",
            title="标题",
            summary="",
            content="内容",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash="hash-2",
        )
    )

    failed = database.list_articles_by_translation_status("zh-CN", status="failed")
    succeeded = database.list_articles_by_translation_status("zh-CN", status="succeeded")

    assert [article.id for article in failed] == [first.id]
    assert [article.id for article in succeeded] == [second.id]


def test_parameterized_sql_preserves_quote_characters(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article

    assert database.get_article(article.id).title == "O'Reilly article"
