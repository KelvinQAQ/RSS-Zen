from __future__ import annotations

from pathlib import Path

import pytest

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


def _article(*, content: str = "Original body", guid: str | None = "article-1") -> ArticleInput:
    return ArticleInput(
        guid=guid,
        canonical_url="https://example.test/articles/one",
        title="O'Reilly article",
        summary="Original summary",
        content=content,
        author="Author",
        categories=("technology",),
        published_at="2026-08-11T10:00:00+00:00",
    )


def test_initialization_creates_current_schema(database: Database) -> None:
    assert database.schema_version() == 3
    assert database.table_names() >= {
        "feeds",
        "articles",
        "translations",
        "extractions",
        "export_runs",
        "sync_runs",
    }


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


def test_parameterized_sql_preserves_quote_characters(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article

    assert database.get_article(article.id).title == "O'Reilly article"
