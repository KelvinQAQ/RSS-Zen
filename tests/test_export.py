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


def _keyword_article(
    database: Database,
    *,
    guid: str,
    title: str,
    summary: str,
    content: str,
    zh_title: str | None = None,
    zh_summary: str | None = None,
    zh_content: str | None = None,
) -> None:
    feed = database.get_feed_by_url("https://example.test/keyword.xml")
    if feed is None:
        feed = database.upsert_feed(
            FeedInput(name="Keyword feed", url="https://example.test/keyword.xml")
        )
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid=guid,
            canonical_url=f"https://example.test/articles/{guid}",
            title=title,
            summary=summary,
            content=content,
            author="Author",
            categories=(),
            published_at="2026-08-11T10:00:00+00:00",
        ),
    ).article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title=zh_title,
            summary=zh_summary,
            content=zh_content,
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )


def _indopac_export(database: Database, output_path: Path, **filters) -> None:
    MarkdownExporter(database).export_profile(
        ExportProfile(
            name="indopac",
            output_path=output_path,
            title="印太",
            fields=["title", "content"],
            filters=ExportFilters(**filters),
        )
    )


def test_export_keyword_filter_matches_content_not_just_title(tmp_path: Path) -> None:
    """Relevance keywords must match the original AND translated text, including
    the body, so an article whose title does not mention the topic is still kept."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _keyword_article(
        database,
        guid="a",
        title="A build-up in the North",
        summary="",
        content="The People's Liberation Army deployed more ships in the South China Sea.",
        zh_title="北方的一次集结",
        zh_content="中国人民解放军在南海部署了更多舰船。",
    )
    _keyword_article(
        database,
        guid="b",
        title="A quiet domestic story",
        summary="",
        content="Local farmers held a seasonal market.",
        zh_title="一个平静的国内故事",
        zh_content="当地农民举办了一个季节性集市。",
    )
    output_path = tmp_path / "indopac.md"

    _indopac_export(
        database,
        output_path,
        keywords=["china"],
        content_keywords=["south china sea", "南海"],
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert "北方的一次集结" in rendered
    assert "平静的国内故事" not in rendered


def test_export_keyword_uses_word_boundaries(tmp_path: Path) -> None:
    """ASCII keywords must match whole words, so 'pla' does not match 'plans'."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _keyword_article(
        database,
        guid="a",
        title="The PLA commander spoke",
        summary="",
        content="",
        zh_title="解放军指挥官讲话",
        zh_content="",
    )
    _keyword_article(
        database,
        guid="b",
        title="They made plans for the harvest",
        summary="",
        content="",
        zh_title="他们为收获做了计划",
        zh_content="",
    )
    output_path = tmp_path / "indopac.md"

    _indopac_export(database, output_path, keywords=["pla"])

    rendered = output_path.read_text(encoding="utf-8")
    assert "解放军指挥官讲话" in rendered
    assert "为收获做了计划" not in rendered


def test_export_keyword_require_all(tmp_path: Path) -> None:
    """keyword_match=all requires every keyword to match within the title/summary tier."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()

    def add(guid: str, title: str, summary: str, zh_title: str, zh_summary: str) -> None:
        _keyword_article(
            database,
            guid=guid,
            title=title,
            summary=summary,
            content="",
            zh_title=zh_title,
            zh_summary=zh_summary,
            zh_content="",
        )

    add("a", "Naval exercise near Taiwan", "China and Taiwan", "台湾海军演习", "中国与台湾")
    add("b", "Naval exercise near Taiwan", "India and Japan", "台海军演", "印度与日本")
    output_path = tmp_path / "indopac.md"

    _indopac_export(
        database,
        output_path,
        keywords=["naval", "taiwan", "台湾", "中国"],
        keyword_match="all",
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert "中国与台湾" in rendered
    assert "印度与日本" not in rendered


def test_export_content_keywords_match_body_only(tmp_path: Path) -> None:
    """content_keywords scan the body only; broad keywords never scan full text."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _keyword_article(
        database,
        guid="a",
        title="Allied exercise concludes",
        summary="",
        content="Warships sailed through the Taiwan Strait during the exercise.",
        zh_title="盟军演习结束",
        zh_content="演习期间舰船穿越台湾海峡。",
    )
    _keyword_article(
        database,
        guid="b",
        title="Allied fleet returns home",
        summary="",
        content="The fleet returned to its home ports in Europe.",
        zh_title="盟军舰队返航",
        zh_content="舰队返回欧洲母港。",
    )
    output_path = tmp_path / "indopac.md"

    _indopac_export(
        database,
        output_path,
        keywords=["exercise"],
        content_keywords=["taiwan strait", "台湾海峡"],
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert "台湾海峡" in rendered
    assert "欧洲母港" not in rendered


def test_export_include_untranslated_falls_back_to_original(tmp_path: Path) -> None:
    """include_untranslated keeps failed/missing translations and falls back to the
    original-language text for matching and rendering."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.get_feed_by_url("https://example.test/keyword.xml")
    if feed is None:
        feed = database.upsert_feed(
            FeedInput(name="Keyword feed", url="https://example.test/keyword.xml")
        )
    database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="untrans",
            canonical_url="https://example.test/articles/untrans",
            title="PLA fleet sails through Pacific",
            summary="",
            content="The Chinese Navy crossed the Pacific last week.",
            author="Author",
            categories=(),
            published_at="2026-08-11T10:00:00+00:00",
        ),
    )
    output_path = tmp_path / "indopac.md"

    _indopac_export(database, output_path, keywords=["pla"], include_untranslated=True)

    rendered = output_path.read_text(encoding="utf-8")
    assert "PLA fleet sails through Pacific" in rendered
    assert "crossed the Pacific" in rendered


def test_export_untranslated_excluded_without_include_flag(tmp_path: Path) -> None:
    """Without include_untranslated, untranslated articles are still excluded."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.get_feed_by_url("https://example.test/keyword.xml")
    if feed is None:
        feed = database.upsert_feed(
            FeedInput(name="Keyword feed", url="https://example.test/keyword.xml")
        )
    database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="untrans",
            canonical_url="https://example.test/articles/untrans",
            title="PLA fleet sails through Pacific",
            summary="",
            content="The Chinese Navy crossed the Pacific last week.",
            author="Author",
            categories=(),
            published_at="2026-08-11T10:00:00+00:00",
        ),
    )
    output_path = tmp_path / "indopac.md"

    _indopac_export(database, output_path, keywords=["pla"])

    rendered = output_path.read_text(encoding="utf-8")
    assert "PLA fleet sails through Pacific" not in rendered
