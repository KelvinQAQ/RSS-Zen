"""Language detection and pluggable Simplified Chinese translation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Protocol

import httpx
from langdetect import DetectorFactory, LangDetectException, detect

from rss_zen.db import ArticleRecord, Database, TranslationInput
from rss_zen.errors import AppError
from rss_zen.models import TranslationProviderConfig, TranslationSettings

try:
    from deep_translator import GoogleTranslator
except Exception:  # pragma: no cover - optional dependency
    GoogleTranslator = None

DetectorFactory.seed = 0


class TranslationProviderError(AppError):
    """A provider-level error that may allow fallback to another backend."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(code, message, retryable=retryable)


class TranslationProvider(Protocol):
    """A backend that can translate one text field."""

    name: str
    model: str | None

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        """Translate text or raise TranslationProviderError."""


@dataclass(frozen=True)
class TranslationOutcome:
    """The persistent result of translating one article."""

    article_id: int
    status: str
    provider_name: str
    error_code: str | None = None


@dataclass(frozen=True)
class TextTranslation:
    """Translation result for content that is not an RSS source field."""

    text: str
    provider_name: str
    provider_model: str | None


class LibreTranslateProvider:
    """Adapter for a LibreTranslate-compatible HTTP endpoint."""

    def __init__(self, config: TranslationProviderConfig, client: httpx.Client) -> None:
        self.name = config.name
        self.model = None
        self._endpoint = config.endpoint
        self._api_key = config.api_key
        self._timeout = config.timeout_seconds
        self._client = client

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        payload: dict[str, str] = {
            "q": text,
            "source": source_language or "auto",
            "target": _libre_language(target_language),
            "format": "text",
        }
        if self._api_key:
            payload["api_key"] = self._api_key
        try:
            response = self._client.post(self._endpoint, json=payload, timeout=self._timeout)
        except httpx.TimeoutException as error:
            raise TranslationProviderError(
                "translation_timeout", "translation request timed out", retryable=True
            ) from error
        except httpx.RequestError as error:
            raise TranslationProviderError(
                "translation_network_error", "translation request failed", retryable=True
            ) from error
        if response.status_code >= 400:
            raise TranslationProviderError(
                f"translation_http_{response.status_code}",
                f"translation provider returned HTTP {response.status_code}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            translated = response.json().get("translatedText")
        except ValueError as error:
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned invalid JSON"
            ) from error
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned no translated text"
            )
        return translated


class OpenAICompatibleProvider:
    """Adapter for OpenAI-compatible Chat Completions translation endpoints."""

    _default_max_chars = 4000

    def __init__(self, config: TranslationProviderConfig, client: httpx.Client) -> None:
        self.name = config.name
        self.model = config.model
        self._endpoint = _chat_completions_url(config.endpoint)
        self._api_key = config.api_key
        self._reasoning_effort = config.reasoning_effort
        self._timeout = config.timeout_seconds
        self._max_chars = config.max_chars or self._default_max_chars
        self._client = client

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        chunks = _split_text_chunks(text, max_chars=self._max_chars)
        translated_chunks = [
            self._translate_chunk(chunk, source_language, target_language) for chunk in chunks
        ]
        return "\n".join(translated_chunks).strip()

    def _translate_chunk(
        self, text: str, source_language: str | None, target_language: str
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Translate the user's text into {target_language}. Preserve links, "
                        "formatting, and factual meaning. Output only the translation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Source language: {source_language or 'auto'}\n\n{text}",
                },
            ],
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        try:
            response = self._client.post(
                self._endpoint, headers=headers, json=payload, timeout=self._timeout
            )
        except httpx.TimeoutException as error:
            raise TranslationProviderError(
                "translation_timeout", "translation request timed out", retryable=True
            ) from error
        except httpx.RequestError as error:
            raise TranslationProviderError(
                "translation_network_error", "translation request failed", retryable=True
            ) from error
        if response.status_code >= 400:
            raise TranslationProviderError(
                f"translation_http_{response.status_code}",
                f"translation provider returned HTTP {response.status_code}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            choices = response.json().get("choices", [])
            translated = choices[0]["message"]["content"] if choices else None
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned invalid JSON"
            ) from error
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned no translated text"
            )
        return translated


