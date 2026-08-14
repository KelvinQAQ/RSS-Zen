"""SQLite schema migrations and typed repository operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rss_zen.backup import create_verified_sqlite_snapshot


@dataclass(frozen=True)
class FeedInput:
    """A feed declaration ready to be stored."""

    name: str
    url: str
    categories: tuple[str, ...] = ()
    language: str | None = None
    poll_interval_minutes: int | None = None
    enabled: bool = True
    origin: str = "config"


@dataclass(frozen=True)
class FeedRecord:
    """A persisted feed."""

    id: int
    name: str
    url: str
    categories: tuple[str, ...]
    language: str | None
    poll_interval_minutes: int | None
    enabled: bool
    origin: str
    etag: str | None
    last_modified: str | None
    last_checked_at: str | None
    last_success_at: str | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True)
class ArticleInput:
    """RSS/Atom fields normalized before persistence."""

    guid: str | None
    canonical_url: str
    title: str
    summary: str | None
    content: str | None
    author: str | None
    categories: tuple[str, ...]
    published_at: str | None
    source_updated_at: str | None = None
    detected_language: str | None = None
    source_language: str | None = None


@dataclass(frozen=True)
class ArticleRecord:
    """A persisted original RSS/Atom article."""

    id: int
    feed_id: int
    guid: str | None
    canonical_url: str
    title: str
    summary: str | None
    content: str | None
    author: str | None
    categories: tuple[str, ...]
    published_at: str | None
    source_updated_at: str | None
    detected_language: str | None
    source_language: str | None
    content_hash: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class ArticleReconciliation:
    """Result of creating or updating a source article."""

    article: ArticleRecord
    created: bool
    content_changed: bool


@dataclass(frozen=True)
class TranslationInput:
    """A translation attempt for one article and target language."""

    article_id: int
    target_language: str
    title: str | None
    summary: str | None
    content: str | None
    provider_name: str
    provider_model: str | None
    status: str
    source_hash: str
    error_code: str | None = None
    error_message: str | None = None
    attempt_count: int = 0
    next_retry_at: str | None = None
    last_attempt_at: str | None = None
    terminal: bool = False


@dataclass(frozen=True)
class TranslationRecord:
    """The latest translation state for one target language."""

    article_id: int
    target_language: str
    title: str | None
    summary: str | None
    content: str | None
    provider_name: str
    provider_model: str | None
    status: str
    source_hash: str
    error_code: str | None
    error_message: str | None
    attempt_count: int
    next_retry_at: str | None
    last_attempt_at: str | None
    terminal: bool


@dataclass(frozen=True)
class ExtractionInput:
    """One full-text extraction attempt."""

    article_id: int
    provider_name: str
    source_url: str
    content: str | None
    status: str
    translated_content: str | None = None
    translation_provider_name: str | None = None
    request_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ExtractionRecord:
    """A stored full-text extraction attempt."""

    id: int
    article_id: int
    provider_name: str
    source_url: str
    content: str | None
    translated_content: str | None
    translation_provider_name: str | None
    status: str
    request_id: str | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class ExportRunRecord:
    """One Markdown export invocation."""

    id: int
    profile_name: str
    output_path: Path
    article_count: int
    status: str


@dataclass(frozen=True)
class BatchRunRecord:
    """A materialized explicit translate or extract batch."""

    id: int
    command: str
    status: str
    selector: Mapping[str, object]
    limits: Mapping[str, object]


@dataclass(frozen=True)
class ExportArticleRecord:
    """Article data assembled for one Markdown export row."""

    article: ArticleRecord
    feed_name: str
    feed_url: str
    translation: TranslationRecord
    extraction: ExtractionRecord | None


@dataclass(frozen=True)
class ArticleOverview:
    """Article plus its latest translation/extraction status for the list command."""

    article: ArticleRecord
    feed_name: str
    translation_status: str | None
    translation_provider: str | None
    extraction_status: str | None


@dataclass(frozen=True)
class ProcessingCounts:
    """Local processing counts for the status command."""

    article_count: int
    pending_translation_count: int
    failed_translation_count: int
    terminal_translation_count: int
    failed_extraction_count: int


@dataclass(frozen=True)
class ProcessingErrorCount:
    """One persisted processing error code and its local occurrence count."""

    workflow: str
    error_code: str
    count: int


@dataclass(frozen=True)
class BatchHealthCounts:
    """Aggregate local manual-batch state for health reporting."""

    running: int
    interrupted: int
    resumable_items: int


@dataclass(frozen=True)
class EditionDeliveryHealth:
    """Aggregate edition and outbox state without content or target identifiers."""

    edition_active: int
    edition_queued: int
    edition_delivered: int
    edition_terminal: int
    edition_degraded: int
    delivery_pending: int
    delivery_sending: int
    delivery_retry_wait: int
    delivery_delivered: int
    delivery_terminal: int
    latest_delivered_at: str | None


@dataclass(frozen=True)
class UsageTotals:
    """Aggregate provider usage without content or credentials."""

    requests: int = 0
    source_chars: int = 0
    response_bytes: int = 0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0


@dataclass(frozen=True)
class RetentionCounts:
    """Candidate row counts from a non-destructive retention preview."""

    articles: int = 0
    failed_extractions: int = 0
    export_runs: int = 0
    batch_runs: int = 0


@dataclass(frozen=True)
class TopicProfileInput:
    """One immutable, versioned topic definition without credentials or article content."""

    key: str
    version: int
    name: str
    timezone: str
    delivery_deadline: str
    lookback_hours: int
    selection: Mapping[str, object]
    safety_limits: Mapping[str, object]
    enabled: bool = True


@dataclass(frozen=True)
class TopicProfileRecord:
    """A persisted immutable topic-profile version."""

    id: int
    key: str
    version: int
    name: str
    timezone: str
    delivery_deadline: str
    lookback_hours: int
    selection: Mapping[str, object]
    safety_limits: Mapping[str, object]
    enabled: bool


@dataclass(frozen=True)
class EditionRunRecord:
    """One idempotent topic edition for a local calendar date."""

    id: int
    topic_profile_id: int
    local_date: str
    deadline_at: str
    status: str
    candidate_count: int
    translated_count: int
    degraded_reason_code: str | None
    artifact_path: Path | None
    artifact_sha256: str | None


@dataclass(frozen=True)
class EditionItemInput:
    """One selected article and its deterministic content-provenance decision."""

    article_id: int
    content_source: str


@dataclass(frozen=True)
class EditionItemRecord:
    """One frozen edition candidate without duplicated article content."""

    edition_run_id: int
    article_id: int
    position: int
    content_source: str


@dataclass(frozen=True)
class DeliveryOutboxRecord:
    """One durable, idempotent delivery attempt without a message body or credentials."""

    id: int
    edition_run_id: int
    channel: str
    target_ref: str
    idempotency_key: str
    artifact_path: Path
    payload_sha256: str
    status: str
    attempt_count: int
    next_attempt_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    provider_message_id: str | None
    error_code: str | None
    delivered_at: str | None


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    categories_json TEXT NOT NULL DEFAULT '[]',
    language TEXT,
    poll_interval_minutes INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    author TEXT,
    categories_json TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    source_updated_at TEXT,
    detected_language TEXT,
    source_language TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(feed_id, canonical_url)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_feed_guid
ON articles(feed_id, guid) WHERE guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source_language ON articles(source_language);

CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    target_language TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    content TEXT,
    provider_name TEXT NOT NULL,
    provider_model TEXT,
    status TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(article_id, target_language)
);
CREATE INDEX IF NOT EXISTS idx_translations_status ON translations(status);

CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    provider_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content TEXT,
    status TEXT NOT NULL,
    request_id TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_article ON extractions(article_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_status ON extractions(status);

CREATE TABLE IF NOT EXISTS export_runs (
    id INTEGER PRIMARY KEY,
    profile_name TEXT NOT NULL,
    output_path TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    article_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    feed_count INTEGER NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT
);
"""

_MIGRATION_2 = """
ALTER TABLE extractions ADD COLUMN translated_content TEXT;
ALTER TABLE extractions ADD COLUMN translation_provider_name TEXT;
"""

_MIGRATION_3 = """
ALTER TABLE translations ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translations ADD COLUMN next_retry_at TEXT;
ALTER TABLE translations ADD COLUMN last_attempt_at TEXT;
ALTER TABLE translations ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_translations_retry
ON translations(status, terminal, next_retry_at);
"""

_MIGRATION_4 = """
CREATE TABLE batch_runs (
    id INTEGER PRIMARY KEY,
    command TEXT NOT NULL CHECK(command IN ('translate', 'extract')),
    selector_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'interrupted', 'succeeded', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE batch_run_items (
    batch_run_id INTEGER NOT NULL REFERENCES batch_runs(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed', 'skipped')),
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(batch_run_id, article_id),
    UNIQUE(batch_run_id, position)
);
CREATE INDEX idx_batch_run_items_pending
ON batch_run_items(batch_run_id, status, position);
"""

