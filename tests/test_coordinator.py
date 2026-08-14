from __future__ import annotations

from pathlib import Path

import pytest

from rss_zen.coordinator import DeadlineCoordinator
from rss_zen.db import ArticleInput, Database, FeedInput, TranslationInput
from rss_zen.models import TopicConfig


def _topic() -> TopicConfig:
    return TopicConfig.model_validate(
        {
            "key": "indo-pacific",
            "version": 1,
            "name": "印太安全",
            "timezone": "Asia/Shanghai",
            "delivery_deadline": "07:30",
            "preparation_minutes": 60,
            "selection": {"keywords": ["Taiwan"]},
            "safety_limits": {"max_candidates": 10, "max_rendered_bytes": 100000},
        }
    )


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/rss"))
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
    return database


def _coordinator(database: Database, tmp_path: Path) -> DeadlineCoordinator:
    return DeadlineCoordinator(
        database,
        target_language="zh-CN",
        output_directory=tmp_path / "editions",
        target_ref="chat:oc_approved",
    )


def test_coordinator_skips_before_window_and_dry_run_is_mutation_free(tmp_path: Path) -> None:
    database = _database(tmp_path)
    coordinator = _coordinator(database, tmp_path)

    before = coordinator.run([_topic()], now="2026-08-13T22:29:59+00:00", dry_run=True)
    assert before[0].action == "not_due"
    assert database.latest_topic_profile("indo-pacific") is None

    due = coordinator.run([_topic()], now="2026-08-13T22:30:00+00:00", dry_run=True)
    assert due[0].action == "would_build"
    assert due[0].deadline_at == "2026-08-13T23:30:00+00:00"
    assert due[0].article_count == 1
    assert database.latest_topic_profile("indo-pacific") is None
    assert database.edition_delivery_health().edition_queued == 0
    assert not (tmp_path / "editions").exists()


def test_coordinator_rejects_changed_or_regressed_topic_versions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    coordinator = _coordinator(database, tmp_path)
    coordinator.run([_topic()], now="2026-08-13T22:30:00+00:00", dry_run=False)

    changed = _topic().model_copy(update={"name": "Changed without version bump"})
    with pytest.raises(ValueError, match="reuses a version"):
        coordinator.run([changed], now="2026-08-13T22:30:00+00:00", dry_run=True)

    version_two = _topic().model_copy(update={"version": 2, "name": "Version two"})
    coordinator.run([version_two], now="2026-08-14T22:30:00+00:00", dry_run=False)
    with pytest.raises(ValueError, match="go backwards"):
        coordinator.run([_topic()], now="2026-08-15T22:30:00+00:00", dry_run=False)


def test_coordinator_builds_once_and_catches_up_after_deadline(tmp_path: Path) -> None:
    database = _database(tmp_path)
    coordinator = _coordinator(database, tmp_path)

    first = coordinator.run([_topic()], now="2026-08-13T22:30:00+00:00", dry_run=False)
    repeated = coordinator.run([_topic()], now="2026-08-13T23:00:00+00:00", dry_run=False)

    assert first[0].action == "built"
    assert first[0].late is False
    assert repeated[0].edition_run_id == first[0].edition_run_id
    assert repeated[0].delivery_id == first[0].delivery_id
    health = database.edition_delivery_health()
    assert health.edition_queued == 1
    assert health.delivery_pending == 1

    after_deadline = coordinator.run(
        [_topic()], now="2026-08-14T00:00:00+00:00", dry_run=False
    )
    assert after_deadline[0].late is True
    assert after_deadline[0].local_date == "2026-08-14"
    assert after_deadline[0].edition_run_id == first[0].edition_run_id

    next_day = coordinator.run([_topic()], now="2026-08-14T22:30:00+00:00", dry_run=False)
    assert next_day[0].late is False
    assert next_day[0].local_date == "2026-08-15"
    assert next_day[0].edition_run_id != first[0].edition_run_id
    assert database.edition_delivery_health().edition_queued == 2
