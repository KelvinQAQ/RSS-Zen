"""Typed Agent-safe control operations for public unauthenticated feeds."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import feedparser

from rss_zen.db import Database, FeedInput, FeedRecord
from rss_zen.errors import AppError
from rss_zen.http_client import FeedHttpClient
from rss_zen.network import FeedUrlPolicy


@dataclass(frozen=True)
class FeedProbeResult:
    token: str
    normalized_url: str
    title: str | None
    entry_count: int
    expires_at: str


class FeedControlService:
    """Probe and mutate public feeds through bounded typed operations."""

    def __init__(
        self,
        database: Database,
        http: FeedHttpClient | None = None,
        *,
        policy: FeedUrlPolicy | None = None,
        now=lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._http = http
        self._policy = policy or FeedUrlPolicy()
        self._now = now

    def probe(self, url: str) -> FeedProbeResult:
        normalized = self._policy.validate(url)
        if urlsplit(normalized).query:
            raise AppError(
                "feed_confirmation_required",
                "public Agent feed probe does not accept URL query parameters",
            )
        if self._http is None:
            raise RuntimeError("feed probe requires an HTTP client")
        response = self._http.get_feed(normalized, {})
        try:
            parsed = feedparser.parse(response.content)
            entries = list(parsed.entries)
            if parsed.bozo and not entries:
                raise AppError("feed_parse_error", "feed could not be parsed")
            title = parsed.feed.get("title")
            title = title if isinstance(title, str) else None
        finally:
            response.close()
        token = str(uuid.uuid4())
        expires = (self._now() + timedelta(minutes=15)).isoformat()
        self._database.save_feed_probe(
            token=token,
            normalized_url=normalized,
            url_hash=_url_hash(normalized),
            feed_title=title,
            entry_count=len(entries),
            expires_at=expires,
        )
        return FeedProbeResult(token, normalized, title, len(entries), expires)

    def add(
        self, *, token: str, url: str, name: str, categories: tuple[str, ...], actor: str
    ) -> FeedRecord:
        if not name.strip() or len(name) > 200:
            raise AppError("invalid_feed_name", "feed name must be non-empty and bounded")
        _validate_actor(actor)
        normalized = self._policy.validate(url)
        if urlsplit(normalized).query:
            raise AppError(
                "feed_confirmation_required", "feed query parameters require confirmation"
            )
        now = self._now().isoformat()
        try:
            self._database.consume_feed_probe(token, url_hash=_url_hash(normalized), now=now)
        except ValueError as error:
            raise AppError(
                "feed_probe_invalid", "feed probe token is invalid", cause=error
            ) from error
        feed = self._database.upsert_feed(
            FeedInput(
                name=name.strip(),
                url=normalized,
                categories=categories,
                enabled=True,
                origin="agent",
            )
        )
        self._database.record_mutation_audit(
            actor=actor,
            operation="feed.add",
            target_type="feed",
            target_id=str(feed.id),
            outcome="succeeded",
            metadata={
                "host": urlsplit(normalized).hostname or "",
                "category_count": len(categories),
            },
        )
        return feed

    def disable(self, feed_id: int, *, actor: str) -> FeedRecord:
        _validate_actor(actor)
        feed = self._database.set_feed_enabled(feed_id, enabled=False)
        self._database.record_mutation_audit(
            actor=actor,
            operation="feed.disable",
            target_type="feed",
            target_id=str(feed.id),
            outcome="succeeded",
            metadata={"host": urlsplit(feed.url).hostname or ""},
        )
        return feed


def _validate_actor(actor: str) -> None:
    if not actor or len(actor) > 128 or any(character.isspace() for character in actor):
        raise AppError("invalid_actor", "actor must be a bounded opaque identifier")


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()