_MIGRATION_5 = """
CREATE TABLE topic_profiles (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    delivery_deadline TEXT NOT NULL,
    lookback_hours INTEGER NOT NULL CHECK(lookback_hours >= 1),
    selection_json TEXT NOT NULL,
    safety_limits_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(key, version)
);
CREATE INDEX idx_topic_profiles_latest ON topic_profiles(key, version DESC);

CREATE TABLE edition_runs (
    id INTEGER PRIMARY KEY,
    topic_profile_id INTEGER NOT NULL REFERENCES topic_profiles(id) ON DELETE RESTRICT,
    local_date TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'planned', 'refreshing', 'selecting', 'frozen', 'editorial', 'rendered', 'degraded',
        'queued', 'delivering', 'delivered', 'terminal'
    )),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    translated_count INTEGER NOT NULL DEFAULT 0 CHECK(translated_count >= 0),
    degraded_reason_code TEXT,
    artifact_path TEXT,
    artifact_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(topic_profile_id, local_date)
);
CREATE INDEX idx_edition_runs_status_deadline ON edition_runs(status, deadline_at);

CREATE TABLE delivery_outbox (
    id INTEGER PRIMARY KEY,
    edition_run_id INTEGER NOT NULL REFERENCES edition_runs(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL CHECK(channel IN ('feishu')),
    target_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    artifact_path TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'sending', 'retry_wait', 'delivered', 'terminal'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    provider_message_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(edition_run_id, channel)
);
CREATE INDEX idx_delivery_outbox_due
ON delivery_outbox(status, next_attempt_at, lease_expires_at, id);
"""

_MIGRATION_6 = """
CREATE TABLE edition_run_items (
    edition_run_id INTEGER NOT NULL REFERENCES edition_runs(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK(position >= 0),
    content_source TEXT NOT NULL CHECK(content_source IN (
        'extracted_full_text', 'rss_content', 'rss_summary',
        'original_rss_content', 'original_rss_summary'
    )),
    created_at TEXT NOT NULL,
    PRIMARY KEY(edition_run_id, article_id),
    UNIQUE(edition_run_id, position)
);
CREATE INDEX idx_edition_run_items_order
ON edition_run_items(edition_run_id, position);
"""

_MIGRATION_7 = """
CREATE TABLE usage_daily (
    local_date TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('feed', 'translation', 'pi', 'delivery')),
    provider TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0 CHECK(requests >= 0),
    source_chars INTEGER NOT NULL DEFAULT 0 CHECK(source_chars >= 0),
    response_bytes INTEGER NOT NULL DEFAULT 0 CHECK(response_bytes >= 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
    cost_microunits INTEGER NOT NULL DEFAULT 0 CHECK(cost_microunits >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(local_date, category, provider)
);
CREATE INDEX idx_usage_daily_category_date ON usage_daily(category, local_date);
"""

