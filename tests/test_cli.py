from pathlib import Path

from typer.testing import CliRunner

from rss_zen.cli import app

runner = CliRunner()


def test_help_lists_primary_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "serve",
        "sync",
        "translate",
        "import-opml",
        "extract",
        "export",
        "status",
    ):
        assert command in result.stdout


def test_import_opml_command_initializes_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    monkeypatch.setenv("AI_TRANSLATION_API_KEY", "ai-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[translation.providers]]
name = "ai"
kind = "openai_compatible"
endpoint = "https://ai.example.test/v1"
api_key_env = "AI_TRANSLATION_API_KEY"
model = "translation-model"
""",
        encoding="utf-8",
    )
    opml_path = tmp_path / "feeds.opml"
    opml_path.write_text(
        """
<opml version="2.0"><body>
  <outline text="Example" type="rss" xmlUrl="https://example.test/feed.xml" />
</body></opml>
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import-opml", str(opml_path), "--config", str(config_path)])

    assert result.exit_code == 0
    assert "imported=1" in result.stdout
    assert (tmp_path / "rss-zen.sqlite3").is_file()

    status = runner.invoke(app, ["status", "--config", str(config_path)])

    assert status.exit_code == 0
    assert "articles=0" in status.stdout
    assert "url=https://example.test/feed.xml" in status.stdout


def test_status_json_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["counts"]["articles"] == 0
    assert payload["feeds"] == []
    assert payload["errors"] == []


def test_status_json_exposes_health_contract_v1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[backup]
directory = "backups"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()

    result = runner.invoke(app, ["status", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["generated_at"]
    assert payload["database"]["schema_version"] == 6
    assert payload["database"]["size_bytes"] > 0
    assert payload["database"]["wal_size_bytes"] >= 0
    assert payload["disk"]["free_bytes"] > 0
    assert payload["feed_health"] == {
        "total": 0,
        "enabled": 0,
        "never_succeeded": 0,
        "stale": 0,
    }
    assert payload["batches"] == {"running": 0, "interrupted": 0, "resumable_items": 0}
    assert payload["editions"] == {
        "active": 0,
        "queued": 0,
        "delivered": 0,
        "terminal": 0,
        "degraded": 0,
    }
    assert payload["delivery"] == {
        "enabled": False,
        "budget_mode": "observe",
        "pending": 0,
        "sending": 0,
        "retry_wait": 0,
        "delivered": 0,
        "terminal": 0,
        "latest_delivered_at": None,
    }
    assert payload["backups"]["newest"] is None


def test_status_json_marks_overdue_and_malformed_timestamps_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[service]
default_poll_interval_minutes = 30

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[feeds]]
name = "Overdue"
url = "https://overdue.example.test/rss"
poll_interval_minutes = 10

[[feeds]]
name = "Malformed"
url = "https://malformed.example.test/rss"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    overdue = database.upsert_feed(
        FeedInput(
            name="Overdue", url="https://overdue.example.test/rss", poll_interval_minutes=10
        )
    )
    malformed = database.upsert_feed(FeedInput(name="Malformed", url="https://malformed.example.test/rss"))
    database.record_feed_success(overdue.id, etag=None, last_modified=None)
    database.record_feed_success(malformed.id, etag=None, last_modified=None)
    with database._connection() as connection:
        connection.execute(
            "UPDATE feeds SET last_success_at = ? WHERE id = ?",
            ("not-a-timestamp", malformed.id),
        )
        connection.execute(
            "UPDATE feeds SET last_success_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", overdue.id),
        )

    result = runner.invoke(app, ["status", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["feed_health"]["stale"] == 2


def test_retention_dry_run_requires_explicit_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database

    Database(tmp_path / "rss-zen.sqlite3").initialize()
    result = runner.invoke(app, ["retention", "--dry-run", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "retention_not_configured" in result.stderr


def test_retention_apply_creates_backup_before_deleting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[retention]
articles_days = 1

[backup]
directory = "backups"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="old", canonical_url="https://example.test/old", title="Old",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    with database._connection() as connection:
        connection.execute(
            "UPDATE articles SET last_seen_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", article.id),
        )

    result = runner.invoke(app, ["retention", "apply", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["articles"] == 1
    assert Path(payload["backup"]).is_file()
    assert database.retention_counts(articles_before="2100-01-01T00:00:00+00:00").articles == 0


def test_retention_apply_does_not_delete_when_backup_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[retention]
articles_days = 1

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput
    from rss_zen.errors import AppError

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="old", canonical_url="https://example.test/old", title="Old",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    with database._connection() as connection:
        connection.execute(
            "UPDATE articles SET last_seen_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", article.id),
        )

    monkeypatch.setattr(
        "rss_zen.cli.backup_database",
        lambda *args, **kwargs: (_ for _ in ()).throw(AppError("backup_failed", "backup failed")),
    )
    result = runner.invoke(app, ["retention", "apply", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "backup_failed" in result.stderr
    assert database.retention_counts(articles_before="2100-01-01T00:00:00+00:00").articles == 1


def test_maintenance_checkpoint_reports_local_wal_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database

    Database(tmp_path / "rss-zen.sqlite3").initialize()
    result = runner.invoke(app, ["maintenance", "checkpoint", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["action"] == "checkpoint"
    assert set(payload) == {"action", "busy", "log_frames", "checkpointed_frames"}


def test_list_command_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[feeds]]
name = "Example"
url = "https://example.test/feed.xml"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["list", "--config", str(config_path)])

    assert result.exit_code == 0
    # No articles yet, so output is empty but the command succeeds.
    assert result.stdout == ""


def test_export_without_profile_lists_available_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[exports]]
name = "daily"
output_path = "exports/daily.md"
title = "Daily"
fields = ["title"]

[[exports]]
name = "weekly"
output_path = "exports/weekly.md"
title = "Weekly"
fields = ["title"]
""",
        encoding="utf-8",
    )

    text_result = runner.invoke(app, ["export", "--config", str(config_path)])
    json_result = runner.invoke(app, ["export", "--json", "--config", str(config_path)])

    assert text_result.exit_code == 0
    assert "daily:" in text_result.stdout
    assert "weekly:" in text_result.stdout
    assert json_result.exit_code == 0
    import json as _json

    profiles = _json.loads(json_result.stdout)
    assert [profile["name"] for profile in profiles] == ["daily", "weekly"]


def test_status_json_includes_last_sync_field(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert "last_sync" in payload
    assert payload["last_sync"]["latest_feed_success"] is None
    assert payload["last_sync"]["stale_feeds"] == 0


def test_doctor_reports_healthy_checks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    json_result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "ok configuration" in result.stdout
    assert "warn database" in result.stdout  # database not created yet
    assert "warn backup" in result.stdout
    import json as _json

    payload = _json.loads(json_result.stdout)
    assert payload["healthy"] is True


def test_doctor_uses_configured_backup_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[backup]
directory = "managed-backups"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.backup import backup_database
    from rss_zen.db import Database

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    backup_database(database.path, tmp_path / "managed-backups")

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "ok backup:" in result.stdout
    assert "no backups found" not in result.stdout


def test_doctor_json_exposes_health_contract_v1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[backup]
directory = "backups"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()

    result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["generated_at"]
    assert payload["warning_count"] >= 1  # no backup is a warning
    assert payload["error_count"] == 0
    assert payload["healthy"] is True
    checks = {check["check"]: check for check in payload["checks"]}
    assert checks["database"]["status"] == "ok"
    assert "database_storage" in checks
    assert "batches" in checks
    assert checks["feishu"]["status"] == "ok"
    assert checks["editions_delivery"]["status"] == "ok"


def test_status_and_doctor_report_pending_feishu_delivery_without_target_leak(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "private-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[feishu]
enabled = true
app_id_env = "FEISHU_APP_ID"
app_secret_env = "FEISHU_APP_SECRET"
target_ref = "chat:oc_private_target"
""",
        encoding="utf-8",
    )
    from pathlib import Path

    from rss_zen.db import Database, TopicProfileInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    topic = database.create_topic_profile(
        TopicProfileInput(
            key="test-topic",
            version=1,
            name="Test",
            timezone="Asia/Shanghai",
            delivery_deadline="07:30",
            lookback_hours=24,
            selection={},
            safety_limits={"max_candidates": 1, "max_rendered_bytes": 1000},
        )
    )
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    database.transition_edition_run(edition.id, status="refreshing")
    database.transition_edition_run(edition.id, status="selecting")
    database.freeze_edition_items(edition.id, ())
    artifact = tmp_path / "edition.md"
    artifact.write_text("# Empty\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    database.transition_edition_run(
        edition.id,
        status="rendered",
        artifact_path=Path("edition.md"),
        artifact_sha256=digest,
    )
    database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:oc_private_target",
        idempotency_key="test:2026-08-14:feishu",
        artifact_path=Path("edition.md"),
        payload_sha256=digest,
    )

    status_result = runner.invoke(app, ["status", "--json", "--config", str(config_path)])
    assert status_result.exit_code == 0
    import json as _json

    status_payload = _json.loads(status_result.stdout)
    assert status_payload["editions"]["queued"] == 1
    assert status_payload["delivery"]["enabled"] is True
    assert status_payload["delivery"]["pending"] == 1
    assert "oc_private_target" not in status_result.stdout
    assert "private-secret" not in status_result.stdout

    doctor_result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])
    assert doctor_result.exit_code == 0
    doctor_payload = _json.loads(doctor_result.stdout)
    checks = {check["check"]: check for check in doctor_payload["checks"]}
    assert checks["feishu"]["status"] == "ok"
    assert checks["editions_delivery"]["status"] == "warning"
    assert "oc_private_target" not in doctor_result.stdout
    assert "private-secret" not in doctor_result.stdout

    claimed = database.claim_due_deliveries(
        worker_id="test-worker",
        now="2026-08-14T00:00:00+00:00",
        lease_expires_at="2026-08-14T00:05:00+00:00",
        limit=1,
    )
    database.record_delivery_terminal(
        claimed[0].id,
        worker_id="test-worker",
        error_code="feishu_target_invalid",
    )
    terminal_result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])
    assert terminal_result.exit_code == 1
    terminal_payload = _json.loads(terminal_result.stdout)
    terminal_checks = {check["check"]: check for check in terminal_payload["checks"]}
    assert terminal_checks["editions_delivery"]["status"] == "error"
    assert "oc_private_target" not in terminal_result.stdout


