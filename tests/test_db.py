from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import rss_zen.db as db_module
from rss_zen.db import (
    ArticleInput,
    Database,
    ExtractionInput,
    FeedInput,
    TopicProfileInput,
    TranslationInput,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "rss-zen.sqlite3")
    instance.initialize()
    return instance


def _feed() -> FeedInput:
    return FeedInput(
        name="Example feed",
        url="https://example.test/feed.xml",
        categories=("technology",),
        language="en",
        poll_interval_minutes=15,
    )


def _article(
    *,
    content: str = "Original body",
    guid: str | None = "article-1",
    canonical_url: str = "https://example.test/articles/one",
    published_at: str = "2026-08-11T10:00:00+00:00",
) -> ArticleInput:
    return ArticleInput(
        guid=guid,
        canonical_url=canonical_url,
        title="O'Reilly article",
        summary="Original summary",
        content=content,
        author="Author",
        categories=("technology",),
        published_at=published_at,
    )


def test_initialization_creates_current_schema(database: Database) -> None:
    assert database.schema_version() == 7
    assert database.table_names() >= {
        "feeds",
        "articles",
        "translations",
        "extractions",
        "export_runs",
        "sync_runs",
        "topic_profiles",
        "edition_runs",
        "edition_run_items",
        "delivery_outbox",
    }


def test_migration_snapshot_includes_uncheckpointed_wal_data(tmp_path: Path) -> None:
    """Pre-migration snapshots must include committed records still held in WAL."""
    database_path = tmp_path / "rss-zen.sqlite3"
    writer = sqlite3.connect(database_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.executescript(db_module._MIGRATION_1)
        writer.executescript(db_module._MIGRATION_2)
        writer.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        writer.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, "2026-08-11T00:00:00+00:00"), (2, "2026-08-11T00:00:01+00:00")],
        )
        writer.commit()
        writer.execute(
            """
            INSERT INTO feeds(name, url, categories_json, enabled, origin, created_at, updated_at)
            VALUES (?, ?, '[]', 1, 'config', ?, ?)
            """,
            (
                "Stored in WAL",
                "https://example.test/wal.xml",
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
            ),
        )
        writer.commit()

        Database(database_path).initialize()
    finally:
        writer.close()

    snapshots = list((tmp_path / "backups" / "pre-migration").glob("*.sqlite3"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("SELECT name FROM feeds").fetchone()[0] == "Stored in WAL"
    assert Database(database_path).schema_version() == 7


def test_current_migrations_snapshot_schema_4_before_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "rss-zen.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(db_module._MIGRATION_1)
        connection.executescript(db_module._MIGRATION_2)
        connection.executescript(db_module._MIGRATION_3)
        connection.executescript(db_module._MIGRATION_4)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, f"2026-08-14T00:00:0{version}+00:00") for version in range(1, 5)],
        )

    Database(database_path).initialize()

    snapshots = list((tmp_path / "backups" / "pre-migration").glob("rss-zen-v4-to-v7-*.sqlite3"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        version = snapshot.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert version == 4
    assert "delivery_outbox" not in tables
    assert Database(database_path).schema_version() == 7


def test_schema_5_upgrades_to_schema_6_without_redefining_migration_5(tmp_path: Path) -> None:
    database_path = tmp_path / "rss-zen.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for migration in (
            db_module._MIGRATION_1,
            db_module._MIGRATION_2,
            db_module._MIGRATION_3,
            db_module._MIGRATION_4,
            db_module._MIGRATION_5,
        ):
            connection.executescript(migration)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, f"2026-08-14T00:00:0{version}+00:00") for version in range(1, 6)],
        )

    Database(database_path).initialize()

    assert Database(database_path).schema_version() == 7
    assert "edition_run_items" in Database(database_path).table_names()
    snapshots = list(
        (tmp_path / "backups" / "pre-migration").glob("rss-zen-v5-to-v7-*.sqlite3")
    )
    assert len(snapshots) == 1


