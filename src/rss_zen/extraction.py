"""Explicit full-text extraction through the documented AnySearch API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from rss_zen.db import ArticleRecord, Database, ExtractionInput
from rss_zen.errors import AppError
from rss_zen.models import AnySearchSettings


class Extractor(Protocol):
    """Retrieve clean full text for one exact source URL."""

    def extract(self, source_url: str) -> ExtractionResponse:
        """Return a source-matched extraction or raise AppError."""


class TextTranslator(Protocol):
    """Translate extracted text without mutating RSS-field translations."""

    def translate_text(self, text: str, *, source_language: str | None) -> object:
        """Translate text and return an object carrying provider metadata."""


@dataclass(frozen=True)
class ExtractionResponse:
    """Validated content from a search result matching the requested source URL."""

    content: str
    source_url: str
    request_id: str | None


@dataclass(frozen=True)
class ExtractionOutcome:
    """Persistent result of extracting one article."""

    article_id: int
    status: str
    error_code: str | None = None


class AnySearchExtractor:
    """Use the documented `/v1/search` endpoint as a conservative URL extractor."""

    def __init__(self, settings: AnySearchSettings, client: httpx.Client) -> None:
        self._settings = settings
        self._client = client

    def extract(self, source_url: str) -> ExtractionResponse:
        """Search for a URL and accept only a response with the exact canonical URL."""
        canonical_url = _normalize_url(source_url)
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        payload: dict[str, object] = {
            "query": canonical_url,
            "tag": self._settings.tag,
            "max_results": self._settings.max_results,
        }
        if self._settings.zone is not None:
            payload["zone"] = self._settings.zone
        if self._settings.language is not None:
            payload["language"] = self._settings.language
        try:
            response = self._client.post(
                f"{self._settings.base_url}/v1/search", headers=headers, json=payload
            )
        except httpx.TimeoutException as error:
            raise AppError(
                "anysearch_timeout", "AnySearch request timed out", retryable=True, cause=error
            ) from error
        except httpx.RequestError as error:
            raise AppError(
                "anysearch_network_error", "AnySearch request failed", retryable=True, cause=error
            ) from error

        request_id, body = _response_body(response)
        if response.status_code >= 400:
            raise _http_error(response.status_code, request_id, body)
        if body.get("code") != 0:
            message = body.get("message")
            safe_message = message if isinstance(message, str) else "AnySearch returned an error"
            raise AppError("anysearch_api_error", safe_message)

        data = body.get("data")
        results = data.get("results") if isinstance(data, Mapping) else None
        if not isinstance(results, list):
            raise AppError("anysearch_invalid_response", "AnySearch returned no result list")
        for result in results:
            if not isinstance(result, Mapping):
                continue
            result_url = result.get("url")
            content = result.get("content")
            if (
                isinstance(result_url, str)
                and isinstance(content, str)
                and content.strip()
                and _normalize_url(result_url) == canonical_url
            ):
                return ExtractionResponse(content, canonical_url, request_id)
        raise AppError(
            "anysearch_exact_source_not_found",
            "AnySearch did not return content for the exact article URL",
        )


class ExtractionService:
    """Run explicit extraction jobs and preserve their source and translated text separately."""

    def __init__(
        self, database: Database, extractor: Extractor, *, translator: TextTranslator | None = None
    ) -> None:
        self._database = database
        self._extractor = extractor
        self._translator = translator

    def extract_selected(
        self,
        *,
        article_ids: tuple[int, ...] = (),
        source: str | None = None,
        without_extraction: bool = False,
        published_after: str | None = None,
        published_before: str | None = None,
    ) -> list[ExtractionOutcome]:
        """Select articles through repository filters, then extract each independently."""
        articles = self._database.list_articles(
            article_ids=article_ids,
            source=source,
            without_extraction=without_extraction,
            published_after=published_after,
            published_before=published_before,
        )
        return self.extract_articles(articles)

    def extract_articles(self, articles: Sequence[ArticleRecord]) -> list[ExtractionOutcome]:
        """Extract each requested article without aborting remaining selections."""
        outcomes = []
        for article in articles:
            try:
                response = self._extractor.extract(article.canonical_url)
            except AppError as error:
                self._database.record_extraction(
                    ExtractionInput(
                        article_id=article.id,
                        provider_name="anysearch",
                        source_url=article.canonical_url,
                        content=None,
                        status="failed",
                        error_code=error.code,
                        error_message=error.message,
                    )
                )
                outcomes.append(ExtractionOutcome(article.id, "failed", error.code))
                continue

            try:
                translation = (
                    self._translator.translate_text(
                        response.content, source_language=article.source_language
                    )
                    if self._translator is not None
                    else None
                )
            except AppError as error:
                self._database.record_extraction(
                    ExtractionInput(
                        article_id=article.id,
                        provider_name="anysearch",
                        source_url=response.source_url,
                        content=response.content,
                        status="translation_failed",
                        request_id=response.request_id,
                        error_code=error.code,
                        error_message=error.message,
                    )
                )
                outcomes.append(ExtractionOutcome(article.id, "translation_failed", error.code))
                continue

            self._database.record_extraction(
                ExtractionInput(
                    article_id=article.id,
                    provider_name="anysearch",
                    source_url=response.source_url,
                    content=response.content,
                    translated_content=translation.text if translation else None,
                    translation_provider_name=translation.provider_name if translation else None,
                    status="succeeded",
                    request_id=response.request_id,
                )
            )
            outcomes.append(ExtractionOutcome(article.id, "succeeded"))
        return outcomes


def _response_body(response: httpx.Response) -> tuple[str | None, Mapping[str, object]]:
    try:
        body = response.json()
    except ValueError as error:
        raise AppError("anysearch_invalid_response", "AnySearch returned invalid JSON") from error
    if not isinstance(body, Mapping):
        raise AppError("anysearch_invalid_response", "AnySearch response must be an object")
    request_id = body.get("request_id")
    return request_id if isinstance(request_id, str) else None, body


def _http_error(status_code: int, request_id: str | None, body: Mapping[str, object]) -> AppError:
    message = body.get("message")
    safe_message = message if isinstance(message, str) else f"AnySearch returned HTTP {status_code}"
    code_map = {
        401: "anysearch_authentication_failed",
        402: "anysearch_quota_exhausted",
        403: "anysearch_authorization_failed",
        429: "anysearch_rate_limited",
    }
    error = AppError(
        code_map.get(status_code, f"anysearch_http_{status_code}"),
        safe_message,
        retryable=status_code in {429, 500, 502, 503, 504},
    )
    if request_id:
        return AppError(error.code, f"{error.message} (request_id={request_id})", error.retryable)
    return error


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AppError("invalid_article_url", "article URL must use HTTP or HTTPS")
    normalized = SplitResult(
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""
    )
    return urlunsplit(normalized)
