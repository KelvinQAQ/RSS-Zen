"""RSS and Atom synchronization pipeline."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import feedparser

from rss_zen.db import ArticleInput, Database, FeedRecord
from rss_zen.errors import AppError
from rss_zen.http_client import FeedHttpClient
from rss_zen.models import LimitsSettings
from rss_zen.translation import TranslationService


@dataclass(frozen=True)
class FeedSyncResult:
    """The outcome of synchronizing one feed."""

    feed_id: int
    created_articles: int = 0
    updated_articles: int = 0
    not_modified: bool = False
    article_ids: tuple[int, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class FeedSyncService:
    """Synchronize persisted feed records without cross-feed failure propagation."""

    def __init__(
        self,
        database: Database,
        http_client: FeedHttpClient,
        translation_service: TranslationService | None = None,
        *,
        limits: LimitsSettings | None = None,
    ) -> None:
        self._database = database
        self._http_client = http_client
        self._translation_service = translation_service
        self._limits = limits or LimitsSettings()

    def sync_all(self, feeds: list[FeedRecord]) -> list[FeedSyncResult]:
        """Synchronize all requested feeds, retaining a result for every feed."""
        results = []
        for feed in feeds:
            try:
                results.append(self.sync_feed(feed))
            except AppError as error:
                self._database.record_feed_failure(
                    feed.id, error_code=error.code, error_message=error.message
                )
                results.append(
                    FeedSyncResult(
                        feed_id=feed.id,
                        error_code=error.code,
                        error_message=error.message,
                    )
                )
        return results

    def sync_feed(self, feed: FeedRecord) -> FeedSyncResult:
        """Fetch, parse, and reconcile one feed."""
        response = self._http_client.get_feed(feed.url, _conditional_headers(feed))
        try:
            if response.status_code == 304:
                self._database.record_feed_success(feed.id, etag=None, last_modified=None)
                return FeedSyncResult(feed_id=feed.id, not_modified=True)

            entries = _parse_entries(
                response.content, max_entries=self._limits.max_entries_per_feed
            )
            created = 0
            updated = 0
            article_ids = []
            for entry in entries:
                article = _entry_to_article(
                    entry, feed.url, max_article_chars=self._limits.max_article_chars
                )
                if article is None:
                    continue
                reconciliation = self._database.reconcile_article(feed.id, article)
                article_ids.append(reconciliation.article.id)
                if reconciliation.created:
                    created += 1
                elif reconciliation.content_changed:
                    updated += 1
                if self._translation_service and (
                    reconciliation.created or reconciliation.content_changed
                ):
                    self._translation_service.translate_article(
                        reconciliation.article,
                        source_language_override=feed.language,
                    )
            self._database.record_feed_success(
                feed.id,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
            return FeedSyncResult(
                feed_id=feed.id,
                created_articles=created,
                updated_articles=updated,
                article_ids=tuple(article_ids),
            )
        finally:
            response.close()


def _conditional_headers(feed: FeedRecord) -> dict[str, str]:
    headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified
    return headers


def _parse_entries(content: bytes, *, max_entries: int) -> list[object]:
    parsed = feedparser.parse(content)
    entries = list(parsed.entries)[:max_entries]
    if parsed.bozo and not entries:
        raise AppError("feed_parse_error", "feed could not be parsed")
    return entries


def _entry_to_article(
    entry: object, feed_url: str, *, max_article_chars: int = 500_000
) -> ArticleInput | None:
    values = entry
    link = values.get("link")
    if not isinstance(link, str) or not link.strip():
        return None
    canonical_url = _normalize_article_url(urljoin(feed_url, link))
    content = _bounded_text(_entry_content(values), max_article_chars)
    summary = _bounded_text(_string_value(values.get("summary")), max_article_chars)
    tags = values.get("tags", [])
    categories = tuple(term for tag in tags if (term := _string_value(tag.get("term"))) is not None)
    return ArticleInput(
        guid=_string_value(values.get("id")),
        canonical_url=canonical_url,
        title=(
            _bounded_text(_string_value(values.get("title")), max_article_chars)
            or "Untitled article"
        ),
        summary=summary,
        content=content,
        author=_bounded_text(_string_value(values.get("author")), max_article_chars),
        categories=categories,
        published_at=_entry_timestamp(values, "published_parsed", "published"),
        source_updated_at=_entry_timestamp(values, "updated_parsed", "updated"),
    )


def _entry_content(values: object) -> str | None:
    contents = values.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        return _string_value(first.get("value"))
    return None


def _entry_timestamp(values: object, parsed_key: str, text_key: str) -> str | None:
    parsed_value = values.get(parsed_key)
    if parsed_value is not None:
        return datetime.fromtimestamp(calendar.timegm(parsed_value), UTC).isoformat()
    return _string_value(values.get(text_key))


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_article_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AppError("invalid_article_url", "article URL must use HTTP or HTTPS")
    normalized = SplitResult(
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""
    )
    return urlunsplit(normalized)


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]