def test_schema_4_backup_restore_preserves_resumable_checkpoint(tmp_path: Path) -> None:
    """A verified post-migration backup must retain checkpoint resume state."""
    from rss_zen.backup import backup_database

    database_path = tmp_path / "rss-zen.sqlite3"
    database = Database(database_path)
    database.initialize()
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two"),
    ).article
    run = database.create_batch_run(
        command="translate",
        article_ids=(first.id, second.id),
        selector={"article_ids": [first.id, second.id]},
        limits={"provider_requests": 2},
    )
    database.complete_batch_run_item(run.id, first.id, status="succeeded")
    database.complete_batch_run_item(
        run.id, second.id, status="skipped", error_code="provider_budget_exhausted"
    )
    database.update_batch_run_status(run.id, status="interrupted")

    backup = backup_database(database_path, tmp_path / "backups")
    restored_path = tmp_path / "restored.sqlite3"
    restored_path.write_bytes(backup.read_bytes())
    restored = Database(restored_path)
    restored.initialize()

    assert restored.schema_version() == 7
    assert restored.get_batch_run(run.id).status == "interrupted"
    assert restored.batch_run_resumable_article_ids(run.id) == (second.id,)


def _topic() -> TopicProfileInput:
    return TopicProfileInput(
        key="indo-pacific",
        version=1,
        name="Indo-Pacific",
        timezone="Asia/Shanghai",
        delivery_deadline="07:30",
        lookback_hours=24,
        selection={"keywords": ["Taiwan Strait", "AUKUS"]},
        safety_limits={"max_candidates": 100, "max_agent_chars": 200_000},
    )


def test_topic_profile_versions_are_idempotent_and_reject_sensitive_metadata(
    database: Database,
) -> None:
    created = database.create_topic_profile(_topic())
    repeated = database.create_topic_profile(_topic())

    assert repeated == created
    assert database.latest_topic_profile("indo-pacific") == created

    conflicting = TopicProfileInput(
        **{**_topic().__dict__, "name": "Changed without a new version"}
    )
    with pytest.raises(ValueError, match="different topic profile"):
        database.create_topic_profile(conflicting)

    unsafe = TopicProfileInput(
        **{**_topic().__dict__, "version": 2, "selection": {"api_key": "must-not-persist"}}
    )
    with pytest.raises(ValueError, match="sensitive key"):
        database.create_topic_profile(unsafe)

    article_body = TopicProfileInput(
        **{**_topic().__dict__, "version": 2, "selection": {"content": "article body"}}
    )
    with pytest.raises(ValueError, match="sensitive key"):
        database.create_topic_profile(article_body)

    oversized = TopicProfileInput(
        **{**_topic().__dict__, "version": 2, "selection": {"keywords": ["x" * 4097]}}
    )
    with pytest.raises(ValueError, match="metadata limit"):
        database.create_topic_profile(oversized)


def test_edition_identity_and_state_transitions_are_validated(database: Database) -> None:
    topic = database.create_topic_profile(_topic())
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    repeated = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    assert repeated == edition

    refreshing = database.transition_edition_run(edition.id, status="refreshing")
    selecting = database.transition_edition_run(
        edition.id, status="selecting", candidate_count=12, translated_count=9
    )
    frozen = database.transition_edition_run(edition.id, status="frozen")
    rendered = database.transition_edition_run(
        edition.id,
        status="rendered",
        artifact_path=Path("editions/2026-08-14-indo-pacific.md"),
        artifact_sha256="a" * 64,
    )

    assert refreshing.status == "refreshing"
    assert selecting.candidate_count == 12
    assert selecting.translated_count == 9
    assert frozen.status == "frozen"
    assert rendered.artifact_path == Path("editions/2026-08-14-indo-pacific.md")

    with pytest.raises(ValueError, match="invalid edition transition"):
        database.transition_edition_run(edition.id, status="refreshing")

    with pytest.raises(ValueError, match="different deadline"):
        database.create_edition_run(
            topic_profile_id=topic.id,
            local_date="2026-08-14",
            deadline_at="2026-08-14T00:00:00+00:00",
        )


