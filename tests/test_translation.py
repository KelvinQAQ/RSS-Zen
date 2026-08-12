from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rss_zen.db import ArticleInput, Database, FeedInput
from rss_zen.translation import TranslationProviderError, TranslationService


@dataclass
class FailingProvider:
    name: str = "free"
    model: str | None = None

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        raise TranslationProviderError("translation_rate_limited", "free translator is unavailable")


@dataclass
class RetryableFailingProvider:
    name = "retryable"
    model = None

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        raise TranslationProviderError(
            "translation_rate_limited", "translator is temporarily unavailable", retryable=True
        )


@dataclass
class RecordingProvider:
    name: str = "ai"
    model: str | None = "translation-model"
    calls: list[tuple[str, str | None, str]] = field(default_factory=list)

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        self.calls.append((text, source_language, target_language))
        return f"ZH:{text}"


def _article(database: Database):
    feed = database.upsert_feed(FeedInput(name="Example", url="https://example.test/feed.xml"))
    return database.reconcile_article(
        feed.id,
        ArticleInput(
            guid="article-1",
            canonical_url="https://example.test/article-1",
            title="English title",
            summary="English summary",
            content="This is a longer English article body for language detection.",
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article


def test_falls_back_to_ai_provider_and_persists_translation(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    fallback = RecordingProvider()
    service = TranslationService(database, [FailingProvider(), fallback], target_language="zh-CN")

    result = service.translate_article(article, source_language_override="en")

    stored = database.latest_translation(article.id, "zh-CN")
    assert result.status == "succeeded"
    assert result.provider_name == "ai"
    assert stored is not None
    assert stored.title == "ZH:English title"
    assert stored.provider_name == "ai"
    assert fallback.calls[0][1] == "en"


def test_marks_article_failed_when_all_translation_providers_fail(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    service = TranslationService(database, [FailingProvider()], target_language="zh-CN")

    result = service.translate_article(article, source_language_override="en")

    stored = database.latest_translation(article.id, "zh-CN")
    assert result.status == "failed"
    assert stored is not None
    assert stored.error_code == "translation_rate_limited"


def test_source_change_retranslates_with_new_content_hash(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    provider = RecordingProvider()
    service = TranslationService(database, [provider], target_language="zh-CN")

    service.translate_article(article, source_language_override="en")
    changed = database.reconcile_article(
        article.feed_id,
        ArticleInput(
            guid="article-1",
            canonical_url=article.canonical_url,
            title="English title",
            summary="Changed summary",
            content="This is changed English article body for language detection.",
            author=None,
            categories=(),
            published_at=None,
        ),
    ).article
    service.translate_article(changed, source_language_override="en")

    stored = database.latest_translation(article.id, "zh-CN")
    assert stored is not None
    assert stored.source_hash == changed.content_hash
    assert stored.summary == "ZH:Changed summary"
    assert len(provider.calls) == 6


def test_feed_language_override_is_stored_in_article(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    provider = RecordingProvider()
    service = TranslationService(database, [provider], target_language="zh-CN")

    service.translate_article(article, source_language_override="fr")

    updated_article = database.get_article(article.id)
    assert updated_article.source_language == "fr"
    assert provider.calls[0][1] == "fr"


def test_mymemory_adapter_uses_documented_language_pair() -> None:
    import httpx

    from rss_zen.models import TranslationProviderConfig
    from rss_zen.translation import MyMemoryProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Hello"
        assert request.url.params["langpair"] == "en|zh-CN"
        return httpx.Response(
            200,
            json={"responseData": {"translatedText": "你好"}, "responseStatus": 200},
        )

    provider = MyMemoryProvider(
        TranslationProviderConfig(
            name="free",
            kind="mymemory",
            endpoint="https://api.mymemory.translated.net/get",
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.translate("Hello", "en", "zh-CN") == "你好"


def test_mymemory_adapter_splits_long_input_on_utf8_boundaries() -> None:
    import httpx

    from rss_zen.models import TranslationProviderConfig
    from rss_zen.translation import MyMemoryProvider

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        text = request.url.params["q"]
        assert len(text.encode("utf-8")) <= 450
        requests.append(text)
        return httpx.Response(
            200,
            json={"responseData": {"translatedText": text}, "responseStatus": 200},
        )

    provider = MyMemoryProvider(
        TranslationProviderConfig(
            name="free",
            kind="mymemory",
            endpoint="https://api.mymemory.translated.net/get",
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    source = "word " * 200

    assert provider.translate(source, "en", "zh-CN") == source.strip()
    assert len(requests) > 1


def test_openai_compatible_sends_reasoning_effort_and_timeout() -> None:
    import httpx

    from rss_zen.models import TranslationProviderConfig
    from rss_zen.translation import OpenAICompatibleProvider

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def post(self, url: str, **kwargs: object) -> httpx.Response:
            self.calls.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "??"}}]},
            )

    provider = OpenAICompatibleProvider(
        TranslationProviderConfig(
            name="ai",
            kind="openai_compatible",
            endpoint="https://ai.example.test/v1",
            api_key="secret",
            model="translation-model",
            reasoning_effort="none",
            timeout_seconds=120,
        ),
        RecordingClient(),  # type: ignore[arg-type]
    )

    assert provider.translate("Hello", "en", "zh-CN") == "??"

    call = provider._client.calls[0]  # type: ignore[attr-defined]
    assert call["url"] == "https://ai.example.test/v1/chat/completions"
    assert call["timeout"] == 120
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"]["reasoning_effort"] == "none"
    assert call["json"]["model"] == "translation-model"


def test_openai_compatible_splits_long_text_into_chunks() -> None:
    import json

    import httpx

    from rss_zen.models import TranslationProviderConfig
    from rss_zen.translation import OpenAICompatibleProvider

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        requests.append(body)
        text = str(body["messages"][1]["content"]).split("\n\n", 1)[1]
        assert len(text) <= 60
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": f"ZH:{text}"}}]},
        )

    provider = OpenAICompatibleProvider(
        TranslationProviderConfig(
            name="ai",
            kind="openai_compatible",
            endpoint="https://ai.example.test/v1",
            model="translation-model",
            max_chars=60,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    source = "\n".join(f"paragraph number {i} of the daily brief" for i in range(20))
    translated = provider.translate(source, "en", "zh-CN")

    assert len(requests) > 1
    assert translated == "\n".join(f"ZH:{line}" for line in source.split("\n"))


def test_openai_compatible_defaults_to_4000_char_chunks() -> None:
    import json

    import httpx

    from rss_zen.models import TranslationProviderConfig
    from rss_zen.translation import OpenAICompatibleProvider

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        requests.append(body)
        text = str(body["messages"][1]["content"]).split("\n\n", 1)[1]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": f"ZH:{text}"}}]},
        )

    provider = OpenAICompatibleProvider(
        TranslationProviderConfig(
            name="ai",
            kind="openai_compatible",
            endpoint="https://ai.example.test/v1",
            model="translation-model",
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    source = "x" * 4500
    translated = provider.translate(source, "en", "zh-CN")

    assert len(requests) == 2
    assert len(str(requests[0]["messages"][1]["content"]).split("\n\n", 1)[1]) == 4000
    assert len(str(requests[1]["messages"][1]["content"]).split("\n\n", 1)[1]) == 500
    assert translated == "ZH:" + "x" * 4000 + "\nZH:" + "x" * 500


def test_split_text_chunks_keeps_lines_intact() -> None:
    from rss_zen.translation import _split_text_chunks

    source = "\n".join(f"line {i} content here" for i in range(10))
    chunks = _split_text_chunks(source, max_chars=30)

    assert "\n".join(chunks) == source
    assert all(len(chunk) <= 30 for chunk in chunks)
    assert len(chunks) > 1


def test_retryable_failure_is_due_and_eventually_becomes_terminal(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from random import Random

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    def now() -> datetime:
        return datetime(2026, 8, 11, tzinfo=UTC)
    service = TranslationService(
        database,
        [RetryableFailingProvider()],
        target_language="zh-CN",
        max_attempts=2,
        now=now,
        random=Random(0),
    )

    first = service.translate_article(article, source_language_override="en")
    stored = database.latest_translation(article.id, "zh-CN")

    assert first.status == "failed"
    assert stored is not None
    assert stored.attempt_count == 1
    assert stored.terminal is False
    assert stored.next_retry_at is not None

    outcomes = service.retry_due(limit=10)
    stored = database.latest_translation(article.id, "zh-CN")

    assert len(outcomes) == 0
    assert stored is not None

    outcomes = service.retry_due(limit=10)
    assert outcomes == []

    due = database.list_due_translations("zh-CN", now="2030-01-01T00:00:00+00:00")
    assert len(due) == 1

    service.translate_article(due[0][0], force=True)
    stored = database.latest_translation(article.id, "zh-CN")
    assert stored is not None
    assert stored.attempt_count == 2
    assert stored.terminal is True


def test_translation_limits_source_field_size(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    article = _article(database)
    provider = RecordingProvider()
    service = TranslationService(
        database, [provider], target_language="zh-CN", max_translation_chars=7
    )

    service.translate_article(article, source_language_override="en")

    assert provider.calls[0][0] == "English"
    assert provider.calls[1][0] == "English"
    assert provider.calls[2][0] == "This is"


def test_google_provider_requires_deep_translator(monkeypatch) -> None:
    """GoogleProvider raises a clear, non-retryable error when deep_translator is absent."""
    import pytest

    import rss_zen.translation as translation_module

    monkeypatch.setattr(translation_module, "GoogleTranslator", None)
    config = translation_module.TranslationProviderConfig(name="google", kind="google")
    provider = translation_module.GoogleProvider(config)

    with pytest.raises(TranslationProviderError, match="not installed"):
        provider.translate("Hello", None, "zh-CN")


def test_google_provider_clamps_chunk_size_below_deep_translator_cap(monkeypatch) -> None:
    """Chunks must stay strictly under deep_translator's 5000-char Google limit."""
    import rss_zen.translation as translation_module

    calls: list[str] = []

    class FakeGoogle:
        def __init__(self, source: str, target: str) -> None:
            pass

        def translate(self, text: str) -> str:
            calls.append(text)
            return text

    monkeypatch.setattr(translation_module, "GoogleTranslator", FakeGoogle)
    config = translation_module.TranslationProviderConfig(name="google", kind="google")
    provider = translation_module.GoogleProvider(config)

    # Exactly 5000 characters would trip deep_translator's ``len < 5000`` guard;
    # the provider must split it into chunks of at most 4999 characters.
    text = "a" * 5000
    provider.translate(text, None, "zh-CN")

    assert all(len(chunk) < 5000 for chunk in calls)
    assert sum(len(chunk) for chunk in calls) == 5000

    # An operator-configured max_chars above the hard cap is clamped too.
    config = translation_module.TranslationProviderConfig(
        name="google", kind="google", max_chars=9000
    )
    provider = translation_module.GoogleProvider(config)
    assert provider._max_chars == 4999
