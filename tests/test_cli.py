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