def test_delivery_outbox_is_idempotent_and_claims_due_work(database: Database) -> None:
    topic = database.create_topic_profile(_topic())
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    for status in ("refreshing", "selecting", "frozen"):
        edition = database.transition_edition_run(edition.id, status=status)

    with pytest.raises(ValueError, match="rendered or degraded"):
        database.create_delivery_outbox_item(
            edition_run_id=edition.id,
            channel="feishu",
            target_ref="chat:approved-digest",
            idempotency_key="2026-08-14:indo-pacific:premature",
            artifact_path=Path("editions/daily.md"),
            payload_sha256="b" * 64,
        )

    edition = database.transition_edition_run(
        edition.id,
        status="rendered",
        artifact_path=Path("editions/daily.md"),
        artifact_sha256="b" * 64,
    )
    delivery = database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:approved-digest",
        idempotency_key="2026-08-14:indo-pacific:feishu",
        artifact_path=Path("editions/daily.md"),
        payload_sha256="b" * 64,
    )
    repeated = database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:approved-digest",
        idempotency_key="2026-08-14:indo-pacific:feishu",
        artifact_path=Path("editions/daily.md"),
        payload_sha256="b" * 64,
    )
    assert repeated == delivery
    with pytest.raises(ValueError, match="different delivery"):
        database.create_delivery_outbox_item(
            edition_run_id=edition.id,
            channel="feishu",
            target_ref="chat:different-target",
            idempotency_key="2026-08-14:indo-pacific:feishu",
            artifact_path=Path("editions/daily.md"),
            payload_sha256="b" * 64,
        )
    assert database.get_edition_run(edition.id).status == "queued"

    claimed = database.claim_due_deliveries(
        worker_id="delivery-1",
        now="2026-08-14T00:00:00+00:00",
        lease_expires_at="2026-08-14T00:05:00+00:00",
        limit=10,
    )
    assert [item.id for item in claimed] == [delivery.id]
    assert claimed[0].status == "sending"
    assert claimed[0].attempt_count == 1
    assert database.get_edition_run(edition.id).status == "delivering"

    with pytest.raises(ValueError, match="current worker lease"):
        database.record_delivery_retry(
            delivery.id,
            worker_id="wrong-worker",
            error_code="feishu_rate_limited",
            next_attempt_at="2026-08-14T00:10:00+00:00",
        )

    retrying = database.record_delivery_retry(
        delivery.id,
        worker_id="delivery-1",
        error_code="feishu_rate_limited",
        next_attempt_at="2026-08-14T00:10:00+00:00",
    )
    assert retrying.status == "retry_wait"
    assert database.get_edition_run(edition.id).status == "queued"
    assert database.claim_due_deliveries(
        worker_id="delivery-2",
        now="2026-08-14T00:09:59+00:00",
        lease_expires_at="2026-08-14T00:15:00+00:00",
        limit=10,
    ) == []

    reclaimed = database.claim_due_deliveries(
        worker_id="delivery-2",
        now="2026-08-14T00:10:00+00:00",
        lease_expires_at="2026-08-14T00:15:00+00:00",
        limit=10,
    )
    assert reclaimed[0].attempt_count == 2
    delivered = database.record_delivery_success(
        delivery.id, worker_id="delivery-2", provider_message_id="om_123"
    )
    assert delivered.status == "delivered"
    assert delivered.provider_message_id == "om_123"
    assert database.get_edition_run(edition.id).status == "delivered"
    assert database.claim_due_deliveries(
        worker_id="delivery-3",
        now="2026-08-14T00:20:00+00:00",
        lease_expires_at="2026-08-14T00:25:00+00:00",
        limit=10,
    ) == []


def test_delivery_expired_lease_is_recovered_and_can_be_terminal(database: Database) -> None:
    topic = database.create_topic_profile(_topic())
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-15",
        deadline_at="2026-08-14T23:30:00+00:00",
    )
    for status in ("refreshing", "selecting", "frozen", "degraded"):
        edition = database.transition_edition_run(
            edition.id,
            status=status,
            degraded_reason_code="agent_unavailable" if status == "degraded" else None,
            artifact_path=Path("editions/fallback.md") if status == "degraded" else None,
            artifact_sha256="d" * 64 if status == "degraded" else None,
        )
    delivery = database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:approved-digest",
        idempotency_key="2026-08-15:indo-pacific:feishu",
        artifact_path=Path("editions/fallback.md"),
        payload_sha256="d" * 64,
    )

    first = database.claim_due_deliveries(
        worker_id="crashed-worker",
        now="2026-08-15T00:00:00+00:00",
        lease_expires_at="2026-08-15T00:05:00+00:00",
        limit=1,
    )
    assert first[0].attempt_count == 1
    assert database.claim_due_deliveries(
        worker_id="recovery-worker",
        now="2026-08-15T00:04:59+00:00",
        lease_expires_at="2026-08-15T00:09:59+00:00",
        limit=1,
    ) == []

    recovered = database.claim_due_deliveries(
        worker_id="recovery-worker",
        now="2026-08-15T00:05:00+00:00",
        lease_expires_at="2026-08-15T00:10:00+00:00",
        limit=1,
    )
    assert recovered[0].id == delivery.id
    assert recovered[0].attempt_count == 2

    terminal = database.record_delivery_terminal(
        delivery.id,
        worker_id="recovery-worker",
        error_code="feishu_target_invalid",
    )
    assert terminal.status == "terminal"
    assert terminal.error_code == "feishu_target_invalid"
    assert database.get_edition_run(edition.id).status == "terminal"


