from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from rss_zen.cli import app
from rss_zen.control import FeedControlService
from rss_zen.db import Database
from rss_zen.errors import AppError
from rss_zen.http_client import FeedHttpClient
from rss_zen.network import FeedUrlPolicy

RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Public Feed</title><item><title>One</title><link>https://news.example/a</link></item></channel></rss>'
RUNNER = CliRunner()


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


def test_cli_probe_add_list_disable_and_audit_use_stable_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "rss-zen.toml"
    config.write_text(
        """
[database]
path = "rss-zen.sqlite3"
[translation]
[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
""",
        encoding="utf-8",
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        "rss_zen.network.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "rss_zen.cli.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=RSS))
        ),
    )
    probe = RUNNER.invoke(
        app, ["feed-probe", "--url", "https://news.example/rss", "-c", str(config)]
    )
    assert probe.exit_code == 0
    import json

    token = json.loads(probe.stdout)["token"]
    added = RUNNER.invoke(
        app,
        [
            "feed-add",
            "--probe-token",
            token,
            "--url",
            "https://news.example/rss",
            "--name",
            "Public",
            "--category",
            "security",
            "-c",
            str(config),
        ],
    )
    assert added.exit_code == 0
    feed_id = json.loads(added.stdout)["feed_id"]
    listed = RUNNER.invoke(app, ["feed-list", "-c", str(config)])
    assert json.loads(listed.stdout)["feeds"][0]["id"] == feed_id
    disabled = RUNNER.invoke(app, ["feed-disable", "--feed-id", str(feed_id), "-c", str(config)])
    assert json.loads(disabled.stdout)["enabled"] is False
    audit = RUNNER.invoke(app, ["audit-list", "-c", str(config)])
    audit_text = audit.stdout
    assert "news.example" in audit_text
    assert "probe-token" not in audit_text
    assert "token=" not in audit_text
    assert len(json.loads(audit_text)["events"]) == 2


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
