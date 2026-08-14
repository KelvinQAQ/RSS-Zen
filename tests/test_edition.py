from __future__ import annotations

from pathlib import Path

import pytest

from rss_zen.db import (
    ArticleInput,
    Database,
    ExtractionInput,
    FeedInput,
    TopicProfileInput,
    TranslationInput,
)
from rss_zen.edition import EditionBuilder
from rss_zen.editorial import EditorialAttempt, EditorialResult


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    return database


def _topic(
    database: Database,
    *,
    key: str = "indo-pacific",
    keywords: list[str] | None = None,
    max_candidates: int = 10,
    include_untranslated: bool = False,
):
    return database.create_topic_profile(
        TopicProfileInput(
            key=key,
            version=1,
            name="印太安全",
            timezone="Asia/Shanghai",
            delivery_deadline="07:30",
            lookback_hours=24,
            selection={
                "keywords": keywords or ["Taiwan"],
                "content_keywords": [],
                "keyword_match": "any",
                "sources": [],
                "categories": [],
                "feed_priority": ["Priority feed"],
                "dedupe_by_title": True,
                "include_untranslated": include_untranslated,
            },
            safety_limits={
                "max_candidates": max_candidates,
                "max_rendered_bytes": 200_000,
            },
        )
    )


def _article(database: Database, *, guid: str, title: str, content: str | None = None):
    feed = database.get_feed_by_url("https://example.test/feed.xml")
    if feed is None:
        feed = database.upsert_feed(
            FeedInput(
                name="Priority feed",
                url="https://example.test/feed.xml",
                language="en",
            )
        )
    return database.reconcile_article(
        feed.id,
        ArticleInput(
            guid=guid,
            canonical_url=f"https://example.test/{guid}",
            title=title,
            summary=f"Summary for {title}",
            content=content,
            author=None,
            categories=("security",),
            published_at="2026-08-13T22:00:00+00:00",
        ),
    ).article


def _translate(
    database: Database,
    article,
    *,
    title: str,
    summary: str | None = None,
    content: str | None = None,
) -> None:
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title=title,
            summary=summary,
            content=content,
            provider_name="test",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )


def test_builder_selects_dynamic_candidates_and_records_content_provenance(tmp_path: Path) -> None:
    database = _database(tmp_path)
    topic = _topic(database, max_candidates=2)
    extracted = _article(database, guid="a1", title="Taiwan naval update", content="RSS body")
    summarized = _article(database, guid="a2", title="Taiwan policy update")
    excluded = _article(database, guid="a3", title="Unrelated sports")
    _translate(database, extracted, title="台湾海军动态", content="RSS 中文正文")
    _translate(database, summarized, title="台湾政策动态", summary="中文摘要")
    _translate(database, excluded, title="体育新闻", summary="无关摘要")
    database.record_extraction(
        ExtractionInput(
            article_id=extracted.id,
            provider_name="anysearch",
            source_url=extracted.canonical_url,
            content="Extracted original",
            translated_content="提取后的中文全文",
            translation_provider_name="test",
            status="succeeded",
        )
    )

    result = EditionBuilder(
        database, target_language="zh-CN", output_directory=tmp_path / "editions"
    ).build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )

    assert result.article_count == 2
    assert result.degraded is False
    assert result.edition.status == "queued"
    assert result.delivery.status == "pending"
    assert result.artifact_path.is_file()
    rendered = result.artifact_path.read_text(encoding="utf-8")
    assert "台湾海军动态" in rendered
    assert "提取后的中文全文" in rendered
    assert "内容来源: 已提取全文" in rendered
    assert "台湾政策动态" in rendered
    assert "内容来源: RSS 摘要" in rendered
    assert "Unrelated sports" not in rendered
    assert database.edition_item_article_ids(result.edition.id) == (summarized.id, extracted.id)
    assert [item.content_source for item in database.edition_items(result.edition.id)] == [
        "rss_summary",
        "extracted_full_text",
    ]