def test_schema_7_backup_restore_preserves_pending_delivery(tmp_path: Path) -> None:
    from rss_zen.backup import backup_database

    database_path = tmp_path / "rss-zen.sqlite3"
    database = Database(database_path)
    database.initialize()
    topic = database.create_topic_profile(_topic())
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    for status in ("refreshing", "selecting", "frozen", "degraded"):
        edition = database.transition_edition_run(
            edition.id,
            status=status,
            degraded_reason_code="agent_unavailable" if status == "degraded" else None,
            artifact_path=Path("editions/fallback.md") if status == "degraded" else None,
            artifact_sha256="c" * 64 if status == "degraded" else None,
        )
    delivery = database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:approved-digest",
        idempotency_key="2026-08-14:indo-pacific:feishu",
        artifact_path=Path("editions/fallback.md"),
        payload_sha256="c" * 64,
    )

    backup = backup_database(database_path, tmp_path / "backups")
    restored_path = tmp_path / "restored.sqlite3"
    restored_path.write_bytes(backup.read_bytes())
    restored = Database(restored_path)
    restored.initialize()

    assert restored.schema_version() == 7
    assert restored.get_edition_run(edition.id).status == "queued"
    assert restored.get_edition_run(edition.id).degraded_reason_code == "agent_unavailable"
    assert restored.get_delivery_outbox_item(delivery.id).status == "pending"


def test_retention_preview_counts_only_configured_candidates(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article
    with database._connection() as connection:
        connection.execute(
            "UPDATE articles SET last_seen_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", article.id),
        )

    counts = database.retention_counts(articles_before="2001-01-01T00:00:00+00:00")

    assert counts.articles == 1
    assert counts.failed_extractions == 0
    assert database.retention_counts().articles == 0


def test_retention_apply_deletes_only_completed_batch_runs(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article
    completed = database.create_batch_run(
        command="translate", article_ids=(article.id,), selector={}, limits={}
    )
    interrupted = database.create_batch_run(
        command="extract", article_ids=(article.id,), selector={}, limits={}
    )
    database.update_batch_run_status(completed.id, status="succeeded")
    database.update_batch_run_status(interrupted.id, status="interrupted")
    with database._connection() as connection:
        connection.execute(
            "UPDATE batch_runs SET completed_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", completed.id),
        )

    counts = database.apply_retention(batch_runs_before="2001-01-01T00:00:00+00:00")

    assert counts.batch_runs == 1
    with pytest.raises(KeyError):
        database.get_batch_run(completed.id)
    assert database.get_batch_run(interrupted.id).status == "interrupted"


def test_batch_run_materializes_ordered_article_selection(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two"),
    ).article

    run = database.create_batch_run(
        command="translate",
        article_ids=(second.id, first.id),
        selector={"article_ids": [second.id, first.id]},
        limits={"provider_requests": 2},
    )

    assert run.command == "translate"
    assert database.batch_run_pending_article_ids(run.id) == (second.id, first.id)


def test_batch_run_tracks_item_and_run_lifecycle(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two"),
    ).article
    run = database.create_batch_run(
        command="translate",
        article_ids=(first.id, second.id),
        selector={"source": "Example feed"},
        limits={"provider_requests": 2},
    )

    database.complete_batch_run_item(run.id, first.id, status="succeeded")
    database.complete_batch_run_item(
        run.id, second.id, status="skipped", error_code="provider_budget_exhausted"
    )
    database.update_batch_run_status(run.id, status="interrupted")

    loaded = database.get_batch_run(run.id)
    assert loaded.status == "interrupted"
    assert loaded.selector == {"source": "Example feed"}
    assert database.batch_run_resumable_article_ids(run.id) == (second.id,)