_MIGRATION_8 = """
CREATE TABLE edition_editorial_results (
    edition_run_id INTEGER PRIMARY KEY REFERENCES edition_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'fallback')),
    title TEXT,
    introduction TEXT,
    ordered_article_ids_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, _MIGRATION_3),
    (4, _MIGRATION_4),
    (5, _MIGRATION_5),
    (6, _MIGRATION_6),
    (7, _MIGRATION_7),
    (8, _MIGRATION_8),
)
_PRE_MIGRATION_BACKUP_RETENTION_COUNT = 10

_EDITION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "planned": frozenset({"refreshing"}),
    "refreshing": frozenset({"selecting", "degraded", "terminal"}),
    "selecting": frozenset({"frozen", "degraded", "terminal"}),
    "frozen": frozenset({"editorial", "rendered", "degraded", "terminal"}),
    "editorial": frozenset({"rendered", "degraded", "terminal"}),
    "rendered": frozenset({"queued"}),
    "degraded": frozenset({"queued"}),
    "queued": frozenset({"delivering", "terminal"}),
    "delivering": frozenset({"queued", "delivered", "terminal"}),
    "delivered": frozenset(),
    "terminal": frozenset(),
}
_TOPIC_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DEADLINE_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_METADATA_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "body",
    "content",
    "full-text",
    "full_text",
    "summary",
    "text",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "article-body",
    "article_body",
    "article-content",
    "article_content",
}
_MAX_TOPIC_METADATA_BYTES = 65_536
_MAX_TOPIC_METADATA_STRING_CHARS = 4_096


class Database:
    """Own and operate one RSS-Zen SQLite database file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection_timeout_seconds = 30.0
        self.busy_timeout_ms = 10_000

    def initialize(self) -> None:
        """Apply outstanding schema migrations with verified pre-migration snapshots."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            current_version = self._schema_version(connection)

        pending = [(version, sql) for version, sql in _MIGRATIONS if version > current_version]
        if pending and current_version > 0 and self.path.exists():
            self._backup_before_migration(current_version, pending[-1][0])

        for version, sql in pending:
            self._apply_migration(version, sql)

    def _apply_migration(self, version: int, sql: str) -> None:
        """Apply one migration and its version record in one SQLite transaction."""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _execute_sql_script(connection, sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _utc_now()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def schema_version(self) -> int:
        """Return the last applied schema migration version."""
        with self._connection() as connection:
            return self._schema_version(connection)

    def table_names(self) -> set[str]:
        """Return the application tables currently present."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def create_topic_profile(self, item: TopicProfileInput) -> TopicProfileRecord:
        """Persist one immutable topic version, returning an identical existing record."""
        _validate_topic_profile(item)
        selection_json = _json_object(item.selection, field="selection")
        safety_limits_json = _json_object(item.safety_limits, field="safety_limits")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO topic_profiles(
                    key, version, name, timezone, delivery_deadline, lookback_hours,
                    selection_json, safety_limits_json, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.key,
                    item.version,
                    item.name,
                    item.timezone,
                    item.delivery_deadline,
                    item.lookback_hours,
                    selection_json,
                    safety_limits_json,
                    int(item.enabled),
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM topic_profiles WHERE key = ? AND version = ?",
                (item.key, item.version),
            ).fetchone()
        if row is None:
            raise RuntimeError("topic profile insert did not return a record")
        record = _topic_profile_from_row(row)
        if (
            record.name != item.name
            or record.timezone != item.timezone
            or record.delivery_deadline != item.delivery_deadline
            or record.lookback_hours != item.lookback_hours
            or str(row["selection_json"]) != selection_json
            or str(row["safety_limits_json"]) != safety_limits_json
            or record.enabled != item.enabled
        ):
            raise ValueError("a different topic profile already uses this key and version")
        return record

    def latest_topic_profile(self, key: str) -> TopicProfileRecord | None:
        """Return the newest immutable version of one topic key."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM topic_profiles WHERE key = ? ORDER BY version DESC LIMIT 1",
                (key,),
            ).fetchone()
        return _topic_profile_from_row(row) if row is not None else None

    def create_edition_run(
        self, *, topic_profile_id: int, local_date: str, deadline_at: str
    ) -> EditionRunRecord:
        """Create one idempotent edition for a topic-profile version and local date."""
        if topic_profile_id < 1:
            raise ValueError("topic_profile_id must be positive")
        _validate_date(local_date, field="local_date")
        _validate_timestamp(deadline_at, field="deadline_at")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO edition_runs(
                    topic_profile_id, local_date, deadline_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'planned', ?, ?)
                """,
                (topic_profile_id, local_date, deadline_at, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM edition_runs
                WHERE topic_profile_id = ? AND local_date = ?
                """,
                (topic_profile_id, local_date),
            ).fetchone()
        if row is None:
            raise KeyError(topic_profile_id)
        record = _edition_run_from_row(row)
        if record.deadline_at != deadline_at:
            raise ValueError("edition already exists with a different deadline")
        return record

    def get_edition_run(self, run_id: int) -> EditionRunRecord:
        """Return one edition run or raise KeyError."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM edition_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _edition_run_from_row(row)

    def transition_edition_run(
        self,
        run_id: int,
        *,
        status: str,
        candidate_count: int | None = None,
        translated_count: int | None = None,
        degraded_reason_code: str | None = None,
        artifact_path: Path | None = None,
        artifact_sha256: str | None = None,
    ) -> EditionRunRecord:
        """Apply one validated edition lifecycle transition."""
        if status not in _EDITION_TRANSITIONS:
            raise ValueError("invalid edition status")
        if status in {"queued", "delivering", "delivered"}:
            raise ValueError("delivery edition states are managed by the outbox")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM edition_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = _edition_run_from_row(row)
            if status not in _EDITION_TRANSITIONS[current.status]:
                raise ValueError(f"invalid edition transition: {current.status} -> {status}")
            next_candidates = (
                current.candidate_count if candidate_count is None else candidate_count
            )
            next_translated = (
                current.translated_count if translated_count is None else translated_count
            )
            if next_candidates < 0 or next_translated < 0:
                raise ValueError("edition counts must be nonnegative")
            if next_translated > next_candidates:
                raise ValueError("translated_count cannot exceed candidate_count")
            next_reason = degraded_reason_code or current.degraded_reason_code
            next_path = artifact_path or current.artifact_path
            next_hash = artifact_sha256 or current.artifact_sha256
            if status == "degraded" and not next_reason:
                raise ValueError("degraded edition requires a safe reason code")
            if status in {"rendered", "degraded"}:
                if next_path is None or next_hash is None:
                    raise ValueError("rendered edition requires artifact path and SHA-256")
                _validate_sha256(next_hash, field="artifact_sha256")
            completed_at = _utc_now() if status in {"delivered", "terminal"} else None
            connection.execute(
                """
                UPDATE edition_runs SET
                    status = ?, candidate_count = ?, translated_count = ?,
                    degraded_reason_code = ?, artifact_path = ?, artifact_sha256 = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_candidates,
                    next_translated,
                    next_reason,
                    str(next_path) if next_path is not None else None,
                    next_hash,
                    _utc_now(),
                    completed_at,
                    run_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM edition_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("edition transition did not return a record")
        return _edition_run_from_row(updated)

    def editorial_result(self, run_id: int) -> Mapping[str, object] | None:
        """Return persisted bounded editorial metadata without article content."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM edition_editorial_results WHERE edition_run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        ordered = json.loads(row["ordered_article_ids_json"] or "[]")
        return {
            "status": str(row["status"]),
            "title": row["title"],
            "introduction": row["introduction"],
            "ordered_article_ids": tuple(int(value) for value in ordered),
            "error_code": row["error_code"],
        }

    def begin_editorial(self, run_id: int) -> Mapping[str, object] | None:
        """Persist running before invoking Pi; return existing result for crash recovery."""
        existing = self.editorial_result(run_id)
        if existing is not None:
            return existing
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM edition_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if str(row["status"]) != "frozen":
                raise ValueError("editorial can only begin for a frozen edition")
            connection.execute(
                """
                INSERT INTO edition_editorial_results(
                    edition_run_id, status, created_at, updated_at
                ) VALUES (?, 'running', ?, ?)
                """,
                (run_id, now, now),
            )
            connection.execute(
                "UPDATE edition_runs SET status='editorial', updated_at=? WHERE id=?",
                (now, run_id),
            )
        return None

    def finish_editorial(
        self,
        run_id: int,
        *,
        title: str | None,
        introduction: str | None,
        ordered_article_ids: tuple[int, ...],
        error_code: str | None,
    ) -> Mapping[str, object]:
        """Persist either a successful edit or deterministic fallback decision."""
        status = "fallback" if error_code else "succeeded"
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE edition_editorial_results SET status=?, title=?, introduction=?,
                ordered_article_ids_json=?, error_code=?, updated_at=?
                WHERE edition_run_id=? AND status='running'""",
                (
                    status,
                    title,
                    introduction,
                    json.dumps(ordered_article_ids),
                    error_code,
                    _utc_now(),
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("editorial result is not running")
        result = self.editorial_result(run_id)
        if result is None:
            raise RuntimeError("editorial result was not persisted")
        return result

    def freeze_edition_items(
        self, run_id: int, items: tuple[EditionItemInput, ...]
    ) -> tuple[EditionItemRecord, ...]:
        """Freeze an ordered candidate set and advance selecting to frozen atomically."""
        article_ids = tuple(item.article_id for item in items)
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("edition article IDs must be unique")
        allowed_sources = {
            "extracted_full_text",
            "rss_content",
            "rss_summary",
            "original_rss_content",
            "original_rss_summary",
        }
        if any(item.article_id < 1 for item in items):
            raise ValueError("edition article IDs must be positive")
        if any(item.content_source not in allowed_sources for item in items):
            raise ValueError("invalid edition content source")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM edition_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            existing = connection.execute(
                """
                SELECT * FROM edition_run_items
                WHERE edition_run_id = ? ORDER BY position
                """,
                (run_id,),
            ).fetchall()
            if existing:
                records = tuple(_edition_item_from_row(item) for item in existing)
                expected = tuple(
                    EditionItemRecord(run_id, item.article_id, position, item.content_source)
                    for position, item in enumerate(items)
                )
                if records != expected:
                    raise ValueError("edition candidates are already frozen differently")
                return records
            if str(row["status"]) != "selecting":
                raise ValueError("edition candidates can only freeze while selecting")
            connection.executemany(
                """
                INSERT INTO edition_run_items(
                    edition_run_id, article_id, position, content_source, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (run_id, item.article_id, position, item.content_source, now)
                    for position, item in enumerate(items)
                ],
            )
            connection.execute(
                """
                UPDATE edition_runs SET status = 'frozen', candidate_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (len(items), now, run_id),
            )
            rows = connection.execute(
                """
                SELECT * FROM edition_run_items
                WHERE edition_run_id = ? ORDER BY position
                """,
                (run_id,),
            ).fetchall()
        return tuple(_edition_item_from_row(row) for row in rows)

    def edition_items(self, run_id: int) -> tuple[EditionItemRecord, ...]:
        """Return frozen candidates in rendering order."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM edition_run_items
                WHERE edition_run_id = ? ORDER BY position
                """,
                (run_id,),
            ).fetchall()
        return tuple(_edition_item_from_row(row) for row in rows)

    def edition_item_article_ids(self, run_id: int) -> tuple[int, ...]:
        """Return frozen candidate article IDs in rendering order."""
        return tuple(item.article_id for item in self.edition_items(run_id))

    def create_delivery_outbox_item(
        self,
        *,
        edition_run_id: int,
        channel: str,
        target_ref: str,
        idempotency_key: str,
        artifact_path: Path,
        payload_sha256: str,
    ) -> DeliveryOutboxRecord:
        """Enqueue artifact delivery without storing its content or provider credentials."""
        if channel != "feishu":
            raise ValueError("unsupported delivery channel")
        if not target_ref or len(target_ref) > 255 or any(char.isspace() for char in target_ref):
            raise ValueError("target_ref must be a bounded opaque reference")
        if not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("idempotency_key must be non-empty and at most 255 characters")
        if not str(artifact_path):
            raise ValueError("artifact_path must be non-empty")
        _validate_sha256(payload_sha256, field="payload_sha256")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE idempotency_key = ? OR (edition_run_id = ? AND channel = ?)
                """,
                (idempotency_key, edition_run_id, channel),
            ).fetchone()
            if existing is not None:
                record = _delivery_outbox_from_row(existing)
                if record.idempotency_key != idempotency_key:
                    raise ValueError("edition already has a different channel delivery key")
                _require_same_delivery(
                    record,
                    edition_run_id=edition_run_id,
                    channel=channel,
                    target_ref=target_ref,
                    artifact_path=artifact_path,
                    payload_sha256=payload_sha256,
                )
                return record
            edition_row = connection.execute(
                "SELECT * FROM edition_runs WHERE id = ?", (edition_run_id,)
            ).fetchone()
            if edition_row is None:
                raise KeyError(edition_run_id)
            edition = _edition_run_from_row(edition_row)
            if edition.status not in {"rendered", "degraded"}:
                raise ValueError("delivery requires a rendered or degraded edition")
            if edition.artifact_path != artifact_path or edition.artifact_sha256 != payload_sha256:
                raise ValueError("delivery artifact must match the edition artifact")
            cursor = connection.execute(
                """
                INSERT INTO delivery_outbox(
                    edition_run_id, channel, target_ref, idempotency_key, artifact_path,
                    payload_sha256, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    edition_run_id,
                    channel,
                    target_ref,
                    idempotency_key,
                    str(artifact_path),
                    payload_sha256,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE edition_runs SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, edition_run_id),
            )
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery insert did not return a record")
        return _delivery_outbox_from_row(row)

    def delivery_for_edition(
        self, edition_run_id: int, *, channel: str
    ) -> DeliveryOutboxRecord | None:
        """Return the edition's channel delivery when already enqueued."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE edition_run_id = ? AND channel = ?
                """,
                (edition_run_id, channel),
            ).fetchone()
        return _delivery_outbox_from_row(row) if row is not None else None

    def get_delivery_outbox_item(self, item_id: int) -> DeliveryOutboxRecord:
        """Return one durable delivery item or raise KeyError."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return _delivery_outbox_from_row(row)

    def claim_due_deliveries(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
        limit: int,
    ) -> list[DeliveryOutboxRecord]:
        """Atomically claim due or expired-lease delivery items in stable order."""
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and bounded")
        now_dt = _validate_timestamp(now, field="now")
        lease_dt = _validate_timestamp(lease_expires_at, field="lease_expires_at")
        if lease_dt <= now_dt:
            raise ValueError("lease_expires_at must be later than now")
        if limit < 1 or limit > 100:
            raise ValueError("delivery claim limit must be between 1 and 100")
        claimed: list[DeliveryOutboxRecord] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT delivery_outbox.id, delivery_outbox.edition_run_id
                FROM delivery_outbox
                JOIN edition_runs ON edition_runs.id = delivery_outbox.edition_run_id
                WHERE edition_runs.status IN ('queued', 'delivering')
                  AND (
                    delivery_outbox.status = 'pending'
                    OR (delivery_outbox.status = 'retry_wait'
                        AND delivery_outbox.next_attempt_at <= ?)
                    OR (delivery_outbox.status = 'sending'
                        AND delivery_outbox.lease_expires_at <= ?)
                  )
                ORDER BY COALESCE(
                    delivery_outbox.next_attempt_at, delivery_outbox.created_at
                ), delivery_outbox.id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for row in rows:
                item_id = int(row["id"])
                edition_run_id = int(row["edition_run_id"])
                connection.execute(
                    """
                    UPDATE delivery_outbox SET
                        status = 'sending', attempt_count = attempt_count + 1,
                        lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (worker_id, lease_expires_at, now, item_id),
                )
                connection.execute(
                    """
                    UPDATE edition_runs SET status = 'delivering', updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'delivering')
                    """,
                    (now, edition_run_id),
                )
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)
                ).fetchone()
                if updated is not None:
                    claimed.append(_delivery_outbox_from_row(updated))
        return claimed

    def record_delivery_retry(
        self,
        item_id: int,
        *,
        worker_id: str,
        error_code: str,
        next_attempt_at: str,
    ) -> DeliveryOutboxRecord:
        """Release a held delivery lease into bounded retry state."""
        _validate_error_code(error_code)
        _validate_timestamp(next_attempt_at, field="next_attempt_at")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            held = connection.execute(
                """
                SELECT edition_run_id FROM delivery_outbox
                WHERE id = ? AND status = 'sending' AND lease_owner = ?
                """,
                (item_id, worker_id),
            ).fetchone()
            if held is None:
                raise ValueError("delivery retry requires the current worker lease")
            connection.execute(
                """
                UPDATE delivery_outbox SET
                    status = 'retry_wait', error_code = ?, next_attempt_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (error_code, next_attempt_at, now, item_id),
            )
            connection.execute(
                """
                UPDATE edition_runs SET status = 'queued', updated_at = ?
                WHERE id = ? AND status = 'delivering'
                """,
                (now, int(held["edition_run_id"])),
            )
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery retry did not return a record")
        return _delivery_outbox_from_row(row)

    def record_delivery_success(
        self, item_id: int, *, worker_id: str, provider_message_id: str
    ) -> DeliveryOutboxRecord:
        """Record confirmed provider delivery while holding the current lease."""
        if not provider_message_id or len(provider_message_id) > 255:
            raise ValueError("provider_message_id must be non-empty and bounded")
        delivered_at = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            held = connection.execute(
                """
                SELECT edition_run_id FROM delivery_outbox
                WHERE id = ? AND status = 'sending' AND lease_owner = ?
                """,
                (item_id, worker_id),
            ).fetchone()
            if held is None:
                raise ValueError("delivery success requires the current worker lease")
            connection.execute(
                """
                UPDATE delivery_outbox SET
                    status = 'delivered', provider_message_id = ?, error_code = NULL,
                    next_attempt_at = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    delivered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (provider_message_id, delivered_at, delivered_at, item_id),
            )
            connection.execute(
                """
                UPDATE edition_runs SET status = 'delivered', updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'delivering'
                """,
                (delivered_at, delivered_at, int(held["edition_run_id"])),
            )
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery success did not return a record")
        return _delivery_outbox_from_row(row)

    def record_delivery_terminal(
        self, item_id: int, *, worker_id: str, error_code: str
    ) -> DeliveryOutboxRecord:
        """Record a non-retryable delivery failure while holding the current lease."""
        _validate_error_code(error_code)
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            held = connection.execute(
                """
                SELECT edition_run_id FROM delivery_outbox
                WHERE id = ? AND status = 'sending' AND lease_owner = ?
                """,
                (item_id, worker_id),
            ).fetchone()
            if held is None:
                raise ValueError("delivery terminal result requires the current worker lease")
            connection.execute(
                """
                UPDATE delivery_outbox SET
                    status = 'terminal', error_code = ?, next_attempt_at = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (error_code, now, item_id),
            )
            connection.execute(
                """
                UPDATE edition_runs SET status = 'terminal', updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'delivering'
                """,
                (now, now, int(held["edition_run_id"])),
            )
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery terminal result did not return a record")
        return _delivery_outbox_from_row(row)

    def upsert_feed(self, item: FeedInput) -> FeedRecord:
        """Create or update a feed by its canonical URL."""
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO feeds (
                    name, url, categories_json, language, poll_interval_minutes, enabled,
                    origin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name = excluded.name,
                    categories_json = excluded.categories_json,
                    language = excluded.language,
                    poll_interval_minutes = excluded.poll_interval_minutes,
                    enabled = excluded.enabled,
                    origin = excluded.origin,
                    updated_at = excluded.updated_at
                """,
                (
                    item.name,
                    item.url,
                    _json_array(item.categories),
                    item.language,
                    item.poll_interval_minutes,
                    int(item.enabled),
                    item.origin,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM feeds WHERE url = ?", (item.url,)).fetchone()
        if row is None:
            raise RuntimeError("feed upsert did not return a record")
        return _feed_from_row(row)

    def get_feed_by_url(self, url: str) -> FeedRecord | None:
        """Look up a feed by URL."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
        return _feed_from_row(row) if row is not None else None

    def list_feeds(self) -> list[FeedRecord]:
        """List all feeds in deterministic URL order."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM feeds ORDER BY url").fetchall()
        return [_feed_from_row(row) for row in rows]

    def record_feed_success(
        self, feed_id: int, *, etag: str | None, last_modified: str | None
    ) -> None:
        """Record a successful fetch while preserving absent cache validators."""
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE feeds SET
                    etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified),
                    last_checked_at = ?,
                    last_success_at = ?,
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (etag, last_modified, now, now, now, feed_id),
            )

    def record_feed_failure(self, feed_id: int, *, error_code: str, error_message: str) -> None:
        """Record one feed-local failure without affecting other feeds."""
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE feeds SET
                    last_checked_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, error_code, error_message, now, feed_id),
            )

    def reconcile_article(self, feed_id: int, item: ArticleInput) -> ArticleReconciliation:
        """Create or update an article by GUID first, then canonical URL."""
        item_hash = _article_hash(item)
        now = _utc_now()
        with self._connection() as connection:
            existing = self._find_article(connection, feed_id, item)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO articles (
                        feed_id, guid, canonical_url, title, summary, content, author,
                        categories_json, published_at, source_updated_at, detected_language,
                        source_language, content_hash, first_seen_at, last_seen_at, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _article_values(feed_id, item, item_hash, now, now, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM articles WHERE id = last_insert_rowid()"
                ).fetchone()
                if row is None:
                    raise RuntimeError("article insert did not return a record")
                return ArticleReconciliation(
                    _article_from_row(row), created=True, content_changed=True
                )

            content_changed = existing["content_hash"] != item_hash
            connection.execute(
                """
                UPDATE articles SET
                    guid = ?, canonical_url = ?, title = ?, summary = ?, content = ?, author = ?,
                    categories_json = ?, published_at = ?, source_updated_at = ?,
                    detected_language = ?, source_language = ?, content_hash = ?, last_seen_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    item.guid,
                    item.canonical_url,
                    item.title,
                    item.summary,
                    item.content,
                    item.author,
                    _json_array(item.categories),
                    item.published_at,
                    item.source_updated_at,
                    item.detected_language,
                    item.source_language,
                    item_hash,
                    now,
                    now,
                    existing["id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM articles WHERE id = ?", (existing["id"],)
            ).fetchone()
        if row is None:
            raise RuntimeError("article update did not return a record")
        return ArticleReconciliation(
            _article_from_row(row), created=False, content_changed=content_changed
        )

    def get_article(self, article_id: int) -> ArticleRecord:
        """Return one article or raise KeyError."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if row is None:
            raise KeyError(article_id)
        return _article_from_row(row)

    def list_articles(
        self,
        *,
        article_ids: tuple[int, ...] = (),
        source: str | None = None,
        without_extraction: bool = False,
        published_after: str | None = None,
        published_before: str | None = None,
    ) -> list[ArticleRecord]:
        """List articles selected by safe, explicit repository filters."""
        conditions: list[str] = []
        parameters: list[object] = []
        if article_ids:
            placeholders = ", ".join("?" for _ in article_ids)
            conditions.append(f"articles.id IN ({placeholders})")
            parameters.extend(article_ids)
        if source is not None:
            conditions.append("(feeds.name = ? OR feeds.url = ?)")
            parameters.extend((source, source))
        if published_after is not None:
            conditions.append("articles.published_at >= ?")
            parameters.append(published_after)
        if published_before is not None:
            conditions.append("articles.published_at <= ?")
            parameters.append(published_before)
        if without_extraction:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM extractions WHERE extractions.article_id = articles.id "
                "AND extractions.status = 'succeeded')"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            "SELECT articles.* FROM articles "
            "JOIN feeds ON feeds.id = articles.feed_id "
            f"{where} ORDER BY articles.published_at DESC, articles.id DESC"
        )
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_article_from_row(row) for row in rows]

    def list_articles_by_translation_status(
        self, target_language: str, *, status: str, limit: int | None = None
    ) -> list[ArticleRecord]:
        """List distinct articles whose latest persisted translation has the given status."""
        limit_clause = " LIMIT ?" if limit else ""
        parameters: list[object] = [target_language, status]
        if limit:
            parameters.append(limit)
        query = f"""
            SELECT articles.*
            FROM translations
            JOIN articles ON articles.id = translations.article_id
            WHERE translations.target_language = ?
              AND translations.status = ?
            GROUP BY articles.id
            ORDER BY MAX(translations.id) DESC
            {limit_clause}
        """
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_article_from_row(row) for row in rows]

    def list_articles_overview(
        self,
        *,
        target_language: str,
        source: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        translation_status: str | None = None,
        limit: int | None = None,
    ) -> list[ArticleOverview]:
        """Return articles with their latest translation/extraction status."""
        conditions: list[str] = []
        parameters: list[object] = []
        if source is not None:
            conditions.append("(feeds.name = ? OR feeds.url = ?)")
            parameters.extend((source, source))
        if published_after is not None:
            conditions.append("articles.published_at >= ?")
            parameters.append(published_after)
        if published_before is not None:
            conditions.append("articles.published_at <= ?")
            parameters.append(published_before)
        if translation_status is not None:
            conditions.append("COALESCE(translations.status, '') = ?")
            parameters.append(translation_status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = " LIMIT ?" if limit else ""
        if limit:
            parameters.append(limit)
        query = f"""
            SELECT
                articles.*,
                feeds.name AS overview_feed_name,
                translations.status AS overview_translation_status,
                translations.provider_name AS overview_translation_provider,
                (
                    SELECT latest.status FROM extractions AS latest
                    WHERE latest.article_id = articles.id
                    ORDER BY latest.id DESC LIMIT 1
                ) AS overview_extraction_status
            FROM articles
            JOIN feeds ON feeds.id = articles.feed_id
            LEFT JOIN translations ON translations.article_id = articles.id
                AND translations.target_language = ?
            {where}
            ORDER BY articles.published_at DESC, articles.id DESC
            {limit_clause}
        """
        with self._connection() as connection:
            rows = connection.execute(query, [target_language, *parameters]).fetchall()
        return [
            ArticleOverview(
                article=_article_from_row(row),
                feed_name=str(row["overview_feed_name"]),
                translation_status=row["overview_translation_status"],
                translation_provider=row["overview_translation_provider"],
                extraction_status=row["overview_extraction_status"],
            )
            for row in rows
        ]

    def update_article_languages(
        self, article_id: int, *, detected_language: str | None, source_language: str | None
    ) -> ArticleRecord:
        """Persist language detection and any configured source-language override."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE articles SET detected_language = ?, source_language = ?, updated_at = ?
                WHERE id = ?
                """,
                (detected_language, source_language, _utc_now(), article_id),
            )
            row = connection.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if row is None:
            raise KeyError(article_id)
        return _article_from_row(row)

    def save_translation(self, item: TranslationInput) -> TranslationRecord:
        """Save the current translation state without touching original article fields."""
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO translations (
                    article_id, target_language, title, summary, content, provider_name,
                    provider_model, status, source_hash, error_code, error_message,
                    attempt_count, next_retry_at, last_attempt_at, terminal, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id, target_language) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    content = excluded.content,
                    provider_name = excluded.provider_name,
                    provider_model = excluded.provider_model,
                    status = excluded.status,
                    source_hash = excluded.source_hash,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    attempt_count = excluded.attempt_count,
                    next_retry_at = excluded.next_retry_at,
                    last_attempt_at = excluded.last_attempt_at,
                    terminal = excluded.terminal,
                    updated_at = excluded.updated_at
                """,
                (
                    item.article_id,
                    item.target_language,
                    item.title,
                    item.summary,
                    item.content,
                    item.provider_name,
                    item.provider_model,
                    item.status,
                    item.source_hash,
                    item.error_code,
                    item.error_message,
                    item.attempt_count,
                    item.next_retry_at,
                    item.last_attempt_at,
                    int(item.terminal),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM translations
                WHERE article_id = ? AND target_language = ?
                """,
                (item.article_id, item.target_language),
            ).fetchone()
        if row is None:
            raise RuntimeError("translation upsert did not return a record")
        return _translation_from_row(row)

    def begin_translation(
        self,
        article: ArticleRecord,
        *,
        target_language: str,
        provider_name: str,
        provider_model: str | None,
    ) -> TranslationRecord:
        """Persist a pending translation before making an external provider request."""
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO translations (
                    article_id, target_language, title, summary, content, provider_name,
                    provider_model, status, source_hash, error_code, error_message,
                    attempt_count, next_retry_at, last_attempt_at, terminal, created_at, updated_at
                ) VALUES (
                    ?, ?, NULL, NULL, NULL, ?, ?, 'pending', ?, NULL, NULL, 0, ?, NULL, 0, ?, ?
                )
                ON CONFLICT(article_id, target_language) DO UPDATE SET
                    title = NULL,
                    summary = NULL,
                    content = NULL,
                    provider_name = excluded.provider_name,
                    provider_model = excluded.provider_model,
                    status = 'pending',
                    source_hash = excluded.source_hash,
                    error_code = NULL,
                    error_message = NULL,
                    attempt_count = CASE
                        WHEN translations.source_hash = excluded.source_hash
                        THEN translations.attempt_count
                        ELSE 0
                    END,
                    next_retry_at = excluded.next_retry_at,
                    last_attempt_at = NULL,
                    terminal = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    article.id,
                    target_language,
                    provider_name,
                    provider_model,
                    article.content_hash,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM translations WHERE article_id = ? AND target_language = ?",
                (article.id, target_language),
            ).fetchone()
        if row is None:
            raise RuntimeError("translation pending insert did not return a record")
        return _translation_from_row(row)

    def list_due_translations(
        self, target_language: str, *, now: str | None = None, limit: int = 100
    ) -> list[tuple[ArticleRecord, TranslationRecord]]:
        """Return pending or retryable translations that are due for processing."""
        due_at = now or _utc_now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT articles.*, translations.*
                FROM translations
                JOIN articles ON articles.id = translations.article_id
                WHERE translations.target_language = ?
                  AND translations.status IN ('pending', 'failed')
                  AND translations.terminal = 0
                  AND (translations.next_retry_at IS NULL OR translations.next_retry_at <= ?)
                ORDER BY COALESCE(translations.next_retry_at, '') ASC, translations.article_id ASC
                LIMIT ?
                """,
                (target_language, due_at, limit),
            ).fetchall()
        return [(_article_from_row(row), _translation_from_row(row)) for row in rows]

    def latest_translation(self, article_id: int, target_language: str) -> TranslationRecord | None:
        """Retrieve the current translation for one target language."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM translations
                WHERE article_id = ? AND target_language = ?
                """,
                (article_id, target_language),
            ).fetchone()
        return _translation_from_row(row) if row is not None else None

    def record_extraction(self, item: ExtractionInput) -> ExtractionRecord:
        """Append a full-text extraction attempt."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO extractions (
                    article_id, provider_name, source_url, content, translated_content,
                    translation_provider_name, status, request_id, error_code, error_message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.article_id,
                    item.provider_name,
                    item.source_url,
                    item.content,
                    item.translated_content,
                    item.translation_provider_name,
                    item.status,
                    item.request_id,
                    item.error_code,
                    item.error_message,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM extractions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        if row is None:
            raise RuntimeError("extraction insert did not return a record")
        return _extraction_from_row(row)

    def latest_extraction(self, article_id: int) -> ExtractionRecord | None:
        """Return the most recent extraction attempt for an article."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM extractions WHERE article_id = ? ORDER BY id DESC LIMIT 1",
                (article_id,),
            ).fetchone()
        return _extraction_from_row(row) if row is not None else None

    def list_export_articles(
        self,
        *,
        target_language: str,
        translation_status: str | None = "succeeded",
        sources: tuple[str, ...] = (),
        categories: tuple[str, ...] = (),
        published_after: str | None = None,
        published_before: str | None = None,
        require_full_text: bool = False,
        include_untranslated: bool = False,
        sort_by: str = "published_at",
        sort_descending: bool = True,
    ) -> list[ExportArticleRecord]:
        """Join article, translation, feed, and latest extraction for Markdown rendering."""
        order_column = {
            "published_at": "articles.published_at",
            "first_seen_at": "articles.first_seen_at",
        }.get(sort_by)
        if order_column is None:
            raise ValueError("unsupported export sort column")
        include_untranslated = include_untranslated or translation_status is None
        conditions: list[str] = []
        parameters: list[object] = []
        if not include_untranslated:
            conditions.append("translations.status = ?")
            parameters.append(translation_status)
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            conditions.append(f"(feeds.name IN ({placeholders}) OR feeds.url IN ({placeholders}))")
            parameters.extend(sources)
            parameters.extend(sources)
        if published_after:
            conditions.append("articles.published_at >= ?")
            parameters.append(published_after)
        if published_before:
            conditions.append("articles.published_at <= ?")
            parameters.append(published_before)
        if require_full_text:
            conditions.append("extractions.id IS NOT NULL")
            conditions.append("extractions.translated_content IS NOT NULL")
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        direction = "DESC" if sort_descending else "ASC"
        join_kind = "LEFT JOIN" if include_untranslated else "JOIN"
        query = f"""
            SELECT
                articles.*,
                feeds.name AS export_feed_name,
                feeds.url AS export_feed_url,
                translations.target_language AS export_translation_target_language,
                translations.title AS export_translation_title,
                translations.summary AS export_translation_summary,
                translations.content AS export_translation_content,
                translations.provider_name AS export_translation_provider_name,
                translations.provider_model AS export_translation_provider_model,
                translations.status AS export_translation_status,
                translations.source_hash AS export_translation_source_hash,
                translations.error_code AS export_translation_error_code,
                translations.error_message AS export_translation_error_message,
                translations.attempt_count AS export_translation_attempt_count,
                translations.next_retry_at AS export_translation_next_retry_at,
                translations.last_attempt_at AS export_translation_last_attempt_at,
                translations.terminal AS export_translation_terminal,
                extractions.id AS extraction_id,
                extractions.provider_name AS export_extraction_provider_name,
                extractions.source_url AS export_extraction_source_url,
                extractions.content AS export_extraction_content,
                extractions.translated_content AS export_extraction_translated_content,
                extractions.translation_provider_name AS export_extraction_translator,
                extractions.status AS export_extraction_status,
                extractions.request_id AS export_extraction_request_id,
                extractions.error_code AS export_extraction_error_code,
                extractions.error_message AS export_extraction_error_message
            FROM articles
            JOIN feeds ON feeds.id = articles.feed_id
            {join_kind} translations ON translations.article_id = articles.id
                AND translations.target_language = ?
            LEFT JOIN extractions ON extractions.id = (
                SELECT latest.id FROM extractions AS latest
                WHERE latest.article_id = articles.id
                ORDER BY latest.id DESC LIMIT 1
            )
            {where_sql}
            ORDER BY {order_column} {direction}, articles.id {direction}
        """
        parameters.insert(0, target_language)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        records = [_export_article_from_row(row) for row in rows]
        if not categories:
            return records
        category_set = {category.casefold() for category in categories}
        return [
            record
            for record in records
            if category_set.intersection(
                category.casefold() for category in record.article.categories
            )
        ]

    def processing_counts(self, target_language: str) -> ProcessingCounts:
        """Return bounded local status counters without contacting providers."""
        with self._connection() as connection:
            article_count = int(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            pending_translation_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM articles
                    LEFT JOIN translations ON translations.article_id = articles.id
                        AND translations.target_language = ?
                    WHERE translations.id IS NULL
                        OR translations.status != 'succeeded'
                        OR translations.source_hash != articles.content_hash
                    """,
                    (target_language,),
                ).fetchone()[0]
            )
            failed_translation_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM translations
                    WHERE target_language = ? AND status = 'failed'
                    """,
                    (target_language,),
                ).fetchone()[0]
            )
            terminal_translation_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM translations
                    WHERE target_language = ? AND status = 'failed' AND terminal = 1
                    """,
                    (target_language,),
                ).fetchone()[0]
            )
            failed_extraction_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM extractions "
                    "WHERE status IN ('failed', 'translation_failed')"
                ).fetchone()[0]
            )
        return ProcessingCounts(
            article_count=article_count,
            pending_translation_count=pending_translation_count,
            failed_translation_count=failed_translation_count,
            terminal_translation_count=terminal_translation_count,
            failed_extraction_count=failed_extraction_count,
        )

    def retention_counts(
        self,
        *,
        articles_before: str | None = None,
        failed_extractions_before: str | None = None,
        export_runs_before: str | None = None,
        batch_runs_before: str | None = None,
    ) -> RetentionCounts:
        """Preview independently configured retention candidates without mutation."""
        with self._connection() as connection:
            articles = _count_before(connection, "articles", "last_seen_at", articles_before)
            failed_extractions = _count_before(
                connection,
                "extractions",
                "created_at",
                failed_extractions_before,
                "status IN ('failed', 'translation_failed')",
            )
            export_runs = _count_before(connection, "export_runs", "created_at", export_runs_before)
            batch_runs = _count_before(
                connection,
                "batch_runs",
                "completed_at",
                batch_runs_before,
                "status IN ('succeeded', 'failed')",
            )
        return RetentionCounts(articles, failed_extractions, export_runs, batch_runs)

    def checkpoint_wal(self) -> tuple[int, int, int]:
        """Run an explicit truncating WAL checkpoint and return SQLite's counters."""
        with self._connection() as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            raise RuntimeError("wal checkpoint returned no result")
        return tuple(int(value) for value in row)

    def apply_retention(
        self,
        *,
        articles_before: str | None = None,
        failed_extractions_before: str | None = None,
        export_runs_before: str | None = None,
        batch_runs_before: str | None = None,
    ) -> RetentionCounts:
        """Delete configured retention candidates in one database transaction."""
        with self._connection() as connection:
            articles = _delete_before(connection, "articles", "last_seen_at", articles_before)
            failed_extractions = _delete_before(
                connection,
                "extractions",
                "created_at",
                failed_extractions_before,
                "status IN ('failed', 'translation_failed')",
            )
            export_runs = _delete_before(
                connection, "export_runs", "created_at", export_runs_before
            )
            batch_runs = _delete_before(
                connection,
                "batch_runs",
                "completed_at",
                batch_runs_before,
                "status IN ('succeeded', 'failed')",
            )
        return RetentionCounts(articles, failed_extractions, export_runs, batch_runs)

    def batch_health_counts(self) -> BatchHealthCounts:
        """Return local checkpoint counts without selecting article content."""
        with self._connection() as connection:
            running = int(
                connection.execute(
                    "SELECT COUNT(*) FROM batch_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            interrupted = int(
                connection.execute(
                    "SELECT COUNT(*) FROM batch_runs WHERE status = 'interrupted'"
                ).fetchone()[0]
            )
            resumable_items = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM batch_run_items
                    WHERE status IN ('pending', 'failed')
                       OR (status = 'skipped' AND error_code = 'provider_budget_exhausted')
                    """
                ).fetchone()[0]
            )
        return BatchHealthCounts(running, interrupted, resumable_items)

    def reserve_usage(
        self,
        *,
        local_date: str,
        category: str,
        provider: str,
        requests: int = 0,
        source_chars: int = 0,
        max_requests: int | None = None,
        max_source_chars: int | None = None,
    ) -> UsageTotals:
        """Atomically reserve bounded provider usage across processes and restarts."""
        _validate_usage_dimensions(local_date, category, provider)
        if requests < 0 or source_chars < 0 or requests + source_chars < 1:
            raise ValueError("usage reservation must be nonnegative and non-empty")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM usage_daily WHERE local_date=? AND category=? AND provider=?",
                (local_date, category, provider),
            ).fetchone()
            current_requests = int(row["requests"]) if row else 0
            current_chars = int(row["source_chars"]) if row else 0
            if max_requests is not None and current_requests + requests > max_requests:
                raise ValueError("daily provider request budget is exhausted")
            if max_source_chars is not None and current_chars + source_chars > max_source_chars:
                raise ValueError("daily provider source-character budget is exhausted")
            connection.execute(
                """
                INSERT INTO usage_daily(
                    local_date, category, provider, requests, source_chars, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_date, category, provider) DO UPDATE SET
                    requests=requests+excluded.requests,
                    source_chars=source_chars+excluded.source_chars,
                    updated_at=excluded.updated_at
                """,
                (local_date, category, provider, requests, source_chars, _utc_now()),
            )
        return self.usage_totals(local_date=local_date, category=category, provider=provider)

    def record_usage(
        self,
        *,
        local_date: str,
        category: str,
        provider: str,
        response_bytes: int = 0,
        attempts: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> UsageTotals:
        """Add non-budget usage counters after local or external work."""
        _validate_usage_dimensions(local_date, category, provider)
        counters = (response_bytes, attempts, input_tokens, output_tokens)
        if any(value < 0 for value in counters) or sum(counters) < 1:
            raise ValueError("usage counters must be nonnegative and non-empty")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO usage_daily(
                    local_date, category, provider, response_bytes, attempts,
                    input_tokens, output_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_date, category, provider) DO UPDATE SET
                    response_bytes=response_bytes+excluded.response_bytes,
                    attempts=attempts+excluded.attempts,
                    input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    updated_at=excluded.updated_at
                """,
                (
                    local_date,
                    category,
                    provider,
                    response_bytes,
                    attempts,
                    input_tokens,
                    output_tokens,
                    _utc_now(),
                ),
            )
        return self.usage_totals(local_date=local_date, category=category, provider=provider)

    def usage_totals(
        self, *, local_date: str, category: str | None = None, provider: str | None = None
    ) -> UsageTotals:
        """Aggregate one local reporting day with optional category/provider filters."""
        _validate_date(local_date, field="local_date")
        clauses = ["local_date = ?"]
        values: list[object] = [local_date]
        if category is not None:
            _validate_usage_dimensions(local_date, category, provider or "all")
            clauses.append("category = ?")
            values.append(category)
        if provider is not None:
            clauses.append("provider = ?")
            values.append(provider)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COALESCE(SUM(requests),0) requests,
                       COALESCE(SUM(source_chars),0) source_chars,
                       COALESCE(SUM(response_bytes),0) response_bytes,
                       COALESCE(SUM(attempts),0) attempts,
                       COALESCE(SUM(input_tokens),0) input_tokens,
                       COALESCE(SUM(output_tokens),0) output_tokens,
                       COALESCE(SUM(cost_microunits),0) cost_microunits
                FROM usage_daily WHERE {" AND ".join(clauses)}
                """,
                values,
            ).fetchone()
        return UsageTotals(*(int(value) for value in row))

    def usage_totals_period(self, *, start_date: str, end_date: str) -> UsageTotals:
        """Aggregate usage over a half-open local-date range."""
        start = _validate_date(start_date, field="start_date")
        end = _validate_date(end_date, field="end_date")
        if end <= start:
            raise ValueError("usage period end_date must be after start_date")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(requests),0), COALESCE(SUM(source_chars),0),
                       COALESCE(SUM(response_bytes),0), COALESCE(SUM(attempts),0),
                       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(cost_microunits),0)
                FROM usage_daily WHERE local_date >= ? AND local_date < ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return UsageTotals(*(int(value) for value in row))

    def edition_delivery_health(self) -> EditionDeliveryHealth:
        """Return local edition/outbox counts without article content or delivery targets."""
        with self._connection() as connection:
            editions = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN (
                        'planned', 'refreshing', 'selecting', 'frozen', 'editorial',
                        'rendered', 'degraded'
                    ) THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status IN ('queued', 'delivering') THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                    SUM(CASE WHEN status = 'terminal' THEN 1 ELSE 0 END) AS terminal,
                    SUM(CASE WHEN degraded_reason_code IS NOT NULL THEN 1 ELSE 0 END) AS degraded
                FROM edition_runs
                """
            ).fetchone()
            deliveries = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'sending' THEN 1 ELSE 0 END) AS sending,
                    SUM(CASE WHEN status = 'retry_wait' THEN 1 ELSE 0 END) AS retry_wait,
                    SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                    SUM(CASE WHEN status = 'terminal' THEN 1 ELSE 0 END) AS terminal,
                    MAX(delivered_at) AS latest_delivered_at
                FROM delivery_outbox
                """
            ).fetchone()
        if editions is None or deliveries is None:
            raise RuntimeError("edition delivery health query returned no row")
        return EditionDeliveryHealth(
            edition_active=int(editions["active"] or 0),
            edition_queued=int(editions["queued"] or 0),
            edition_delivered=int(editions["delivered"] or 0),
            edition_terminal=int(editions["terminal"] or 0),
            edition_degraded=int(editions["degraded"] or 0),
            delivery_pending=int(deliveries["pending"] or 0),
            delivery_sending=int(deliveries["sending"] or 0),
            delivery_retry_wait=int(deliveries["retry_wait"] or 0),
            delivery_delivered=int(deliveries["delivered"] or 0),
            delivery_terminal=int(deliveries["terminal"] or 0),
            latest_delivered_at=deliveries["latest_delivered_at"],
        )

    def processing_error_counts(self, target_language: str) -> list[ProcessingErrorCount]:
        """Aggregate stored failures without exposing provider response bodies."""
        with self._connection() as connection:
            translation_rows = connection.execute(
                """
                SELECT error_code, COUNT(*) AS count
                FROM translations
                WHERE target_language = ? AND error_code IS NOT NULL
                GROUP BY error_code
                ORDER BY count DESC, error_code
                """,
                (target_language,),
            ).fetchall()
            extraction_rows = connection.execute(
                """
                SELECT error_code, COUNT(*) AS count
                FROM extractions
                WHERE error_code IS NOT NULL
                GROUP BY error_code
                ORDER BY count DESC, error_code
                """
            ).fetchall()
        return [
            *[
                ProcessingErrorCount("translation", str(row["error_code"]), int(row["count"]))
                for row in translation_rows
            ],
            *[
                ProcessingErrorCount("extraction", str(row["error_code"]), int(row["count"]))
                for row in extraction_rows
            ],
        ]

    def create_batch_run(
        self,
        *,
        command: str,
        article_ids: tuple[int, ...],
        selector: Mapping[str, object],
        limits: Mapping[str, object],
    ) -> BatchRunRecord:
        """Persist an ordered, immutable selection before external batch work begins."""
        if command not in {"translate", "extract"}:
            raise ValueError("unsupported batch command")
        if not article_ids or len(article_ids) != len(set(article_ids)):
            raise ValueError("batch article IDs must be non-empty and unique")
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO batch_runs(
                    command, selector_json, limits_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (
                    command,
                    json.dumps(dict(selector), sort_keys=True),
                    json.dumps(dict(limits), sort_keys=True),
                    now,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO batch_run_items(batch_run_id, article_id, position, status, updated_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                [
                    (run_id, article_id, position, now)
                    for position, article_id in enumerate(article_ids)
                ],
            )
        return BatchRunRecord(run_id, command, "running", dict(selector), dict(limits))

    def get_batch_run(self, run_id: int) -> BatchRunRecord:
        """Return one batch run or raise KeyError when it no longer exists."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM batch_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _batch_run_from_row(row)

    def require_batch_run_command(self, run_id: int, command: str) -> BatchRunRecord:
        """Return a run only when it belongs to the command requesting resume."""
        run = self.get_batch_run(run_id)
        if run.command != command:
            raise ValueError(f"batch run {run_id} command is {run.command}, not {command}")
        return run

    def batch_run_pending_article_ids(self, run_id: int) -> tuple[int, ...]:
        """Return pending items in their original materialized order."""
        return self._batch_run_article_ids(run_id, statuses=("pending",))

    def batch_run_resumable_article_ids(self, run_id: int) -> tuple[int, ...]:
        """Return unfinished items eligible for explicit operator resume."""
        return self._batch_run_article_ids(
            run_id, statuses=("pending", "failed", "skipped"), resumable_skips_only=True
        )

    def _batch_run_article_ids(
        self,
        run_id: int,
        *,
        statuses: tuple[str, ...],
        resumable_skips_only: bool = False,
    ) -> tuple[int, ...]:
        placeholders = ", ".join("?" for _ in statuses)
        skipped_clause = (
            "AND (status != 'skipped' OR error_code = 'provider_budget_exhausted')"
            if resumable_skips_only
            else ""
        )
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT article_id FROM batch_run_items
                WHERE batch_run_id = ? AND status IN ({placeholders}) {skipped_clause}
                ORDER BY position
                """,
                (run_id, *statuses),
            ).fetchall()
        return tuple(int(row["article_id"]) for row in rows)

    def complete_batch_run_item(
        self, run_id: int, article_id: int, *, status: str, error_code: str | None = None
    ) -> None:
        """Record one terminal or interrupted item state after external work completes."""
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("invalid batch item status")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE batch_run_items SET status = ?, error_code = ?, updated_at = ?
                WHERE batch_run_id = ? AND article_id = ?
                """,
                (status, error_code, _utc_now(), run_id, article_id),
            )
        if cursor.rowcount != 1:
            raise KeyError((run_id, article_id))

    def update_batch_run_status(self, run_id: int, *, status: str) -> None:
        """Set the aggregate batch state after its item transitions are persisted."""
        if status not in {"running", "interrupted", "succeeded", "failed"}:
            raise ValueError("invalid batch run status")
        completed_at = _utc_now() if status in {"succeeded", "failed"} else None
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE batch_runs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?
                """,
                (status, _utc_now(), completed_at, run_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(run_id)

    def record_export_run(
        self,
        *,
        profile_name: str,
        output_path: Path,
        filters: Mapping[str, object],
        article_count: int,
        status: str,
    ) -> ExportRunRecord:
        """Append an auditable Markdown export outcome."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO export_runs (
                    profile_name, output_path, filters_json, article_count, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_name,
                    str(output_path),
                    json.dumps(dict(filters), ensure_ascii=False, sort_keys=True),
                    article_count,
                    status,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM export_runs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        if row is None:
            raise RuntimeError("export run insert did not return a record")
        return _export_run_from_row(row)

    def _find_article(
        self, connection: sqlite3.Connection, feed_id: int, item: ArticleInput
    ) -> sqlite3.Row | None:
        if item.guid is not None:
            row = connection.execute(
                "SELECT * FROM articles WHERE feed_id = ? AND guid = ?", (feed_id, item.guid)
            ).fetchone()
            if row is not None:
                return row
        return connection.execute(
            "SELECT * FROM articles WHERE feed_id = ? AND canonical_url = ?",
            (feed_id, item.canonical_url),
        ).fetchone()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self.connection_timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    def _backup_before_migration(self, from_version: int, to_version: int) -> Path:
        """Snapshot committed main-database and WAL state before schema changes."""
        backup_directory = self.path.parent / "backups" / "pre-migration"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_directory / (
            f"rss-zen-v{from_version}-to-v{to_version}-{timestamp}.sqlite3"
        )
        create_verified_sqlite_snapshot(self.path, backup_path)
        backups = sorted(backup_directory.glob("rss-zen-v*-to-v*-*.sqlite3"), reverse=True)
        for obsolete in backups[_PRE_MIGRATION_BACKUP_RETENTION_COUNT:]:
            obsolete.unlink()
        return backup_path


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute a semicolon-delimited migration without executescript's implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("migration SQL ended with an incomplete statement")


