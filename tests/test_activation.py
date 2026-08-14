from __future__ import annotations

import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from rss_zen.cli import app
from rss_zen.db import (
    ArticleInput,
    Database,
    FeedInput,
    TopicProfileInput,
    TranslationInput,
)

runner = CliRunner()


def _config(tmp_path: Path, *, enabled: bool, with_secrets: bool = False) -> Path:
    config = tmp_path / "rss-zen.toml"
    config.write_text(
        f"""
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"

[feishu]
enabled = {str(enabled).lower()}
app_id_env = "FEISHU_APP_ID"
app_secret_env = "FEISHU_APP_SECRET"
{"target_ref = \"chat:oc_approved\"" if enabled else ""}

[[topics]]
key = "indo-pacific"
version = 1
name = "印太安全"
timezone = "Asia/Shanghai"
delivery_deadline = "07:30"
lookback_hours = 24
preparation_minutes = 60
enabled = true

[topics.selection]
keywords = ["Taiwan"]
include_untranslated = false

[topics.safety_limits]
max_candidates = 10
max_rendered_bytes = 100000
""",
        encoding="utf-8",
    )
    return config


def _seed(database: Database) -> None:
    topic = database.create_topic_profile(
        TopicProfileInput(
            key="indo-pacific",
            version=1,
            name="印太安全",
            timezone="Asia/Shanghai",
            delivery_deadline="07:30",
            lookback_hours=24,
            selection={
                "keywords": ["Taiwan"],
                "content_keywords": [],
                "keyword_match": "any",
                "sources": [],
                "categories": [],
                "feed_priority": [],
                "dedupe_by_title": True,
                "include_untranslated": False,
            },
            safety_limits={
                "max_candidates": 10,
                "max_rendered_bytes": 100_000,
                "preparation_minutes": 60,
            },
        )
    )
    assert topic.key == "indo-pacific"
    feed = database.upsert_feed(
        FeedInput(name="Example", url="https://example.test/rss", language="en")
    )
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1",
            canonical_url="https://example.test/a1",
            title="Taiwan update",
            summary="Summary",
            content=None,
            author=None,
            categories=(),
            published_at="2026-08-13T22:00:00+00:00",
        ),
    ).article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="台湾动态",
            summary="中文摘要",
            content=None,
            provider_name="test",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )


def test_edition_dry_run_is_mutation_and_file_free(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _seed(database)
    before = database.edition_delivery_health()

    result = runner.invoke(
        app,
        [
            "edition-build",
            "--topic",
            "indo-pacific",
            "--local-date",
            "2026-08-14",
            "--dry-run",
            "--json",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["article_count"] == 1
    assert payload["deadline_at"] == "2026-08-13T23:30:00+00:00"
    assert payload["content_sources"] == ["rss_summary"]
    assert database.edition_delivery_health() == before
    assert not (tmp_path / "editions").exists()


def test_actual_build_refuses_disabled_delivery_before_creating_an_edition(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _seed(database)

    result = runner.invoke(
        app,
        [
            "edition-build",
            "--topic",
            "indo-pacific",
            "--local-date",
            "2026-08-14",
            "--json",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 1
    assert "feishu_delivery_disabled" in result.stderr
    assert database.edition_delivery_health().edition_active == 0


def test_build_enqueues_but_delivery_dry_run_and_missing_credentials_do_not_claim(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, enabled=True)
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _seed(database)

    build = runner.invoke(
        app,
        [
            "edition-build",
            "--topic",
            "indo-pacific",
            "--local-date",
            "2026-08-14",
            "--deadline-at",
            "2026-08-13T23:30:00+00:00",
            "--json",
            "--config",
            str(config),
        ],
    )
    assert build.exit_code == 0
    assert json.loads(build.stdout)["delivery_status"] == "pending"

    preview = runner.invoke(
        app, ["delivery-run", "--dry-run", "--json", "--config", str(config)]
    )
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["pending"] == 1
    health = database.edition_delivery_health()
    assert health.delivery_pending == 1
    assert health.delivery_sending == 0

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    refused = runner.invoke(app, ["delivery-run", "--json", "--config", str(config)])
    assert refused.exit_code == 1
    assert "feishu_credentials_missing" in refused.stderr
    assert database.edition_delivery_health().delivery_pending == 1


def test_deadline_run_dry_run_and_actual_are_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _seed(database)

    preview = runner.invoke(
        app,
        [
            "deadline-run",
            "--dry-run",
            "--now",
            "2026-08-13T22:30:00+00:00",
            "--json",
            "--config",
            str(config),
        ],
    )
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["topics"][0]["action"] == "would_build"
    assert database.edition_delivery_health().edition_queued == 0

    first = runner.invoke(
        app,
        [
            "deadline-run",
            "--now",
            "2026-08-13T22:30:00+00:00",
            "--json",
            "--config",
            str(config),
        ],
    )
    repeated = runner.invoke(
        app,
        [
            "deadline-run",
            "--now",
            "2026-08-13T23:00:00+00:00",
            "--json",
            "--config",
            str(config),
        ],
    )
    assert first.exit_code == 0
    assert repeated.exit_code == 0
    first_topic = json.loads(first.stdout)["topics"][0]
    repeated_topic = json.loads(repeated.stdout)["topics"][0]
    assert first_topic["edition_run_id"] == repeated_topic["edition_run_id"]
    assert database.edition_delivery_health().edition_queued == 1


def test_delivery_run_uses_mock_transport_and_persists_success(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path, enabled=True)
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    _seed(database)
    build = runner.invoke(
        app,
        [
            "edition-build",
            "--topic",
            "indo-pacific",
            "--local-date",
            "2026-08-14",
            "--deadline-at",
            "2026-08-13T23:30:00+00:00",
            "--config",
            str(config),
        ],
    )
    assert build.exit_code == 0
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "private-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 60})
        if request.url.path.endswith("/im/v1/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "f"}})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_success"}})

    real_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rss_zen.cli.httpx.Client", mock_client)
    result = runner.invoke(app, ["delivery-run", "--json", "--config", str(config)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["delivered"] == 1
    assert database.edition_delivery_health().delivery_delivered == 1