class MyMemoryProvider:
    """Adapter for MyMemory's anonymous, rate-limited translation endpoint."""

    def __init__(self, config: TranslationProviderConfig, client: httpx.Client) -> None:
        self.name = config.name
        self.model = None
        self._endpoint = config.endpoint
        self._timeout = config.timeout_seconds
        self._client = client

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        chunks = _split_mymemory_text(text)
        translated_chunks = [
            self._translate_chunk(chunk, source_language, target_language) for chunk in chunks
        ]
        return " ".join(translated_chunks).strip()

    def _translate_chunk(
        self, text: str, source_language: str | None, target_language: str
    ) -> str:
        language_pair = f"{source_language or 'autodetect'}|{_mymemory_language(target_language)}"
        try:
            response = self._client.get(
                self._endpoint,
                params={"q": text, "langpair": language_pair},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise TranslationProviderError(
                "translation_timeout", "translation request timed out", retryable=True
            ) from error
        except httpx.RequestError as error:
            raise TranslationProviderError(
                "translation_network_error", "translation request failed", retryable=True
            ) from error
        if response.status_code >= 400:
            raise TranslationProviderError(
                f"translation_http_{response.status_code}",
                f"translation provider returned HTTP {response.status_code}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            body = response.json()
            response_data = body.get("responseData", {})
            translated = response_data.get("translatedText")
            status_code = body.get("responseStatus")
        except (AttributeError, ValueError) as error:
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned invalid JSON"
            ) from error
        if status_code != 200:
            raise TranslationProviderError(
                "translation_provider_error", "translation provider rejected the request"
            )
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationProviderError(
                "translation_invalid_response", "translation provider returned no translated text"
            )
        return translated


class GoogleProvider:
    """Translate through deep_translator's free GoogleTranslate web endpoint.

    Requires no API key or endpoint configuration. Falls back gracefully when the
    deep_translator package is unavailable or a request is rejected. Long text is
    split into per-request chunks because GoogleTranslate caps each request.
    """

    _default_max_chars = 5000

    def __init__(self, config: TranslationProviderConfig) -> None:
        self.name = config.name
        self.model = None
        self._timeout = config.timeout_seconds
        self._max_chars = config.max_chars or self._default_max_chars

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        if GoogleTranslator is None:
            raise TranslationProviderError(
                "translation_unavailable",
                "deep_translator is not installed; google provider unavailable",
                retryable=False,
            )
        chunks = _split_text_chunks(text, max_chars=self._max_chars)
        translated_chunks = [
            self._translate_chunk(chunk, source_language, target_language) for chunk in chunks
        ]
        return "\n".join(translated_chunks).strip()

    def _translate_chunk(self, text: str, source_language: str | None, target_language: str) -> str:
        try:
            translator = GoogleTranslator(source="auto", target=target_language)
            translated = translator.translate(text)
        except TranslationProviderError:
            raise
        except Exception as error:  # deep_translator raises many exception types
            raise TranslationProviderError(
                "translation_provider_error",
                f"Google translation request failed: {error}",
                retryable=True,
            ) from error
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationProviderError(
                "translation_invalid_response", "Google returned no translated text"
            )
        return translated


class TranslationService:
    """Translate source articles through a provider chain and persist every outcome."""

    def __init__(
        self,
        database: Database,
        providers: Sequence[TranslationProvider],
        *,
        target_language: str,
        max_attempts: int = 5,
        max_backoff_minutes: int = 360,
        max_translation_chars: int = 100_000,
        now: Callable[[], datetime] | None = None,
        random: Random | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one translation provider is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if max_translation_chars < 1:
            raise ValueError("max_translation_chars must be at least one")
        self._database = database
        self._providers = providers
        self._target_language = target_language
        self._max_attempts = max_attempts
        self._max_backoff_minutes = max_backoff_minutes
        self._max_translation_chars = max_translation_chars
        self._now = now or (lambda: datetime.now(UTC))
        self._random = random or Random()

    def translate_article(
        self,
        article: ArticleRecord,
        *,
        source_language_override: str | None = None,
        force: bool = False,
    ) -> TranslationOutcome:
        """Translate one article and persist retryable state for provider failures."""
        detected_language = article.detected_language or detect_article_language(article)
        source_language = source_language_override or article.source_language or detected_language
        article = self._database.update_article_languages(
            article.id,
            detected_language=detected_language,
            source_language=source_language,
        )
        existing = self._database.latest_translation(article.id, self._target_language)
        if (
            not force
            and existing
            and existing.status == "succeeded"
            and existing.source_hash == article.content_hash
        ):
            return TranslationOutcome(article.id, "succeeded", existing.provider_name)

        pending = self._database.begin_translation(
            article,
            target_language=self._target_language,
            provider_name=self._providers[0].name,
            provider_model=self._providers[0].model,
        )
        last_error: TranslationProviderError | None = None
        for provider in self._providers:
            try:
                title, summary, content = _translate_fields(
                    provider,
                    article,
                    source_language,
                    self._target_language,
                    max_chars=self._max_translation_chars,
                )
            except TranslationProviderError as error:
                last_error = error
                continue
            self._database.save_translation(
                TranslationInput(
                    article_id=article.id,
                    target_language=self._target_language,
                    title=title,
                    summary=summary,
                    content=content,
                    provider_name=provider.name,
                    provider_model=provider.model,
                    status="succeeded",
                    source_hash=article.content_hash,
                    attempt_count=pending.attempt_count + 1,
                    last_attempt_at=self._timestamp(),
                )
            )
            return TranslationOutcome(article.id, "succeeded", provider.name)

        error = last_error or TranslationProviderError(
            "translation_failed", "no translation provider produced a result"
        )
        attempts = pending.attempt_count + 1
        terminal = not error.retryable or attempts >= self._max_attempts
        self._database.save_translation(
            TranslationInput(
                article_id=article.id,
                target_language=self._target_language,
                title=None,
                summary=None,
                content=None,
                provider_name=self._providers[-1].name,
                provider_model=self._providers[-1].model,
                status="failed",
                source_hash=article.content_hash,
                error_code=error.code,
                error_message=error.message,
                attempt_count=attempts,
                next_retry_at=None if terminal else self._next_retry_at(attempts),
                last_attempt_at=self._timestamp(),
                terminal=terminal,
            )
        )
        return TranslationOutcome(article.id, "failed", self._providers[-1].name, error.code)

    def retry_due(self, *, limit: int = 100) -> list[TranslationOutcome]:
        """Retry due, non-terminal persisted translations without invoking extraction."""
        outcomes = []
        for article, _translation in self._database.list_due_translations(
            self._target_language, now=self._timestamp(), limit=limit
        ):
            outcomes.append(self.translate_article(article, force=True))
        return outcomes

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()

    def _next_retry_at(self, attempts: int) -> str:
        minutes = min(2 ** (attempts - 1), self._max_backoff_minutes)
        jitter = self._random.uniform(0.0, min(minutes * 0.1, 5.0))
        return (self._now().astimezone(UTC) + timedelta(minutes=minutes + jitter)).isoformat()

    def translate_text(self, text: str, *, source_language: str | None = None) -> TextTranslation:
        """Translate arbitrary extracted content with the configured fallback chain."""
        detected_language = source_language or _detect_text_language(text)
        last_error: TranslationProviderError | None = None
        for provider in self._providers:
            try:
                translated = provider.translate(text, detected_language, self._target_language)
            except TranslationProviderError as error:
                last_error = error
                continue
            return TextTranslation(translated, provider.name, provider.model)
        if last_error is not None:
            raise last_error
        raise TranslationProviderError(
            "translation_failed", "no translation provider produced a result"
        )


def build_translation_service(
    database: Database,
    settings: TranslationSettings,
    client: httpx.Client,
    *,
    max_attempts: int = 5,
    max_backoff_minutes: int = 360,
    max_translation_chars: int = 100_000,
) -> TranslationService:
    """Build configured adapters in declared priority order."""
    providers: list[TranslationProvider] = []
    for provider in settings.providers:
        if provider.kind == "libretranslate":
            providers.append(LibreTranslateProvider(provider, client))
        elif provider.kind == "mymemory":
            providers.append(MyMemoryProvider(provider, client))
        elif provider.kind == "google":
            providers.append(GoogleProvider(provider))
        else:
            providers.append(OpenAICompatibleProvider(provider, client))
    return TranslationService(
        database,
        providers,
        target_language=settings.target_language,
        max_attempts=max_attempts,
        max_backoff_minutes=max_backoff_minutes,
        max_translation_chars=max_translation_chars,
    )


def detect_article_language(article: ArticleRecord) -> str | None:
    """Run deterministic local language detection against the richest source text."""
    text = "\n".join(part for part in (article.title, article.summary, article.content) if part)
    return _detect_text_language(text)


def _detect_text_language(text: str) -> str | None:
    if len(text.strip()) < 3:
        return None
    try:
        return detect(text)
    except LangDetectException:
        return None


def _translate_fields(
    provider: TranslationProvider,
    article: ArticleRecord,
    source_language: str | None,
    target_language: str,
    *,
    max_chars: int,
) -> tuple[str | None, str | None, str | None]:
    values = []
    for value in (article.title, article.summary, article.content):
        values.append(
            provider.translate(value[:max_chars], source_language, target_language)
            if value
            else None
        )
    return values[0], values[1], values[2]


def _libre_language(language: str) -> str:
    return "zh" if language.casefold() == "zh-cn" else language


def _mymemory_language(language: str) -> str:
    return "zh-CN" if language.casefold() == "zh-cn" else language


def _split_text_chunks(text: str, *, max_chars: int = 4000) -> list[str]:
    """Split long text into OpenAI-compatible requests under ``max_chars`` characters."""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in text.split("\n"):
        for piece in _split_oversized_line(line, max_chars):
            separator = 1 if current else 0
            if current and current_size + separator + len(piece) > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
            current.append(piece)
            current_size += separator + len(piece)
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def _split_oversized_line(line: str, max_chars: int) -> list[str]:
    """Split one over-long line on spaces, then hard-cut as a last resort."""
    if len(line) <= max_chars:
        return [line]
    pieces: list[str] = []
    remaining = line
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip(" ")
    pieces.append(remaining)
    return pieces


def _split_mymemory_text(text: str, *, max_bytes: int = 450) -> list[str]:
    """Split text on whitespace while keeping every UTF-8 request under the limit."""
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for token in text.split(" "):
        token_bytes = len(token.encode("utf-8"))
        separator_bytes = 1 if current else 0
        if current and current_bytes + separator_bytes + token_bytes > max_bytes:
            chunks.append(" ".join(current))
            current = []
            current_bytes = 0
        if token_bytes > max_bytes:
            encoded = token.encode("utf-8")
            while encoded:
                piece = encoded[:max_bytes]
                while piece and (piece[-1] & 0xC0) == 0x80:
                    piece = piece[:-1]
                if not piece:
                    piece = encoded[:max_bytes]
                chunks.append(piece.decode("utf-8", errors="ignore"))
                encoded = encoded[len(piece) :]
            continue
        current.append(token)
        current_bytes += separator_bytes + token_bytes
    if current:
        chunks.append(" ".join(current))
    return chunks or [""]


def _chat_completions_url(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    return trimmed if trimmed.endswith("/chat/completions") else f"{trimmed}/chat/completions"
