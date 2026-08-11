from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rss_zen.db import Database, FeedInput
from rss_zen.errors import AppError
from rss_zen.http_client import FeedHttpClient
from rss_zen.models import LimitsSettings
from rss_zen.network import FeedUrlPolicy
from rss_zen.sync import FeedSyncService


def _fixture(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / name).read_bytes()


def _service(tmp_path: Path, handler: httpx.MockTransport) -> tuple[Database, FeedSyncService]:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    client = httpx.Client(transport=handler)
    http_client = FeedHttpClient(client, max_attempts=2, sleep=lambda _: None)
    return database, FeedSyncService(database, http_client)


def test_sync_uses_conditional_headers_and_handles_not_modified(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"', "Last-Modified": "Mon, 11 Aug 2026 10:00:00 GMT"},
                content=_fixture("sample.rss.xml"),
            )
        return httpx.Response(304)

    database, service = _service(tmp_path, httpx.MockTransport(handler))
    feed = database.upsert_feed(FeedInput(name="RSS", url="https://example.test/feed.xml"))

    first = service.sync_feed(feed)
    refreshed_feed = database.get_feed_by_url(feed.url)
    second = service.sync_feed(refreshed_feed)

    assert first.created_articles == 1
    assert second.not_modified is True
    assert requests[1].headers["if-none-match"] == '"v1"'
    assert requests[1].headers["if-modified-since"] == "Mon, 11 Aug 2026 10:00:00 GMT"


def test_sync_decodes_gzip_encoded_feed(tmp_path: Path) -> None:
    import gzip

    compressed = gzip.compress(_fixture("sample.rss.xml"))

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=compressed
        )

    database, service = _service(tmp_path, httpx.MockTransport(handler))
    feed = database.upsert_feed(FeedInput(name="Gzip RSS", url="https://example.test/gzip.xml"))

    result = service.sync_feed(feed)
    article = database.get_article(result.article_ids[0])

    assert result.created_articles == 1
    assert article.guid == "rss-item-1"


def test_sync_parses_atom_full_content(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_fixture("sample.atom.xml"))

    database, service = _service(tmp_path, httpx.MockTransport(handler))
    feed = database.upsert_feed(FeedInput(name="Atom", url="https://example.test/atom.xml"))

    result = service.sync_feed(feed)
    article = database.get_article(result.article_ids[0])

    assert result.created_articles == 1
    assert article.guid == "atom-item-1"
    assert article.content == "<p>Atom full body</p>"
    assert article.author == "Atom author"
    assert article.categories == ("News",)


def test_sync_updates_article_when_source_content_changes(tmp_path: Path) -> None:
    response_contents = [
        _fixture("sample.rss.xml"),
        _fixture("sample.rss.xml").replace(b"First summary", b"Updated"),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=response_contents.pop(0))

    database, service = _service(tmp_path, httpx.MockTransport(handler))
    feed = database.upsert_feed(FeedInput(name="RSS", url="https://example.test/feed.xml"))

    first = service.sync_feed(feed)
    second = service.sync_feed(database.get_feed_by_url(feed.url))

    assert first.created_articles == 1
    assert second.updated_articles == 1
    assert database.get_article(first.article_ids[0]).summary == "<p>Updated</p>"


def test_sync_isolates_feed_failure_from_other_feeds(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "broken.test":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, content=_fixture("sample.rss.xml"))

    database, service = _service(tmp_path, httpx.MockTransport(handler))
    broken = database.upsert_feed(FeedInput(name="Broken", url="https://broken.test/feed.xml"))
    working = database.upsert_feed(FeedInput(name="Working", url="https://working.test/feed.xml"))

    results = service.sync_all([broken, working])

    assert results[0].error_code == "feed_timeout"
    assert results[1].created_articles == 1


def test_http_client_retries_timeout_before_returning_response() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, text="ok")

    client = FeedHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=2, sleep=lambda _: None
    )

    response = client.get_feed("https://example.test/feed.xml", {})

    assert response.status_code == 200
    assert calls == 2


def test_http_client_rejects_redirect_to_private_address() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://internal.test/feed.xml"})

    client = FeedHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        policy=FeedUrlPolicy(
            resolver=lambda host, _port: (
                ("93.184.216.34",) if host == "example.test" else ("127.0.0.1",)
            )
        ),
    )

    with pytest.raises(AppError, match="non-public"):
        client.get_feed("https://example.test/feed.xml", {})


def test_http_client_rejects_oversized_response() -> None:
    client = FeedHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"Content-Length": "11"}, content=b"01234567890"
                )
            )
        ),
        max_response_bytes=10,
    )

    with pytest.raises(AppError, match="size limit"):
        client.get_feed("https://example.test/feed.xml", {})


def test_sync_respects_entry_and_article_size_limits(tmp_path: Path) -> None:
    content = _fixture("sample.rss.xml").replace(b"First summary", b"A" * 50)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = FeedSyncService(
        database,
        FeedHttpClient(client),
        limits=LimitsSettings(max_entries_per_feed=1, max_article_chars=10),
    )
    feed = database.upsert_feed(FeedInput(name="RSS", url="https://example.test/feed.xml"))

    result = service.sync_feed(feed)

    article = database.get_article(result.article_ids[0])
    assert result.created_articles == 1
    assert article is not None
    assert article.summary == "<p>AAAAAAA"