def test_builder_is_idempotent_after_artifact_and_outbox_are_created(tmp_path: Path) -> None:
    database = _database(tmp_path)
    topic = _topic(database)
    article = _article(database, guid="a1", title="Taiwan update")
    _translate(database, article, title="台湾动态", summary="中文摘要")
    builder = EditionBuilder(
        database, target_language="zh-CN", output_directory=tmp_path / "editions"
    )

    first = builder.build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )
    initial_bytes = first.artifact_path.read_bytes()
    first.artifact_path.unlink()
    second = builder.build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )

    assert second.edition.id == first.edition.id
    assert second.delivery.id == first.delivery.id
    assert second.artifact_sha256 == first.artifact_sha256
    assert second.artifact_path.read_bytes() == initial_bytes

    with pytest.raises(ValueError, match="different target"):
        builder.build(
            topic,
            local_date="2026-08-14",
            deadline_at="2026-08-13T23:30:00+00:00",
            target_ref="chat:different-target",
        )


def test_builder_applies_and_persists_editorial_order_before_outbox(tmp_path: Path) -> None:
    database = _database(tmp_path)
    topic = _topic(database, max_candidates=2)
    first = _article(database, guid="a1", title="Taiwan first")
    second = _article(database, guid="a2", title="Taiwan second")
    _translate(database, first, title="第一篇", summary="摘要一")
    _translate(database, second, title="第二篇", summary="摘要二")

    class Editorial:
        calls = 0

        def attempt(self, request):
            self.calls += 1
            return EditorialAttempt(
                EditorialResult(
                    "编辑标题", "编辑导语", tuple(reversed(request.article_ids)), 10, 5
                ),
                False,
            )

    editorial = Editorial()
    builder = EditionBuilder(
        database,
        target_language="zh-CN",
        output_directory=tmp_path / "editions",
        editorial_service=editorial,
    )
    result = builder.build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )
    rendered = result.artifact_path.read_text(encoding="utf-8")
    assert rendered.startswith("# 编辑标题\n\n编辑导语")
    assert rendered.index("第一篇") < rendered.index("第二篇")
    assert editorial.calls == 1
    result.artifact_path.unlink()
    builder.build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )
    assert editorial.calls == 1
    assert result.artifact_path.read_text(encoding="utf-8").startswith("# 编辑标题")


def test_builder_recovers_running_editorial_as_fallback_without_calling_agent(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    topic = _topic(database, keywords=["No match"])
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    database.transition_edition_run(edition.id, status="refreshing")
    database.transition_edition_run(edition.id, status="selecting")
    database.freeze_edition_items(edition.id, ())
    database.begin_editorial(edition.id)

    class MustNotRun:
        def attempt(self, request):
            raise AssertionError("agent must not rerun after interrupted state")

    result = EditionBuilder(
        database,
        target_language="zh-CN",
        output_directory=tmp_path / "editions",
        editorial_service=MustNotRun(),
    ).build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )
    assert result.degraded is True
    assert result.edition.degraded_reason_code == "editorial_interrupted"
    assert "降级版" in result.artifact_path.read_text(encoding="utf-8")


def test_builder_renders_a_successful_empty_edition(tmp_path: Path) -> None:
    database = _database(tmp_path)
    topic = _topic(database, keywords=["No matching keyword"])

    result = EditionBuilder(
        database, target_language="zh-CN", output_directory=tmp_path / "editions"
    ).build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )

    assert result.article_count == 0
    assert result.degraded is False
    assert "今日无符合主题的更新" in result.artifact_path.read_text(encoding="utf-8")


def test_builder_can_render_bounded_untranslated_fallback_as_degraded(tmp_path: Path) -> None:
    database = _database(tmp_path)
    topic = _topic(database, include_untranslated=True, max_candidates=1)
    article = _article(
        database,
        guid="a1",
        title="Taiwan untranslated update",
        content="Original RSS body",
    )

    result = EditionBuilder(
        database, target_language="zh-CN", output_directory=tmp_path / "editions"
    ).build(
        topic,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
        target_ref="chat:approved-digest",
    )

    assert result.article_count == 1
    assert result.degraded is True
    assert result.edition.degraded_reason_code == "translation_incomplete"
    rendered = result.artifact_path.read_text(encoding="utf-8")
    assert "降级版" in rendered
    assert "Taiwan untranslated update" in rendered
    assert "Original RSS body" in rendered
    assert "内容来源: RSS 全文（原文）" in rendered
    assert database.edition_item_article_ids(result.edition.id) == (article.id,)
