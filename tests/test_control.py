from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rss_zen.control import FeedControlService
from rss_zen.db import Database
from rss_zen.errors import AppError
from rss_zen.http_client import FeedHttpClient
from rss_zen.network import FeedUrlPolicy

RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Public Feed</title><item><title>One</title><link>https://news.example/a</link></item></channel></rss>'


def _service(tmp_path, clock):
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=RSS)))
    policy = FeedUrlPolicy(resolver=lambda host, port: ["8.8.8.8"])
    http = FeedHttpClient(client, policy=policy)
    return database, FeedControlService(database, http, policy=policy, now=lambda: clock[0])


def test_public_feed_probe_add_disable_is_token_bound_and_audited(tmp_path) -> None:
    clock = [datetime(2026, 8, 14, tzinfo=UTC)]
    database, service = _service(tmp_path, clock)

    probe = service.probe("https://news.example/rss")
    assert probe.title == "Public Feed"
    assert probe.entry_count == 1
    assert database.list_feeds() == []

    feed = service.add(
        token=probe.token,
        url="https://news.example/rss",
        name="Public Feed",
        categories=("security",),
        actor="pi-agent",
    )
    assert feed.origin == "agent"
    assert feed.enabled is True
    disabled = service.disable(feed.id, actor="pi-agent")
    assert disabled.enabled is False
    assert database.list_feeds()[0].id == feed.id

    with pytest.raises(AppError) as excinfo:
        service.add(
            token=probe.token,
            url="https://news.example/rss",
            name="Again",
            categories=(),
            actor="pi-agent",
        )
    assert excinfo.value.code == "feed_probe_invalid"


def test_probe_rejects_private_query_and_expired_or_mismatched_token(tmp_path) -> None:
    clock = [datetime(2026, 8, 14, tzinfo=UTC)]
    database, service = _service(tmp_path, clock)
    with pytest.raises(AppError) as excinfo:
        service.probe("https://news.example/rss?token=private")
    assert excinfo.value.code == "feed_confirmation_required"

    probe = service.probe("https://news.example/rss")
    with pytest.raises(AppError) as excinfo:
        service.add(
            token=probe.token,
            url="https://other.example/rss",
            name="Mismatch",
            categories=(),
            actor="pi-agent",
        )
    assert excinfo.value.code == "feed_probe_invalid"

    probe = service.probe("https://news.example/rss")
    clock[0] += timedelta(minutes=16)
    with pytest.raises(AppError) as excinfo:
        service.add(
            token=probe.token,
            url="https://news.example/rss",
            name="Expired",
            categories=(),
            actor="pi-agent",
        )
    assert excinfo.value.code == "feed_probe_invalid"
    assert database.list_feeds() == []
