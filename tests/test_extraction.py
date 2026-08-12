from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from rss_zen.db import ArticleInput, Database, FeedInput
from rss_zen.extraction import AnySearchExtractor, ExtractionResponse, ExtractionService
from rss_zen.models import AnySearchSettings
from rss_zen.translation import TextTranslation


def _article(database: Database):
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="article-1",
            canonical_url="https://example.test/articles/one",
            title="Article",
            summary="Summary",
            content=None,
            author=None,
            categories=(),
            published_at=None,
            source_language="en",
        ),
    ).article
    return article


def test_anysearch_posts_documented_request_and_requires_exact_url_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.anysearch.com/v1/search"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload == {
            "query": "https://example.test/articles/one",
            "tag": "general.general",
            "zone": "intl",
            "language": "zh-CN",
            "max_results": 3,
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "request-1",
                "data": {
                    "results": [
                        {"url": "https://other.test/article", "content": "Ignore this"},
                        {
                            "url": "https://example.test/articles/one",
                            "title": "Article",
                            "content": "Full article body",
                        },
                    ]
                },
            },
        )

    settings = AnySearchSettings(api_key="test-key", zone="intl", language="zh-CN")
    extractor = AnySearchExtractor(settings, httpx.Client(transport=httpx.MockTransport(handler)))

    result = extractor.extract("https://example.test/articles/one")

    assert result.content == "Full article body"
    assert result.request_id == "request-1"


@dataclass
class StaticExtractor:
    def extract(self, source_url: str) -> ExtractionResponse:
        return ExtractionResponse(
            content="Full body", source_url=source_url, request_id="request-1"
        )


@dataclass
class StaticTranslator:
    def translate_text(self, text: str, *, source_language: str | None) -> TextTranslation:
        assert source_language == "en"
        return TextTranslation("完整正文", "ai", "translation-model")


def test_explicit_extraction_persists_raw_and_translated_content(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    service = ExtractionService(database, StaticExtractor(), translator=StaticTranslator())

    results = service.extract_articles([article])

    stored = database.latest_extraction(article.id)
    assert results[0].status == "succeeded"
    assert stored is not None
    assert stored.content == "Full body"
    assert stored.translated_content == "完整正文"
    assert stored.translation_provider_name == "ai"


def test_extraction_only_selects_requested_article_ids(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    first = _article(database)
    second = database.reconcile_article(
        first.feed_id,
        ArticleInput(
            guid="article-2",
            canonical_url="https://example.test/articles/two",
            title="Second",
            summary=None,
            content=None,
            author=None,
            categories=(),
            published_at=None,
            source_language="en",
        ),
    ).article
    service = ExtractionService(database, StaticExtractor(), translator=StaticTranslator())

    results = service.extract_selected(article_ids=(second.id,))

    assert [result.article_id for result in results] == [second.id]
    assert database.latest_extraction(first.id) is None


def test_extraction_selects_by_published_window(tmp_path: Path) -> None:
    """extract_selected respects the published_after/before window."""
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    old = _article(database)
    recent = database.reconcile_article(
        old.feed_id,
        ArticleInput(
            guid="article-2",
            canonical_url="https://example.test/articles/two",
            title="Recent",
            summary=None,
            content=None,
            author=None,
            categories=(),
            published_at="2026-08-12T10:00:00+00:00",
            source_language="en",
        ),
    ).article
    service = ExtractionService(database, StaticExtractor(), translator=StaticTranslator())

    results = service.extract_selected(
        published_after="2026-08-12T00:00:00+00:00",
        published_before="2026-08-13T00:00:00+00:00",
    )

    assert [result.article_id for result in results] == [recent.id]
    assert database.latest_extraction(old.id) is None
