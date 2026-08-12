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
