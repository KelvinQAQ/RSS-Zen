from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from rss_zen.db import ArticleInput, ArticleRecord, Database, FeedInput
from rss_zen.edition_priority import EditionPriorityMatcher, build_edition_priority
from rss_zen.models import TopicConfig, TranslationProviderConfig, TranslationSettings
from rss_zen.translation import TranslationProviderError, TranslationService


def _db(tmp_path):
    return Database(tmp_path / "prio.sqlite3")


def _article(database: Database, *, guid: str, title: str, content: str | None = None, hours_ago: float = 1.0):
    feed = database.upsert_feed(FeedInput(name="Example Feed", url="https://example.test/feed.xml"))
    published = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
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
            published_at=published,
        ),
    ).article


def _topic(*, keywords=None, content_keywords=None, lookback_hours=24):
    return TopicConfig.model_validate(
        {
            "key": "indo-pacific",
            "version": 1,
            "name": "印太安全",
            "timezone": "Asia/Shanghai",
            "delivery_deadline": "07:30",
            "lookback_hours": lookback_hours,
            "preparation_minutes": 60,
            "selection": {
                "keywords": keywords or ["naval"],
                "content_keywords": content_keywords or ["China"],
                "sources": [],
                "categories": [],
                "dedupe_by_title": True,
            },
            "safety_limits": {"max_candidates": 10, "max_rendered_bytes": 100000},
        }
    )


def test_matcher_classifies_edition_candidate_by_keyword(tmp_path) -> None:
    db = _db(tmp_path); db.initialize()
    relevant = _article(db, guid="a1", title="China naval buildup in South China Sea")
    irrelevant = _article(db, guid="a2", title="Weather forecast for Tokyo", content="no keywords")
    matcher = build_edition_priority([_topic()])
    assert matcher.is_candidate(relevant) is True
    assert matcher.is_candidate(irrelevant) is False


def test_matcher_respects_lookback_window(tmp_path) -> None:
    db = _db(tmp_path); db.initialize()
    old = _article(db, guid="old", title="China naval report", hours_ago=100)
    matcher = build_edition_priority([_topic(lookback_hours=24)])
    assert matcher.is_candidate(old) is False


def test_translate_prioritized_orders_candidates_first(tmp_path) -> None:
    db = _db(tmp_path); db.initialize()
    relevant = _article(db, guid="r1", title="China submarine deployment", content="China navy")
    irrelevant = _article(db, guid="i1", title="Technology stock market update", content="no match")
    ordered_calls = []

    class RecordingProvider:
        name = "test"
        model = None
        def translate(self, text, source_language, target_language):
            ordered_calls.append(text)
            return "中文译文"

    matcher = build_edition_priority([_topic()])
    service = TranslationService(db, [RecordingProvider()], target_language="zh-CN", edition_priority=matcher)
    service.translate_prioritized([irrelevant, relevant], source_language_override="en")
    # Candidate "r1" must be consumed before non-candidate "i1". Providers see a
    # concatenation of title/summary/content, so assert on distinctive fragments.
    # Three provider calls per article (title/summary/content). The candidate's
    # block must come entirely before the non-candidate's block.
    assert len(ordered_calls) == 6
    candidate_block = " ".join(ordered_calls[:3])
    other_block = " ".join(ordered_calls[3:])
    assert "submarine" in candidate_block
    assert "stock market" in other_block
    assert "finance" not in candidate_block


def test_retry_due_retries_candidates_first(tmp_path) -> None:
    db = _db(tmp_path); db.initialize()
    relevant = _article(db, guid="r2", title="China naval exercise", content="China navy drill")
    irrelevant = _article(db, guid="i2", title="General finance news", content="nope")
    ordered_calls = []
    clock = {"at": datetime(2026, 8, 11, 0, 0, tzinfo=UTC)}

    def now():
        return clock["at"]

    class RecordingProvider:
        name = "free"
        model = None
        def translate(self, text, source_language, target_language):
            ordered_calls.append(text)
            raise TranslationProviderError("translation_rate_limited", "limit", retryable=True)

    matcher = build_edition_priority([_topic()])
    service = TranslationService(
        db, [RecordingProvider()], target_language="zh-CN", edition_priority=matcher, now=now
    )
    service.translate_article(relevant, source_language_override="en")
    service.translate_article(irrelevant, source_language_override="en")
    ordered_calls.clear()

    # Advance the clock well past the retry backoff so both are due.
    clock["at"] = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    outcomes = service.retry_due(limit=10)
    assert len(ordered_calls) == 2
    # Candidate retried first.
    assert "naval exercise" in ordered_calls[0]
    assert "finance news" in ordered_calls[1]