def _delete_before(
    connection: sqlite3.Connection,
    table: str,
    timestamp_column: str,
    cutoff: str | None,
    extra_where: str = "",
) -> int:
    """Delete trusted fixed-schema retention candidates with bound cutoff values."""
    if cutoff is None:
        return 0
    where = f"{timestamp_column} < ?"
    if extra_where:
        where += f" AND {extra_where}"
    return connection.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,)).rowcount


def _count_before(
    connection: sqlite3.Connection,
    table: str,
    timestamp_column: str,
    cutoff: str | None,
    extra_where: str = "",
) -> int:
    """Count trusted fixed-schema retention candidates using bound cutoff values."""
    if cutoff is None:
        return 0
    where = f"{timestamp_column} < ?"
    if extra_where:
        where += f" AND {extra_where}"
    row = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (cutoff,)).fetchone()
    return int(row[0])


def _topic_profile_from_row(row: sqlite3.Row) -> TopicProfileRecord:
    selection = _json_mapping_from_row(row, "selection_json")
    safety_limits = _json_mapping_from_row(row, "safety_limits_json")
    return TopicProfileRecord(
        id=int(row["id"]),
        key=str(row["key"]),
        version=int(row["version"]),
        name=str(row["name"]),
        timezone=str(row["timezone"]),
        delivery_deadline=str(row["delivery_deadline"]),
        lookback_hours=int(row["lookback_hours"]),
        selection=selection,
        safety_limits=safety_limits,
        enabled=bool(row["enabled"]),
    )


