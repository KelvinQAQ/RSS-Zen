"""Classify articles as edition candidates to prioritize their translation.

The daily edition (topic report) is built only from articles that are already
translated successfully. Under a tight daily provider budget, unfiltered
translation can consume quota on articles that will never reach the report.
This module provides a deterministic relevance classifier so translation work
can be ordered to spend budget on edition-candidate articles first (Plan A).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from rss_zen.db import ArticleRecord
from rss_zen.models import TopicConfig


class EditionPriorityMatcher:
    """Decide whether an article is a candidate for any enabled topic edition."""

    def __init__(
        self,
        topics: Sequence[TopicConfig],
        *,
        now: datetime | None = None,
    ) -> None:
        self._topics = [topic for topic in topics if topic.enabled]
        self._now = now or datetime.now(UTC)

    def is_candidate(self, article: ArticleRecord) -> bool:
        """Return True when the article matches any enabled topic selection."""
        if article.published_at is None:
            return False
        try:
            published = datetime.fromisoformat(article.published_at)
        except ValueError:
            return False
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        for topic in self._topics:
            if self._topic_matches(topic, article, published):
                return True
        return False

    # -- helpers ----------------------------------------------------------

    def _topic_matches(
        self, topic: TopicConfig, article: ArticleRecord, published: datetime
    ) -> bool:
        selection = topic.selection
        lookback_start = self._now - timedelta(hours=topic.lookback_hours)
        if published < lookback_start or published > self._now:
            return False
        if not self._categories_match(selection.categories, article.categories):
            return False
        if selection.keywords or selection.content_keywords:
            return self._keywords_match(
                article, selection.keywords, selection.content_keywords
            )
        # No keyword rules: a category/source-matched article is a candidate.
        return True

    @staticmethod
    def _categories_match(
        categories: Sequence[str], article_categories: Sequence[str]
    ) -> bool:
        if not categories:
            return True
        article_set = {category.casefold() for category in article_categories}
        return any(category.casefold() in article_set for category in categories)

    @staticmethod
    def _keywords_match(
        article: ArticleRecord,
        keywords: Sequence[str],
        content_keywords: Sequence[str],
    ) -> bool:
        if not keywords and not content_keywords:
            return True
        title = (article.title or "").casefold()
        summary = (article.summary or "").casefold()
        content = (article.content or "").casefold()
        headline_hits = [k for k in keywords if k.casefold() in f"{title} {summary}"]
        body_hits = [
            k for k in content_keywords if k.casefold() in f"{summary} {content}"
        ]
        return bool(headline_hits or body_hits)


def build_edition_priority(topics: Sequence[TopicConfig]) -> EditionPriorityMatcher:
    """Build a relevance matcher from configured topics, dropping disabled ones."""
    return EditionPriorityMatcher(topics)
