from __future__ import annotations

from pathlib import Path

from rss_zen.db import (
    ArticleInput,
    Database,
    ExtractionInput,
    FeedInput,
    TranslationInput,
)
from rss_zen.export import MarkdownExporter
from rss_zen.models import ExportFilters, ExportProfile, PreprocessStep


def _article(database: Database, *, guid: str, url: str, published_at: str):
    feed = database.get_feed_by_url("https://example.test/feed.xml")
    if feed is None:
        feed = database.upsert_feed(
            FeedInput(name="Example feed", url="https://example.test/feed.xml")
        )
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid=guid,
            canonical_url=url,
            title="Original title",
            summary="Original summary",
            content="Original content",
            author="Author",
            categories=("Technology",),
            published_at=published_at,
        ),
    ).article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="相同标题",
            summary="<p>中文摘要</p>",
            content="<p>RSS 中文正文</p>",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )
    return article


def test_export_writes_directory_stable_anchors_and_full_text_fallback(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    first = _article(
        database,
        guid="article-1",
        url="https://example.test/articles/one",
        published_at="2026-08-11T10:00:00+00:00",
    )
    _ = _article(
        database,
        guid="article-2",
        url="https://example.test/articles/two",
        published_at="2026-08-10T10:00:00+00:00",
    )
    database.record_extraction(
        ExtractionInput(
            article_id=first.id,
            provider_name="anysearch",
            source_url=first.canonical_url,
            content="<p>Raw full body</p>",
            translated_content="<p>完整中文正文</p>",
            translation_provider_name="ai",
            status="succeeded",
        )
    )
    output_path = tmp_path / "exports" / "daily.md"
    output_path.parent.mkdir()
    output_path.write_text("old content", encoding="utf-8")
    profile = ExportProfile(
        name="daily",
        output_path=output_path,
        title="每日阅读",
        fields=["source_name", "published_at", "url", "content"],
        filters=ExportFilters(categories=["Technology"]),
        preprocess=[
            PreprocessStep(field="content", operation="strip_html"),
            PreprocessStep(field="published_at", operation="date_format", format="%Y-%m-%d"),
        ],
    )

    result = MarkdownExporter(database).export_profile(profile)

    rendered = output_path.read_text(encoding="utf-8")
    assert result.article_count == 2
    assert "old content" not in rendered
    assert "## 目录" in rendered
    assert "[相同标题](#article-1)" in rendered
    assert "[相同标题](#article-2)" in rendered
    assert '<a id="article-1"></a>' in rendered
    assert '<a id="article-2"></a>' in rendered
    assert "完整中文正文" in rendered
    assert "RSS 中文正文" in rendered
    assert "2026-08-11" in rendered
    assert not list(output_path.parent.glob("*.tmp"))
    assert result.export_run_id > 0


def test_export_profile_filters_by_source_and_full_text_requirement(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(
        database,
        guid="article-1",
        url="https://example.test/articles/one",
        published_at="2026-08-11T10:00:00+00:00",
    )
    database.record_extraction(
        ExtractionInput(
            article_id=article.id,
            provider_name="anysearch",
            source_url=article.canonical_url,
            content="Raw",
            translated_content="全文",
            translation_provider_name="ai",
            status="succeeded",
        )
    )
    profile = ExportProfile(
        name="full-only",
        output_path=tmp_path / "full.md",
        title="全文",
        filters=ExportFilters(sources=["Example feed"], require_full_text=True),
    )

    result = MarkdownExporter(database).export_profile(profile)

    assert result.article_count == 1
    assert "全文" in (tmp_path / "full.md").read_text(encoding="utf-8")


def test_export_escapes_untrusted_metadata_and_removes_unsafe_urls(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Bad\nsource", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="unsafe",
            canonical_url="javascript:alert(1)",
            title="Bad ](https://attacker.test)\n# heading",
            summary="Summary",
            content="Content",
            author="Author",
            categories=(),
            published_at=None,
        ),
    ).article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="Bad ](https://attacker.test)\n# heading",
            summary="Summary",
            content="Content",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )
    output_path = tmp_path / "unsafe.md"

    MarkdownExporter(database).export_profile(
        ExportProfile(
            name="unsafe", output_path=output_path, title="Unsafe", fields=["title", "url"]
        )
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert "javascript:" not in rendered
    assert r"Bad \](https://attacker.test)" not in rendered
    assert r"\# heading" in rendered