def test_doctor_warns_when_enabled_feishu_secret_environment_is_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    monkeypatch.delenv("MISSING_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("MISSING_FEISHU_APP_SECRET", raising=False)
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[feishu]
enabled = true
app_id_env = "MISSING_FEISHU_APP_ID"
app_secret_env = "MISSING_FEISHU_APP_SECRET"
target_ref = "chat:oc_approved"
""",
        encoding="utf-8",
    )
    from rss_zen.db import Database

    Database(tmp_path / "rss-zen.sqlite3").initialize()
    result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    checks = {check["check"]: check for check in payload["checks"]}
    assert checks["feishu"]["status"] == "warning"
    assert "MISSING_FEISHU_APP_ID" in checks["feishu"]["detail"]
    assert "MISSING_FEISHU_APP_SECRET" in checks["feishu"]["detail"]


def test_doctor_checks_curl_only_when_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    monkeypatch.setattr("rss_zen.cli.shutil.which", lambda _name: None)
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[feeds]]
name = "Curl feed"
url = "https://example.test/rss"
fetcher = "curl"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])

    assert result.exit_code == 1
    import json as _json

    checks = {check["check"]: check for check in _json.loads(result.stdout)["checks"]}
    assert checks["curl"]["status"] == "error"


def test_doctor_never_creates_missing_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "missing.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--json", "--config", str(config_path)])

    assert result.exit_code == 0
    assert not (tmp_path / "missing.sqlite3").exists()
    assert not (tmp_path / "missing.sqlite3-wal").exists()


def test_doctor_reports_configuration_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "error configuration" in result.stdout
    assert "provider" in result.stdout


def test_translate_maps_report_write_failure_to_safe_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1",
            canonical_url="https://example.test/one",
            title="One",
            summary=None,
            content=None,
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "translate",
            "--article-id",
            str(article.id),
            "--dry-run",
            "--report-json",
            str(blocked / "report.json"),
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "report_write_failed" in result.stderr
    assert "Traceback" not in result.stdout


def test_translate_rejects_budget_override_above_configured_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[limits]
max_provider_requests_per_run = 2
max_source_chars_per_run = 20

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1",
            canonical_url="https://example.test/one",
            title="One",
            summary=None,
            content=None,
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article

    result = runner.invoke(
        app,
        [
            "translate",
            "--article-id",
            str(article.id),
            "--max-requests",
            "3",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "budget override" in result.stderr


def test_translate_writes_report_before_budget_exhaustion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[limits]
max_provider_requests_per_run = 1
max_source_chars_per_run = 100

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1",
            canonical_url="https://example.test/one",
            title="One",
            summary="Two",
            content=None,
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article
    second = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a2",
            canonical_url="https://example.test/two",
            title="Three",
            summary="Four",
            content=None,
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article

    class FakeService:
        called = False

        def translate_article(self, record, *, force=False):
            from rss_zen.errors import AppError
            from rss_zen.translation import TranslationOutcome

            if not self.called:
                self.called = True
                return TranslationOutcome(record.id, "succeeded", "free")
            raise AppError("provider_budget_exhausted", "provider request budget is exhausted")

    monkeypatch.setattr("rss_zen.cli.build_translation_service", lambda *a, **k: FakeService())
    report_path = tmp_path / "reports" / "budget.json"

    result = runner.invoke(
        app,
        [
            "translate",
            "--article-id",
            str(article.id),
            "--article-id",
            str(second.id),
            "--report-json",
            str(report_path),
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "provider_budget_exhausted" in result.stderr
    import json as _json

    report = _json.loads(report_path.read_text(encoding="utf-8"))
    assert report["completed_articles"] == 1
    assert report["skipped_articles"] == 1
    assert report["skipped"][0]["reason"] == "provider_budget_exhausted"
    assert report["skipped"][0]["article_id"] in {article.id, second.id}


def test_translate_rejects_unknown_or_wrong_command_checkpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1", canonical_url="https://example.test/one", title="One",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    extract_run = database.create_batch_run(
        command="extract", article_ids=(article.id,), selector={}, limits={}
    )

    unknown = runner.invoke(app, ["translate", "--resume", "999", "--config", str(config_path)])
    wrong_command = runner.invoke(
        app, ["translate", "--resume", str(extract_run.id), "--config", str(config_path)]
    )

    assert unknown.exit_code == 1
    assert "batch_run_not_found" in unknown.stderr
    assert "Traceback" not in unknown.stdout
    assert wrong_command.exit_code == 1
    assert "invalid_batch_resume" in wrong_command.stderr


def test_translate_creates_checkpoint_and_reports_run_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1", canonical_url="https://example.test/one", title="One",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article

    class FakeService:
        def translate_article(self, record, *, force=False):
            from rss_zen.translation import TranslationOutcome

            return TranslationOutcome(record.id, "succeeded", "free")

    monkeypatch.setattr("rss_zen.cli.build_translation_service", lambda *a, **k: FakeService())
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "translate", "--article-id", str(article.id), "--report-json", str(report_path),
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0
    import json as _json

    report = _json.loads(report_path.read_text(encoding="utf-8"))
    run_id = report["batch_run_id"]
    assert database.get_batch_run(run_id).status == "succeeded"
    assert database.batch_run_pending_article_ids(run_id) == ()

    without_checkpoint = runner.invoke(
        app,
        [
            "translate", "--article-id", str(article.id), "--no-checkpoint",
            "--report-json", str(tmp_path / "no-checkpoint.json"), "--config", str(config_path),
        ],
    )
    assert without_checkpoint.exit_code == 0
    no_checkpoint = _json.loads((tmp_path / "no-checkpoint.json").read_text(encoding="utf-8"))
    assert "batch_run_id" not in no_checkpoint


def test_extract_resume_only_processes_unfinished_checkpoint_items(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput
    from rss_zen.extraction import ExtractionOutcome

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    first = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="first", canonical_url="https://example.test/first", title="First",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    second = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="second", canonical_url="https://example.test/second", title="Second",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    run = database.create_batch_run(
        command="extract", article_ids=(first.id, second.id), selector={}, limits={}
    )
    database.complete_batch_run_item(run.id, first.id, status="succeeded")
    database.complete_batch_run_item(
        run.id, second.id, status="skipped", error_code="provider_budget_exhausted"
    )
    database.update_batch_run_status(run.id, status="interrupted")

    called: list[int] = []

    class FakeExtractionService:
        def extract_articles(self, articles):
            called.extend(article.id for article in articles)
            return [ExtractionOutcome(article.id, "succeeded") for article in articles]

    monkeypatch.setattr(
        "rss_zen.cli.ExtractionService", lambda *a, **k: FakeExtractionService()
    )
    result = runner.invoke(
        app, ["extract", "--resume", str(run.id), "--config", str(config_path)]
    )

    assert result.exit_code == 0
    assert called == [second.id]
    assert database.get_batch_run(run.id).status == "succeeded"


def test_translate_resume_only_processes_unfinished_checkpoint_items(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    first = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="first", canonical_url="https://example.test/first", title="First",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    second = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="second", canonical_url="https://example.test/second", title="Second",
            summary=None, content=None, author=None, categories=(), published_at=None,
        ),
    ).article
    run = database.create_batch_run(
        command="translate",
        article_ids=(first.id, second.id),
        selector={"article_ids": [first.id, second.id]},
        limits={},
    )
    database.complete_batch_run_item(run.id, first.id, status="succeeded")
    database.complete_batch_run_item(
        run.id, second.id, status="skipped", error_code="provider_budget_exhausted"
    )
    database.update_batch_run_status(run.id, status="interrupted")

    called: list[int] = []

    class FakeService:
        def translate_article(self, record, *, force=False):
            from rss_zen.translation import TranslationOutcome

            called.append(record.id)
            return TranslationOutcome(record.id, "succeeded", "free")

    monkeypatch.setattr("rss_zen.cli.build_translation_service", lambda *a, **k: FakeService())
    result = runner.invoke(
        app, ["translate", "--resume", str(run.id), "--config", str(config_path)]
    )

    assert result.exit_code == 0
    assert called == [second.id]
    assert database.get_batch_run(run.id).status == "succeeded"


def test_translate_resume_limit_keeps_remaining_items_interrupted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"
""",
        encoding="utf-8",
    )
    from rss_zen.db import ArticleInput, Database, FeedInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    articles = [
        database.reconcile_article(
            feed.id,
            ArticleInput(
                guid=f"a{index}", canonical_url=f"https://example.test/{index}", title=str(index),
                summary=None, content=None, author=None, categories=(), published_at=None,
            ),
        ).article
        for index in range(2)
    ]
    run = database.create_batch_run(
        command="translate",
        article_ids=tuple(article.id for article in articles),
        selector={},
        limits={},
    )
    database.update_batch_run_status(run.id, status="interrupted")

    class FakeService:
        def translate_article(self, record, *, force=False):
            from rss_zen.translation import TranslationOutcome
            return TranslationOutcome(record.id, "succeeded", "free")

    monkeypatch.setattr("rss_zen.cli.build_translation_service", lambda *a, **k: FakeService())
    result = runner.invoke(
        app, ["translate", "--resume", str(run.id), "--limit", "1", "--config", str(config_path)]
    )

    assert result.exit_code == 0
    assert database.get_batch_run(run.id).status == "interrupted"
    assert len(database.batch_run_resumable_article_ids(run.id)) == 1


