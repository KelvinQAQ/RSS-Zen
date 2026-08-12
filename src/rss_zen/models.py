"""Typed configuration and domain models."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatabaseSettings(BaseModel):
    """SQLite storage location."""

    model_config = ConfigDict(extra="forbid")

    path: Path

    @field_validator("path")
    @classmethod
    def _path_must_not_be_empty(cls, value: Path) -> Path:
        if not str(value).strip() or str(value) == ".":
            raise ValueError("database path must not be empty")
        return value


class ServiceSettings(BaseModel):
    """Long-running service defaults and bounded retry policy."""

    model_config = ConfigDict(extra="forbid")

    default_poll_interval_minutes: int = Field(default=30, ge=1)
    translation_retry_interval_minutes: int = Field(default=5, ge=1)
    translation_max_attempts: int = Field(default=5, ge=1)
    retry_max_backoff_minutes: int = Field(default=360, ge=1)


class LimitsSettings(BaseModel):
    """Resource limits for untrusted feeds and provider requests."""

    model_config = ConfigDict(extra="forbid")

    max_feed_response_bytes: int = Field(default=10_000_000, ge=1)
    max_entries_per_feed: int = Field(default=500, ge=1)
    max_article_chars: int = Field(default=500_000, ge=1)
    max_translation_chars: int = Field(default=100_000, ge=1)
    max_extract_articles_per_run: int = Field(default=20, ge=1)
    max_translate_articles_per_run: int = Field(default=50, ge=1)


class BackupSettings(BaseModel):
    """Destination and bounded retention for verified SQLite snapshots."""

    model_config = ConfigDict(extra="forbid")

    directory: Path = Path("backups")
    retention_days: int = Field(default=30, ge=1)
    retention_count: int = Field(default=30, ge=1)


class TranslationProviderConfig(BaseModel):
    """One translation backend in priority order."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    kind: str
    endpoint: str = Field(default="")
    api_key_env: str | None = None
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    model: str | None = None
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "Optional reasoning effort sent to OpenAI-compatible providers "
            "(for example 'none', 'low', 'medium', 'high'); omitted when unset."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Optional per-request timeout override for this provider.",
    )
    max_chars: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional character limit per request for OpenAI-compatible providers; "
            "long text is split into chunks. Defaults to 4000 when unset."
        ),
    )

    _ENDPOINT_KINDS = {"libretranslate", "mymemory", "openai_compatible"}

    @field_validator("kind")
    @classmethod
    def _supported_kind(cls, value: str) -> str:
        supported = {"libretranslate", "mymemory", "openai_compatible"}
        if value not in supported:
            raise ValueError(f"unsupported translation provider kind: {value}")
        return value

    @field_validator("endpoint")
    @classmethod
    def _https_endpoint(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint must not include URL credentials")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _provider_requirements(self) -> TranslationProviderConfig:
        if self.kind in self._ENDPOINT_KINDS and not self.endpoint:
            raise ValueError(f"{self.kind} providers require an endpoint")
        if self.kind == "openai_compatible" and not self.model:
            raise ValueError("openai_compatible providers require a model")
        return self


class TranslationSettings(BaseModel):
    """Translation target and ordered provider chain."""

    model_config = ConfigDict(extra="forbid")

    target_language: str = "zh-CN"
    providers: list[TranslationProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _provider_names_are_unique(self) -> TranslationSettings:
        names = [provider.name.casefold() for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("translation provider names must be unique")
        return self


class AnySearchSettings(BaseModel):
    """Documented AnySearch `/v1/search` configuration."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://api.anysearch.com"
    api_key_env: str | None = None
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    tag: str = "general.general"
    zone: str | None = None
    language: str | None = None
    max_results: int = Field(default=3, ge=1, le=10)

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("AnySearch base_url must be HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("AnySearch base_url must not include URL credentials")
        return value.rstrip("/")

    @field_validator("tag")
    @classmethod
    def _valid_tag(cls, value: str) -> str:
        if "." not in value or value.startswith(".") or value.endswith("."):
            raise ValueError("AnySearch tag must use domain.sub_domain form")
        return value

    @field_validator("zone")
    @classmethod
    def _valid_zone(cls, value: str | None) -> str | None:
        if value is not None and value not in {"cn", "intl"}:
            raise ValueError("AnySearch zone must be cn or intl")
        return value


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_REQUEST_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "proxy-authorization",
    "proxy-connection",
}
_SECRET_REQUEST_HEADERS = {"authorization", "cookie"}


class FeedConfig(BaseModel):
    """One configured RSS or Atom feed."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str
    categories: list[str] = Field(default_factory=list)
    poll_interval_minutes: int | None = Field(default=None, ge=1)
    language: str | None = None
    enabled: bool = True
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional non-secret request headers (for example a custom User-Agent). "
            "Authorization and Cookie must be supplied through header_env."
        ),
    )
    header_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Sensitive request headers mapped to their required environment variable names, "
            "for example {Authorization = 'PRIVATE_FEED_TOKEN'}."
        ),
    )
    resolved_headers: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    fetcher: Literal["auto", "curl"] = Field(
        default="auto",
        description=(
            "'auto' uses the built-in HTTP client; 'curl' shells out to the system "
            "curl binary for sources whose anti-bot layer fingerprints Python's "
            "TLS client hello (for example Nitter RSS frontends)."
        ),
    )

    @field_validator("url")
    @classmethod
    def _https_feed_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("feed URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("feed URL must not include URL credentials")
        return value

    @field_validator("headers")
    @classmethod
    def _safe_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        _validate_headers(headers, allow_secret_headers=False)
        return headers

    @field_validator("header_env")
    @classmethod
    def _safe_header_environment(cls, header_env: dict[str, str]) -> dict[str, str]:
        _validate_headers({name: environment for name, environment in header_env.items()})
        if any(
            not _ENVIRONMENT_NAME_RE.fullmatch(environment) for environment in header_env.values()
        ):
            raise ValueError("header_env values must be valid environment variable names")
        return header_env

    @model_validator(mode="after")
    def _header_names_are_unique(self) -> FeedConfig:
        direct = {name.casefold() for name in self.headers}
        secret = {name.casefold() for name in self.header_env}
        if direct & secret:
            raise ValueError("headers and header_env must not define the same header")
        return self


class ExportProfile(BaseModel):
    """A named Markdown export definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    output_path: Path
    title: str = Field(min_length=1)
    fields: list[str] = Field(
        default_factory=lambda: ["source_name", "published_at", "url", "content"]
    )
    content_fallback: list[str] = Field(
        default_factory=lambda: ["full_text", "rss_content", "summary"]
    )
    filters: ExportFilters = Field(default_factory=lambda: ExportFilters())
    preprocess: list[PreprocessStep] = Field(default_factory=list)
    sort_by: Literal["published_at", "first_seen_at"] = "published_at"
    sort_descending: bool = True
    dedupe_by: Literal["none", "title"] = Field(
        default="none",
        description=(
            "When 'title', collapse articles whose normalized title is identical, "
            "keeping the highest-priority feed (see feed_priority)."
        ),
    )
    feed_priority: list[str] = Field(
        default_factory=list,
        description=(
            "Feed names ordered by preference for title-deduplication; feeds not "
            "listed rank after all listed feeds."
        ),
    )

    @field_validator("fields")
    @classmethod
    def _supported_fields(cls, values: list[str]) -> list[str]:
        supported = {
            "title",
            "summary",
            "content",
            "source_name",
            "published_at",
            "author",
            "categories",
            "url",
            "source_language",
            "extraction_status",
        }
        unknown = set(values) - supported
        if unknown:
            raise ValueError(f"unsupported export fields: {', '.join(sorted(unknown))}")
        return values

    @field_validator("content_fallback")
    @classmethod
    def _supported_content_fallback(cls, values: list[str]) -> list[str]:
        supported = {"full_text", "rss_content", "summary"}
        if not values or set(values) - supported:
            raise ValueError("content_fallback must use full_text, rss_content, or summary")
        return values


class ExportFilters(BaseModel):
    """Safe article selectors embedded in an export profile."""

    model_config = ConfigDict(extra="forbid")

    sources: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    published_after: str | None = None
    published_before: str | None = None
    translation_status: str = "succeeded"
    require_full_text: bool = False
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Relevance keywords matched against the article title and summary in "
            "both the original and translated text. Empty disables relevance "
            "filtering for those fields."
        ),
    )
    content_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Stricter relevance keywords matched against the article body (original "
            "and translated). Kept separate from keywords because broad terms such "
            "as country names match passing mentions in full text; body matching "
            "should use strong geographic terms only. Empty disables body matching."
        ),
    )
    keyword_match: Literal["any", "all", "groups"] = Field(
        default="any",
        description=(
            "How keyword tiers combine. 'any': a match in either tier keeps the "
            "article. 'all': every keyword in both tiers must match. 'groups': "
            "at least one keyword from each tier must match, combining a topic "
            "tier (keywords) with a region/body tier (content_keywords)."
        ),
    )
    include_untranslated: bool = Field(
        default=False,
        description=(
            "Also include articles whose translation is pending or failed, falling "
            "back to the original-language text for matching and rendering."
        ),
    )


class PreprocessStep(BaseModel):
    """A restricted, declarative transformation for one rendered field."""

    model_config = ConfigDict(extra="forbid")

    field: str
    operation: Literal["strip_html", "collapse_whitespace", "truncate", "replace", "date_format"]
    find: str | None = None
    replacement: str | None = None
    max_length: int | None = Field(default=None, ge=1)
    format: str | None = None

    @model_validator(mode="after")
    def _operation_arguments(self) -> PreprocessStep:
        if self.operation == "replace" and self.find is None:
            raise ValueError("replace preprocessing requires find")
        if self.operation == "truncate" and self.max_length is None:
            raise ValueError("truncate preprocessing requires max_length")
        if self.operation == "date_format" and self.format is None:
            raise ValueError("date_format preprocessing requires format")
        return self


def _validate_headers(headers: dict[str, str], *, allow_secret_headers: bool = True) -> None:
    if len(headers) > 20:
        raise ValueError("feeds may define at most 20 request headers")
    total_size = 0
    for name, value in headers.items():
        normalized = name.casefold()
        if len(name) > 64 or not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError("request header names must be valid HTTP tokens up to 64 characters")
        if normalized in _FORBIDDEN_REQUEST_HEADERS:
            raise ValueError(f"request header {name!r} is managed by the HTTP client")
        if not allow_secret_headers and normalized in _SECRET_REQUEST_HEADERS:
            raise ValueError(f"request header {name!r} must be configured through header_env")
        has_control_character = any(
            ord(character) < 32 or ord(character) == 127 for character in value
        )
        if len(value) > 2048 or has_control_character:
            raise ValueError("request header values must be printable and at most 2048 characters")
        total_size += len(name) + len(value)
    if total_size > 8192:
        raise ValueError("request headers must not exceed 8192 characters in total")


class AppConfig(BaseModel):
    """Complete application configuration after secret resolution."""

    model_config = ConfigDict(extra="forbid")

    database: DatabaseSettings
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    translation: TranslationSettings
    anysearch: AnySearchSettings = Field(default_factory=AnySearchSettings)
    feeds: list[FeedConfig] = Field(default_factory=list)
    exports: list[ExportProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _export_names_are_unique(self) -> AppConfig:
        names = [profile.name.casefold() for profile in self.exports]
        if len(names) != len(set(names)):
            raise ValueError("duplicate export profile names are not allowed")
        return self

    def effective_source_language(
        self, feed: FeedConfig, detected_language: str | None
    ) -> str | None:
        """Prefer an explicit feed language to automatic detection."""
        return feed.language or detected_language
