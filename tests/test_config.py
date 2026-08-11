from __future__ import annotations

from pathlib import Path

import pytest

from rss_zen.config import load_config
from rss_zen.errors import ConfigurationError


def _toml_config() -> str:
    return """
[database]
path = "data/rss-zen.sqlite3"

[service]
default_poll_interval_minutes = 30

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
reasoning_effort = "none"
timeout_seconds = 120
max_chars = 3000

[anysearch]
api_key_env = "ANYSEARCH_API_KEY"
tag = "general.general"
zone = "intl"
language = "zh-CN"
max_results = 3

[[feeds]]
name = "Example"
url = "https://example.test/feed.xml"
categories = ["technology"]
poll_interval_minutes = 15
language = "en"

[[exports]]
name = "daily"
output_path = "exports/daily.md"
title = "Daily reading"
"""


def _yaml_config() -> str:
    return """
database:
  path: data/rss-zen.sqlite3
service:
  default_poll_interval_minutes: 30
translation:
  target_language: zh-CN
  providers:
    - name: free
      kind: libretranslate
      endpoint: https://translate.example.test/translate
      api_key_env: FREE_TRANSLATION_API_KEY
    - name: ai
      kind: openai_compatible
      endpoint: https://ai.example.test/v1
      api_key_env: AI_TRANSLATION_API_KEY
      model: translation-model
      reasoning_effort: none
      timeout_seconds: 120
      max_chars: 3000
anysearch:
  api_key_env: ANYSEARCH_API_KEY
  tag: general.general
  zone: intl
  language: zh-CN
  max_results: 3
feeds:
  - name: Example
    url: https://example.test/feed.xml
    categories: [technology]
    poll_interval_minutes: 15
    language: en
exports:
  - name: daily
    output_path: exports/daily.md
    title: Daily reading
"""


@pytest.fixture(autouse=True)
def _translation_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREE_TRANSLATION_API_KEY", "free-secret")
    monkeypatch.setenv("AI_TRANSLATION_API_KEY", "ai-secret")
    monkeypatch.setenv("ANYSEARCH_API_KEY", "anysearch-secret")


@pytest.mark.parametrize(
    ("filename", "content"),
    [("rss-zen.toml", _toml_config()), ("rss-zen.yaml", _yaml_config())],
)
def test_loads_equivalent_toml_and_yaml_config(tmp_path: Path, filename: str, content: str) -> None:
    config_path = tmp_path / filename
    config_path.write_text(content, encoding="utf-8")

    config = load_config(config_path)

    assert config.database.path == Path("data/rss-zen.sqlite3")
    assert config.feeds[0].name == "Example"
    assert config.exports[0].name == "daily"
    assert config.anysearch.max_results == 3
    assert config.limits.max_feed_response_bytes == 10_000_000
    assert config.limits.max_entries_per_feed == 500
    assert config.service.translation_max_attempts == 5
    assert config.translation.providers[0].api_key == "free-secret"
    ai_provider = config.translation.providers[1]
    assert ai_provider.reasoning_effort == "none"
    assert ai_provider.timeout_seconds == 120
    assert ai_provider.max_chars == 3000


def test_rejects_missing_database_path(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text("[service]\ndefault_poll_interval_minutes = 30\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="database"):
        load_config(config_path)


def test_rejects_non_http_feed_url(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config().replace("https://example.test/feed.xml", "ftp://example.test/feed.xml"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="HTTP"):
        load_config(config_path)


def test_rejects_missing_configured_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FREE_TRANSLATION_API_KEY")
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(_toml_config(), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="FREE_TRANSLATION_API_KEY"):
        load_config(config_path)


def test_rejects_duplicate_export_profile_names(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config()
        + """
[[exports]]
name = "daily"
output_path = "exports/another.md"
title = "Another daily reading"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="duplicate export"):
        load_config(config_path)


def test_rejects_unsupported_translation_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config().replace('kind = "libretranslate"', 'kind = "unsupported"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unsupported"):
        load_config(config_path)


def test_feed_language_override_wins_over_detection(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(_toml_config(), encoding="utf-8")

    config = load_config(config_path)

    assert config.effective_source_language(config.feeds[0], detected_language="de") == "en"


def test_rejects_http_feed_url(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config().replace("https://example.test/feed.xml", "http://example.test/feed.xml"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="HTTPS"):
        load_config(config_path)


def test_rejects_url_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config().replace(
            "https://example.test/feed.xml", "https://user:password@example.test/feed.xml"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="credentials"):
        load_config(config_path)


def test_rejects_invalid_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "rss-zen.toml"
    config_path.write_text(
        _toml_config() + "\n[limits]\nmax_entries_per_feed = 0\n", encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="max_entries_per_feed"):
        load_config(config_path)