def _edition_run_from_row(row: sqlite3.Row) -> EditionRunRecord:
    return EditionRunRecord(
        id=int(row["id"]),
        topic_profile_id=int(row["topic_profile_id"]),
        local_date=str(row["local_date"]),
        deadline_at=str(row["deadline_at"]),
        status=str(row["status"]),
        candidate_count=int(row["candidate_count"]),
        translated_count=int(row["translated_count"]),
        degraded_reason_code=row["degraded_reason_code"],
        artifact_path=Path(str(row["artifact_path"])) if row["artifact_path"] else None,
        artifact_sha256=row["artifact_sha256"],
    )


def _edition_item_from_row(row: sqlite3.Row) -> EditionItemRecord:
    return EditionItemRecord(
        edition_run_id=int(row["edition_run_id"]),
        article_id=int(row["article_id"]),
        position=int(row["position"]),
        content_source=str(row["content_source"]),
    )


def _delivery_outbox_from_row(row: sqlite3.Row) -> DeliveryOutboxRecord:
    return DeliveryOutboxRecord(
        id=int(row["id"]),
        edition_run_id=int(row["edition_run_id"]),
        channel=str(row["channel"]),
        target_ref=str(row["target_ref"]),
        idempotency_key=str(row["idempotency_key"]),
        artifact_path=Path(str(row["artifact_path"])),
        payload_sha256=str(row["payload_sha256"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=row["next_attempt_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        provider_message_id=row["provider_message_id"],
        error_code=row["error_code"],
        delivered_at=row["delivered_at"],
    )


def _require_same_delivery(
    record: DeliveryOutboxRecord,
    *,
    edition_run_id: int,
    channel: str,
    target_ref: str,
    artifact_path: Path,
    payload_sha256: str,
) -> None:
    if (
        record.edition_run_id != edition_run_id
        or record.channel != channel
        or record.target_ref != target_ref
        or record.artifact_path != artifact_path
        or record.payload_sha256 != payload_sha256
    ):
        raise ValueError("a different delivery already uses this idempotency key")


def _batch_run_from_row(row: sqlite3.Row) -> BatchRunRecord:
    selector = json.loads(str(row["selector_json"]))
    limits = json.loads(str(row["limits_json"]))
    if not isinstance(selector, dict) or not isinstance(limits, dict):
        raise ValueError("batch run JSON metadata must be objects")
    return BatchRunRecord(
        id=int(row["id"]),
        command=str(row["command"]),
        status=str(row["status"]),
        selector=selector,
        limits=limits,
    )


def _article_values(
    feed_id: int,
    item: ArticleInput,
    content_hash: str,
    first_seen_at: str,
    last_seen_at: str,
    created_at: str,
    updated_at: str,
) -> tuple[object, ...]:
    return (
        feed_id,
        item.guid,
        item.canonical_url,
        item.title,
        item.summary,
        item.content,
        item.author,
        _json_array(item.categories),
        item.published_at,
        item.source_updated_at,
        item.detected_language,
        item.source_language,
        content_hash,
        first_seen_at,
        last_seen_at,
        created_at,
        updated_at,
    )


def _article_hash(item: ArticleInput) -> str:
    payload = {
        "title": item.title,
        "summary": item.summary,
        "content": item.content,
        "author": item.author,
        "categories": item.categories,
        "published_at": item.published_at,
        "source_updated_at": item.source_updated_at,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_usage_dimensions(local_date: str, category: str, provider: str) -> None:
    _validate_date(local_date, field="local_date")
    if category not in {"feed", "translation", "pi", "delivery"}:
        raise ValueError("invalid usage category")
    if not provider or len(provider) > 128 or any(character.isspace() for character in provider):
        raise ValueError("usage provider must be a bounded opaque identifier")


def _validate_topic_profile(item: TopicProfileInput) -> None:
    if not _TOPIC_KEY_PATTERN.fullmatch(item.key):
        raise ValueError("topic key must use lowercase letters, numbers, and single hyphens")
    if item.version < 1:
        raise ValueError("topic version must be positive")
    if not item.name.strip() or len(item.name) > 200:
        raise ValueError("topic name must be non-empty and at most 200 characters")
    try:
        ZoneInfo(item.timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("topic timezone must be an installed IANA timezone") from error
    if not _DEADLINE_PATTERN.fullmatch(item.delivery_deadline):
        raise ValueError("delivery_deadline must use 24-hour HH:MM format")
    if item.lookback_hours < 1 or item.lookback_hours > 24 * 31:
        raise ValueError("lookback_hours must be between 1 and 744")
    _reject_sensitive_metadata(item.selection)
    _reject_sensitive_metadata(item.safety_limits)


def _reject_sensitive_metadata(value: object, *, path: str = "metadata") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized = key.casefold().replace(" ", "-")
            if normalized in _SENSITIVE_METADATA_KEYS:
                raise ValueError(f"sensitive key is not allowed in {path}: {key}")
            _reject_sensitive_metadata(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_metadata(nested, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) > _MAX_TOPIC_METADATA_STRING_CHARS:
            raise ValueError(f"{path} string exceeds the topic metadata limit")
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ValueError(f"{path} contains a non-JSON value")


def _json_object(value: Mapping[str, object], *, field: str) -> str:
    _reject_sensitive_metadata(value, path=field)
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a JSON object") from error
    if len(encoded.encode("utf-8")) > _MAX_TOPIC_METADATA_BYTES:
        raise ValueError(f"{field} exceeds the topic metadata size limit")
    return encoded


def _json_mapping_from_row(row: sqlite3.Row, column: str) -> Mapping[str, object]:
    value = json.loads(str(row[column]))
    if not isinstance(value, dict):
        raise ValueError(f"{column} must contain a JSON object")
    return value


def _validate_date(value: str, *, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO calendar date") from error
    if value != parsed.isoformat():
        raise ValueError(f"{field} must use normalized YYYY-MM-DD format")
    return parsed


def _validate_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    normalized = parsed.astimezone(UTC).isoformat()
    if parsed.utcoffset().total_seconds() != 0 or value != normalized:
        raise ValueError(f"{field} must be a normalized UTC timestamp")
    return parsed


def _validate_sha256(value: str, *, field: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _validate_error_code(value: str) -> None:
    if not value or len(value) > 128 or not re.fullmatch(r"[a-z0-9_]+", value):
        raise ValueError("error_code must use bounded lowercase snake_case")


def _json_array(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _feed_from_row(row: sqlite3.Row) -> FeedRecord:
    return FeedRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        url=str(row["url"]),
        categories=tuple(json.loads(str(row["categories_json"]))),
        language=row["language"],
        poll_interval_minutes=row["poll_interval_minutes"],
        enabled=bool(row["enabled"]),
        origin=str(row["origin"]),
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_checked_at=row["last_checked_at"],
        last_success_at=row["last_success_at"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
    )


def _article_from_row(row: sqlite3.Row) -> ArticleRecord:
    return ArticleRecord(
        id=int(row["id"]),
        feed_id=int(row["feed_id"]),
        guid=row["guid"],
        canonical_url=str(row["canonical_url"]),
        title=str(row["title"]),
        summary=row["summary"],
        content=row["content"],
        author=row["author"],
        categories=tuple(json.loads(str(row["categories_json"]))),
        published_at=row["published_at"],
        source_updated_at=row["source_updated_at"],
        detected_language=row["detected_language"],
        source_language=row["source_language"],
        content_hash=str(row["content_hash"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
    )


def _translation_from_row(row: sqlite3.Row) -> TranslationRecord:
    return TranslationRecord(
        article_id=int(row["article_id"]),
        target_language=str(row["target_language"]),
        title=row["title"],
        summary=row["summary"],
        content=row["content"],
        provider_name=str(row["provider_name"]),
        provider_model=row["provider_model"],
        status=str(row["status"]),
        source_hash=str(row["source_hash"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        attempt_count=int(row["attempt_count"]),
        next_retry_at=row["next_retry_at"],
        last_attempt_at=row["last_attempt_at"],
        terminal=bool(row["terminal"]),
    )


def _extraction_from_row(row: sqlite3.Row) -> ExtractionRecord:
    return ExtractionRecord(
        id=int(row["id"]),
        article_id=int(row["article_id"]),
        provider_name=str(row["provider_name"]),
        source_url=str(row["source_url"]),
        content=row["content"],
        translated_content=row["translated_content"],
        translation_provider_name=row["translation_provider_name"],
        status=str(row["status"]),
        request_id=row["request_id"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _export_run_from_row(row: sqlite3.Row) -> ExportRunRecord:
    return ExportRunRecord(
        id=int(row["id"]),
        profile_name=str(row["profile_name"]),
        output_path=Path(str(row["output_path"])),
        article_count=int(row["article_count"]),
        status=str(row["status"]),
    )


def _export_article_from_row(row: sqlite3.Row) -> ExportArticleRecord:
    article = _article_from_row(row)
    if row["export_translation_status"] is None:
        translation = TranslationRecord(
            article_id=article.id,
            target_language="",
            title=None,
            summary=None,
            content=None,
            provider_name="",
            provider_model=None,
            status="missing",
            source_hash="",
            error_code=None,
            error_message=None,
            attempt_count=0,
            next_retry_at=None,
            last_attempt_at=None,
            terminal=False,
        )
    else:
        translation = TranslationRecord(
            article_id=article.id,
            target_language=str(row["export_translation_target_language"]),
            title=row["export_translation_title"],
            summary=row["export_translation_summary"],
            content=row["export_translation_content"],
            provider_name=str(row["export_translation_provider_name"]),
            provider_model=row["export_translation_provider_model"],
            status=str(row["export_translation_status"]),
            source_hash=str(row["export_translation_source_hash"]),
            error_code=row["export_translation_error_code"],
            error_message=row["export_translation_error_message"],
            attempt_count=int(row["export_translation_attempt_count"]),
            next_retry_at=row["export_translation_next_retry_at"],
            last_attempt_at=row["export_translation_last_attempt_at"],
            terminal=bool(row["export_translation_terminal"]),
        )
    extraction = None
    if row["extraction_id"] is not None:
        extraction = ExtractionRecord(
            id=int(row["extraction_id"]),
            article_id=article.id,
            provider_name=str(row["export_extraction_provider_name"]),
            source_url=str(row["export_extraction_source_url"]),
            content=row["export_extraction_content"],
            translated_content=row["export_extraction_translated_content"],
            translation_provider_name=row["export_extraction_translator"],
            status=str(row["export_extraction_status"]),
            request_id=row["export_extraction_request_id"],
            error_code=row["export_extraction_error_code"],
            error_message=row["export_extraction_error_message"],
        )
    return ExportArticleRecord(
        article=article,
        feed_name=str(row["export_feed_name"]),
        feed_url=str(row["export_feed_url"]),
        translation=translation,
        extraction=extraction,
    )