def test_translate_retries_failed_articles_by_status(tmp_path, monkeypatch) -> None:
    """--status failed selects failed articles and re-translates them."""
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        """
[database]
path = "rss-zen.sqlite3"

[translation]
target_language = "zh-CN"

[[translation.providers]]
name = "free"
kind = "libretranslate"
endpoint = "https://translate.example.test/translate"
api_key_env = "FREE_TRANSLATION_API_KEY"

[[feeds]]
name = "Example"
url = "https://example.test/feed.xml"
""",
        encoding="utf-8",
    )

    # Seed one failed translation directly in the repository.
    from rss_zen.db import ArticleInput, Database, FeedInput, TranslationInput

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    article = database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="a1",
            canonical_url="https://example.test/one",
            title="One",
            summary=None,
            content=None,
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article
    database.save_translation(
        TranslationInput(
            article_id=article.id,
            target_language="zh-CN",
            title=None,
            summary=None,
            content=None,
            provider_name="free",
            provider_model=None,
            status="failed",
            source_hash="hash",
            error_code="translation_provider_error",
            error_message="boom",
            attempt_count=1,
            terminal=False,
        )
    )

    called: list[int] = []

    class FakeService:
        def translate_article(self, record, *, force=False):
            called.append(record.id)
            from rss_zen.translation import TranslationOutcome

            return TranslationOutcome(record.id, "succeeded", "free")

    monkeypatch.setattr("rss_zen.cli.build_translation_service", lambda *a, **k: FakeService())

    result = runner.invoke(app, ["translate", "--status", "failed", "--config", str(config_path)])

    assert result.exit_code == 0
    assert called == [article.id]
    assert f"article_id={article.id} status=succeeded" in result.stdout

    report_path = tmp_path / "reports" / "translate.json"
    dry_run = runner.invoke(
        app,
        [
            "translate",
            "--status",
            "failed",
            "--dry-run",
            "--report-json",
            str(report_path),
            "--config",
            str(config_path),
        ],
    )

    assert dry_run.exit_code == 0
    assert called == [article.id]
    import json as _json

    report = _json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["command"] == "translate"
    assert report["dry_run"] is True
    assert report["selected_articles"] == 1
    assert report["estimate_only"] is True
    assert not list(report_path.parent.glob("*.tmp"))