def test_batch_run_rejects_unknown_id_and_wrong_command(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article
    run = database.create_batch_run(
        command="extract",
        article_ids=(article.id,),
        selector={},
        limits={},
    )

    with pytest.raises(KeyError):
        database.get_batch_run(999)
    with pytest.raises(ValueError, match="command"):
        database.require_batch_run_command(run.id, "translate")


def test_upsert_feed_updates_existing_record(database: Database) -> None:
    original = database.upsert_feed(_feed())
    updated = database.upsert_feed(
        FeedInput(
            name="Renamed feed",
            url="https://example.test/feed.xml",
            categories=("technology", "python"),
            language="en",
            poll_interval_minutes=30,
        )
    )

    assert updated.id == original.id
    assert updated.name == "Renamed feed"
    assert updated.categories == ("technology", "python")
    assert updated.poll_interval_minutes == 30


def test_reconcile_article_by_guid_or_link_updates_changed_content(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article())
    changed = database.reconcile_article(feed.id, _article(content="Changed body"))

    assert first.created is True
    assert first.content_changed is True
    assert changed.created is False
    assert changed.content_changed is True
    assert changed.article.id == first.article.id
    assert changed.article.content == "Changed body"

    same_link_without_guid = database.reconcile_article(
        feed.id,
        _article(content="Changed again", guid=None),
    )
    assert same_link_without_guid.article.id == first.article.id
    assert same_link_without_guid.content_changed is True


def test_list_articles_overview_filters_by_source_and_status(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    reconciled = database.reconcile_article(feed.id, _article())
    article = reconciled.article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="中文标题",
            summary=None,
            content=None,
            provider_name="google",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )

    overviews = database.list_articles_overview(target_language="zh-CN")
    assert len(overviews) == 1
    row = overviews[0]
    assert row.feed_name == "Example feed"
    assert row.translation_status == "succeeded"
    assert row.translation_provider == "google"
    assert row.extraction_status is None

    # published_after excludes an earlier article timestamp.
    late = database.reconcile_article(
        feed.id,
        _article(guid="article-2", canonical_url="https://example.test/articles/two",
                 published_at="2026-08-12T10:00:00+00:00"),
    )
    overviews = database.list_articles_overview(
        target_language="zh-CN", published_after="2026-08-12T00:00:00+00:00"
    )
    assert [row.article.id for row in overviews] == [late.article.id]


def test_processing_results_and_export_run_are_persisted(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article

    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title="中文标题",
            summary="中文摘要",
            content="中文正文",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash=article.content_hash,
        )
    )
    extraction = database.record_extraction(
        ExtractionInput(
            article_id=article.id,
            provider_name="anysearch",
            source_url=article.canonical_url,
            content="Extracted content",
            status="succeeded",
            request_id="request-1",
        )
    )
    export_run = database.record_export_run(
        profile_name="daily",
        output_path=Path("exports/daily.md"),
        filters={"source": "Example feed"},
        article_count=1,
        status="succeeded",
    )

    translation = database.latest_translation(article.id, "zh-CN")
    assert translation is not None
    assert translation.title == "中文标题"
    assert translation.provider_name == "free"
    assert extraction.content == "Extracted content"
    assert export_run.article_count == 1


def test_list_articles_by_translation_status_returns_distinct_articles(
    database: Database,
) -> None:
    """Status selection returns one row per article, latest translation wins."""
    feed = database.upsert_feed(_feed())
    first = database.reconcile_article(feed.id, _article()).article
    second = database.reconcile_article(
        feed.id, _article(guid="article-2", canonical_url="https://example.test/two")
    ).article
    database.save_translation(
        TranslationInput(
            article_id=first.id,
            target_language="zh-CN",
            title=None,
            summary=None,
            content=None,
            provider_name="free",
            provider_model=None,
            status="failed",
            source_hash="hash-1",
            error_code="translation_provider_error",
            error_message="boom",
            attempt_count=2,
            terminal=True,
        )
    )
    database.save_translation(
        TranslationInput(
            article_id=second.id,
            target_language="zh-CN",
            title="标题",
            summary="",
            content="内容",
            provider_name="free",
            provider_model=None,
            status="succeeded",
            source_hash="hash-2",
        )
    )

    failed = database.list_articles_by_translation_status("zh-CN", status="failed")
    succeeded = database.list_articles_by_translation_status("zh-CN", status="succeeded")

    assert [article.id for article in failed] == [first.id]
    assert [article.id for article in succeeded] == [second.id]


def test_parameterized_sql_preserves_quote_characters(database: Database) -> None:
    feed = database.upsert_feed(_feed())
    article = database.reconcile_article(feed.id, _article()).article

    assert database.get_article(article.id).title == "O'Reilly article"
