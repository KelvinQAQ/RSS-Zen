"""Command-line entry points for RSS-Zen."""

import json
import re
import signal
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import typer

from rss_zen.backup import backup_database
from rss_zen.budget import RunBudget
from rss_zen.config import load_config
from rss_zen.db import Database
from rss_zen.errors import AppError, ConfigurationError
from rss_zen.export import MarkdownExporter
from rss_zen.extraction import AnySearchExtractor, ExtractionService
from rss_zen.feeds import import_opml_file, normalize_feed_url, reconcile_config_feeds
from rss_zen.http_client import FeedHttpClient
from rss_zen.logging import configure_logging
from rss_zen.models import AppConfig
from rss_zen.network import FeedUrlPolicy
from rss_zen.runtime import single_instance_lock
from rss_zen.scheduler import FeedScheduler
from rss_zen.sync import FeedSyncService
from rss_zen.translation import build_translation_service

app = typer.Typer(
    name="rss-zen",
    no_args_is_help=True,
    help="Synchronize multilingual RSS feeds and export Markdown article collections.",
)


def _database_from_config(config_path: Path) -> tuple[Database, AppConfig]:
    config = load_config(config_path)
    database_path = config.database.path
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    database = Database(database_path)
    database.initialize()
    return database, config


def _handle_app_error(error: AppError) -> None:
    typer.echo(f"error [{error.code}]: {error.message}", err=True)
    raise typer.Exit(code=1) from error


def _install_shutdown_handlers(stop: Callable[[], None]) -> dict[int, object]:
    """Arrange for systemd SIGTERM and terminal SIGINT to stop work cleanly."""
    previous: dict[int, object] = {}
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, lambda _number, _frame: stop())
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)


_DURATION_RE = re.compile(r"^(\d+)\s*([hdw])$", re.IGNORECASE)


def _parse_time_bound(value: str | None) -> str | None:
    """Parse a time bound as either ISO 8601 or a relative duration (e.g. 2d, 12h, 1w)."""
    if value is None:
        return None
    match = _DURATION_RE.match(value.strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return (datetime.now(UTC) - delta).isoformat()
    return value


def _json_echo(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@app.command()
def init(
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Initialize the configured SQLite database and configured feeds."""
    try:
        database, config = _database_from_config(config_path)
        feeds = reconcile_config_feeds(database, config.feeds)
    except AppError as error:
        _handle_app_error(error)
        return
    typer.echo(
        f"database={database.path} feeds_imported={feeds.imported} feeds_updated={feeds.updated}"
    )


@app.command()
def serve(
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Run the scheduled feed synchronization service."""
    configure_logging(verbose=verbose)
    try:
        database, config = _database_from_config(config_path)
        with single_instance_lock(database.path):
            reconcile_config_feeds(database, config.feeds)
            with httpx.Client(timeout=30.0) as client:
                translator = build_translation_service(
                    database,
                    config.translation,
                    client,
                    max_attempts=config.service.translation_max_attempts,
                    max_backoff_minutes=config.service.retry_max_backoff_minutes,
                    max_translation_chars=config.limits.max_translation_chars,
                )
                sync_service = FeedSyncService(
                    database,
                    FeedHttpClient(
                        client,
                        max_response_bytes=config.limits.max_feed_response_bytes,
                        policy=FeedUrlPolicy(),
                    ),
                    translator,
                    limits=config.limits,
                    feed_headers=_config_feed_headers(config),
                    curl_urls=_config_curl_urls(config),
                )
                scheduler = FeedScheduler(
                    database,
                    sync_service,
                    default_interval_minutes=config.service.default_poll_interval_minutes,
                    translation_service=translator,
                    translation_retry_interval_minutes=config.service.translation_retry_interval_minutes,
                )
                previous_handlers = _install_shutdown_handlers(scheduler.shutdown)
                try:
                    scheduler.serve()
                finally:
                    _restore_signal_handlers(previous_handlers)
    except KeyboardInterrupt:
        typer.echo("service stopped")
    except AppError as error:
        _handle_app_error(error)


@app.command()
def sync(
    source: str | None = typer.Option(None, "--source", "-s"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Synchronize configured RSS and Atom feeds once."""
    try:
        database, config = _database_from_config(config_path)
        reconcile_config_feeds(database, config.feeds)
        feeds = [feed for feed in database.list_feeds() if feed.enabled]
        if source is not None:
            feeds = [feed for feed in feeds if source in {feed.name, feed.url}]
        if not feeds:
            raise AppError("feed_not_found", "no enabled feed matches the requested source")
        with httpx.Client(timeout=30.0) as client:
            translator = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
            )
            results = FeedSyncService(
                database,
                FeedHttpClient(
                    client,
                    max_response_bytes=config.limits.max_feed_response_bytes,
                    policy=FeedUrlPolicy(),
                ),
                translator,
                limits=config.limits,
                feed_headers=_config_feed_headers(config),
                curl_urls=_config_curl_urls(config),
            ).sync_all(feeds)
    except AppError as error:
        _handle_app_error(error)
        return

    failures = 0
    for result in results:
        if result.error_code:
            failures += 1
            typer.echo(f"feed_id={result.feed_id} error={result.error_code}")
        else:
            typer.echo(
                f"feed_id={result.feed_id} created={result.created_articles} "
                f"updated={result.updated_articles} not_modified={result.not_modified}"
            )
    if failures:
        raise typer.Exit(code=1)


@app.command()
def translate(
    article_ids: list[int] | None = typer.Option(None, "--article-id", "-a"),
    source: str | None = typer.Option(None, "--source", "-s"),
    status_filter: str | None = typer.Option(
        None,
        "--status",
        help="Retry all articles whose translation is pending or failed.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List selected articles without calling providers."
    ),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Retry translation for explicitly selected source articles."""
    if status_filter is not None and status_filter not in {"pending", "failed"}:
        _handle_app_error(
            AppError(
                "invalid_translation_status",
                "--status must be one of: pending, failed",
            )
        )
        return
    selected_ids = tuple(article_ids or [])
    if not (selected_ids or source or status_filter):
        _handle_app_error(
            AppError(
                "translation_selector_required",
                "select articles with --article-id, --source, or --status",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        effective_limit = limit or config.limits.max_translate_articles_per_run
        if status_filter is not None:
            articles = database.list_articles_by_translation_status(
                config.translation.target_language, status=status_filter, limit=effective_limit
            )
        else:
            articles = database.list_articles(
                article_ids=selected_ids, source=source
            )[:effective_limit]
        if not articles:
            raise AppError("article_not_found", "no article matches the requested selector")
        if dry_run:
            for article in articles:
                typer.echo(f"article_id={article.id} title={article.title}")
            typer.echo(f"selected={len(articles)} dry_run=true")
            return
        budget = RunBudget(
            max_requests=config.limits.max_provider_requests_per_run,
            max_source_chars=config.limits.max_source_chars_per_run,
        )
        with httpx.Client(timeout=30.0) as client:
            service = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
                budget=budget,
            )
            outcomes = [
                service.translate_article(article, force=True)
                for article in articles
            ]
    except AppError as error:
        _handle_app_error(error)
        return
    failures = 0
    for outcome in outcomes:
        if outcome.status != "succeeded":
            failures += 1
        typer.echo(
            f"article_id={outcome.article_id} status={outcome.status} "
            f"provider={outcome.provider_name}"
            + (f" error={outcome.error_code}" if outcome.error_code else "")
        )
    if failures:
        raise typer.Exit(code=1)


@app.command("import-opml")
def import_opml(
    opml_path: Path = typer.Argument(..., exists=True, readable=True),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Import subscriptions from an OPML file."""
    try:
        database, config = _database_from_config(config_path)
        reconcile_config_feeds(database, config.feeds)
        result = import_opml_file(database, opml_path)
    except AppError as error:
        _handle_app_error(error)
        return
    typer.echo(f"imported={result.imported} updated={result.updated} skipped={result.skipped}")


@app.command()
def extract(
    article_ids: list[int] | None = typer.Option(None, "--article-id", "-a"),
    source: str | None = typer.Option(None, "--source", "-s"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only articles published since: a duration (2d, 12h, 1w) or ISO datetime.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only articles published before: a duration or ISO datetime.",
    ),
    without_extraction: bool = typer.Option(False, "--without-extraction"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List selected articles without calling providers."
    ),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Retrieve full text for selected articles."""
    selected_ids = tuple(article_ids or [])
    if not (selected_ids or source or without_extraction or since or until):
        _handle_app_error(
            AppError(
                "extraction_selector_required",
                "select articles with --article-id, --source, --since/--until, "
                "or --without-extraction",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        published_after = _parse_time_bound(since)
        published_before = _parse_time_bound(until)
        articles = database.list_articles(
            article_ids=selected_ids,
            source=source,
            without_extraction=without_extraction,
            published_after=published_after,
            published_before=published_before,
        )[: limit or config.limits.max_extract_articles_per_run]
        if not articles:
            raise AppError("article_not_found", "no article matches the requested selector")
        if dry_run:
            for article in articles:
                typer.echo(f"article_id={article.id} title={article.title}")
            typer.echo(f"selected={len(articles)} dry_run=true")
            return
        budget = RunBudget(
            max_requests=config.limits.max_provider_requests_per_run,
            max_source_chars=config.limits.max_source_chars_per_run,
        )
        with httpx.Client(timeout=30.0) as client:
            translator = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
                budget=budget,
            )
            service = ExtractionService(
                database,
                AnySearchExtractor(config.anysearch, client, budget=budget),
                translator=translator,
            )
            results = service.extract_articles(articles)
    except AppError as error:
        _handle_app_error(error)
        return

    failures = 0
    for result in results:
        if result.status != "succeeded":
            failures += 1
        typer.echo(
            f"article_id={result.article_id} status={result.status}"
            + (f" error={result.error_code}" if result.error_code else "")
        )
    if failures:
        raise typer.Exit(code=1)


@app.command("list")
def list_articles(
    source: str | None = typer.Option(None, "--source", "-s", help="Filter by feed name or URL."),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Filter articles published since: a duration (2d, 12h, 1w) or ISO datetime.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Filter articles published before: a duration or ISO datetime.",
    ),
    status_filter: str | None = typer.Option(
        None, "--status", help="Filter by translation status (succeeded, failed, pending)."
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1000, help="Max articles to list."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """List articles with their translation/extraction status."""
    try:
        database, config = _database_from_config(config_path)
        published_after = _parse_time_bound(since)
        published_before = _parse_time_bound(until)
        overviews = database.list_articles_overview(
            target_language=config.translation.target_language,
            source=source,
            published_after=published_after,
            published_before=published_before,
            translation_status=status_filter,
            limit=limit,
        )
    except AppError as error:
        _handle_app_error(error)
        return

    if json_output:
        _json_echo(
            [
                {
                    "id": row.article.id,
                    "feed": row.feed_name,
                    "title": row.article.title,
                    "published_at": row.article.published_at,
                    "url": row.article.canonical_url,
                    "translation_status": row.translation_status,
                    "translation_provider": row.translation_provider,
                    "extraction_status": row.extraction_status,
                }
                for row in overviews
            ]
        )
        return

    for row in overviews:
        article = row.article
        typer.echo(
            " ".join(
                part
                for part in [
                    f"id={article.id}",
                    f"published={article.published_at or '-'}",
                    f"translation={row.translation_status or 'pending'}",
                    f"extraction={row.extraction_status or 'not_requested'}",
                    f"title={article.title}",
                ]
            )
        )


@app.command("export")
def export_articles(
    profile_name: str | None = typer.Argument(None),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only articles published since: a duration (2d, 12h, 1w) or ISO datetime.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only articles published before: a duration or ISO datetime.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Export articles using a named Markdown profile."""
    try:
        database, config = _database_from_config(config_path)
        if profile_name is None:
            profiles = [
                {
                    "name": profile.name,
                    "output_path": str(
                        profile.output_path
                        if profile.output_path.is_absolute()
                        else config_path.parent / profile.output_path
                    ),
                    "title": profile.title or "",
                }
                for profile in config.exports
            ]
            if json_output:
                _json_echo(profiles)
                return
            if not profiles:
                typer.echo("no export profiles configured")
                return
            for profile in profiles:
                typer.echo(f"{profile['name']}: {profile['output_path']}")
            return
        profile = next((item for item in config.exports if item.name == profile_name), None)
        if profile is None:
            raise AppError("export_profile_not_found", f"unknown export profile: {profile_name}")
        output_path = profile.output_path
        if not output_path.is_absolute():
            profile = profile.model_copy(update={"output_path": config_path.parent / output_path})
        # CLI --since/--until override the profile's published filters without editing config.
        published_after = _parse_time_bound(since) or profile.filters.published_after
        published_before = _parse_time_bound(until) or profile.filters.published_before
        if published_after is not None or published_before is not None:
            profile = profile.model_copy(
                update={
                    "filters": profile.filters.model_copy(
                        update={
                            "published_after": published_after,
                            "published_before": published_before,
                        }
                    )
                }
            )
        result = MarkdownExporter(
            database, target_language=config.translation.target_language
        ).export_profile(profile)
    except AppError as error:
        _handle_app_error(error)
        return
    message = (
        f"output={result.output_path} "
        f"articles={result.article_count} "
        f"export_run_id={result.export_run_id}"
    )
    typer.echo(message)


@app.command("doctor")
def doctor(
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Diagnose configuration, database integrity, secrets, and processing health.

    Never modifies state: it does not create the database or contact providers.
    """
    checks: list[dict[str, object]] = []

    def _check(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. Configuration schema and secret resolution.
    config: AppConfig | None = None
    try:
        config = load_config(config_path)
        _check(
            "configuration",
            "ok",
            f"loaded {len(config.feeds)} feeds, {len(config.exports)} export profiles",
        )
    except ConfigurationError as error:
        _check("configuration", "error", error.message)

    # 2. Secret presence without leaking values.
    if config is not None:
        for provider in config.translation.providers:
            name = f"secret:{provider.name}"
            if provider.api_key_env is None:
                _check(name, "ok", "no API key required")
            elif provider.api_key:
                _check(name, "ok", f"env {provider.api_key_env} is set")
            else:
                _check(
                    name,
                    "warning",
                    f"env {provider.api_key_env} is not set; provider may fail",
                )
        if config.anysearch.api_key_env is not None:
            name = "secret:anysearch"
            if config.anysearch.api_key:
                _check(name, "ok", f"env {config.anysearch.api_key_env} is set")
            else:
                _check(
                    name,
                    "warning",
                    f"env {config.anysearch.api_key_env} is not set; extraction will fail",
                )

    # 3. Database file and integrity (without initializing/creating).
    database: Database | None = None
    database_path = (
        config_path.parent / config.database.path
        if config is not None and not config.database.path.is_absolute()
        else (config_path.parent / "rss-zen.sqlite3" if config is None else config.database.path)
    )
    if config is not None:
        if not database_path.is_file():
            _check(
                "database",
                "warning",
                f"not found at {database_path}; run 'rss-zen init' first",
            )
        else:
            try:
                size_mb = database_path.stat().st_size / (1024 * 1024)
                connection = sqlite3.connect(database_path)
                try:
                    result = connection.execute("PRAGMA quick_check").fetchone()
                finally:
                    connection.close()
                integrity = result[0] if result else "unknown"
                if integrity == "ok":
                    database = Database(database_path)
                    _check(
                        "database",
                        "ok",
                        f"{database_path.name} ({size_mb:.1f} MB), integrity ok",
                    )
                else:
                    _check("database", "error", f"integrity check failed: {integrity}")
            except sqlite3.Error as error:
                _check("database", "error", f"cannot open database: {error}")

    # 4. Feed and processing health from the repository (only when initialized).
    if database is not None:
        try:
            feeds = database.list_feeds()
            enabled = [feed for feed in feeds if feed.enabled]
            never_succeeded = [
                feed for feed in enabled if feed.last_success_at is None
            ]
            latest_success = max(
                (feed.last_success_at for feed in feeds if feed.last_success_at), default=None
            )
            _check(
                "feeds",
                "ok" if not never_succeeded else "warning",
                f"{len(feeds)} total, {len(enabled)} enabled, "
                f"{len(never_succeeded)} enabled never succeeded",
            )
            _check(
                "sync",
                "ok" if latest_success else "warning",
                f"latest feed success: {latest_success or 'never'}",
            )
            counts = database.processing_counts(config.translation.target_language)
            problems = counts.failed_translation_count + counts.failed_extraction_count
            _check(
                "processing",
                "ok" if problems == 0 else "warning",
                f"{counts.article_count} articles, "
                f"{counts.pending_translation_count} pending, "
                f"{counts.failed_translation_count} failed translation, "
                f"{counts.failed_extraction_count} failed extraction",
            )
        except sqlite3.Error as error:
            _check("repository", "error", f"cannot read repository state: {error}")

    # 5. Backup freshness.
    backup_directory = (
        _resolve_config_path(config.backup.directory, config_path)
        if config is not None
        else config_path.parent / "backups"
    )
    backups = sorted(backup_directory.glob("rss-*.sqlite3")) if backup_directory.is_dir() else []
    if not backups:
        _check("backup", "warning", "no backups found; run 'rss-zen backup' regularly")
    else:
        newest = backups[-1]
        age = datetime.now(UTC) - datetime.fromtimestamp(newest.stat().st_mtime, UTC)
        _check(
            "backup",
            "ok" if age.days <= 2 else "warning",
            f"newest: {newest.name} ({age.days}d old)",
        )

    if json_output:
        _json_echo(
            {
                "healthy": all(check["status"] != "error" for check in checks),
                "checks": checks,
            }
        )
        return
    for check in checks:
        status = check["status"]
        icon = {"ok": "ok", "warning": "warn", "error": "error"}[str(status)]
        typer.echo(f"{icon} {check['check']}: {check['detail']}")
    if any(check["status"] == "error" for check in checks):
        raise typer.Exit(code=1)


@app.command()
def backup(
    backup_directory: Path | None = typer.Option(None, "--backup-directory", "-o"),
    retention_days: int | None = typer.Option(None, "--retention-days", min=1),
    retention_count: int | None = typer.Option(None, "--retention-count", min=1),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Create one verified local SQLite backup and apply configured retention."""
    try:
        database, config = _database_from_config(config_path)
        target = _resolve_config_path(backup_directory or config.backup.directory, config_path)
        days = retention_days or config.backup.retention_days
        count = retention_count or config.backup.retention_count
        result = backup_database(
            database.path,
            target,
            retention_days=days,
            retention_count=count,
        )
    except AppError as error:
        _handle_app_error(error)
        return
    typer.echo(f"backup={result} retention_days={days} retention_count={count}")


@app.command()
def status(
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show feed and processing status."""
    try:
        database, config = _database_from_config(config_path)
        reconcile_config_feeds(database, config.feeds)
        counts = database.processing_counts(config.translation.target_language)
        errors = database.processing_error_counts(config.translation.target_language)
    except AppError as error:
        _handle_app_error(error)
        return
    if json_output:
        feeds = database.list_feeds()
        latest_success = max(
            (feed.last_success_at for feed in feeds if feed.last_success_at), default=None
        )
        _json_echo(
            {
                "counts": {
                    "articles": counts.article_count,
                    "pending_translation": counts.pending_translation_count,
                    "failed_translation": counts.failed_translation_count,
                    "terminal_translation": counts.terminal_translation_count,
                    "failed_extraction": counts.failed_extraction_count,
                },
                "last_sync": {
                    "latest_feed_success": latest_success,
                    "stale_feeds": sum(
                        1 for feed in feeds if feed.enabled and feed.last_success_at is None
                    ),
                },
                "feeds": [
                    {
                        "id": feed.id,
                        "name": feed.name,
                        "enabled": feed.enabled,
                        "url": feed.url,
                        "last_success": feed.last_success_at,
                        "last_error": feed.last_error_code,
                    }
                    for feed in feeds
                ],
                "errors": [
                    {
                        "workflow": error.workflow,
                        "error_code": error.error_code,
                        "count": error.count,
                    }
                    for error in errors
                ],
            }
        )
        return
    typer.echo(
        " ".join(
            [
                f"articles={counts.article_count}",
                f"pending_translation={counts.pending_translation_count}",
                f"failed_translation={counts.failed_translation_count}",
                f"terminal_translation={counts.terminal_translation_count}",
                f"failed_extraction={counts.failed_extraction_count}",
            ]
        )
    )
    for feed in database.list_feeds():
        typer.echo(
            " ".join(
                [
                    f"feed_id={feed.id}",
                    f"enabled={feed.enabled}",
                    f"url={feed.url}",
                    f"last_success={feed.last_success_at or '-'}",
                    f"last_error={feed.last_error_code or '-'}",
                ]
            )
        )
    for error in errors:
        typer.echo(f"{error.workflow}_error={error.error_code} count={error.count}")


def _resolve_config_path(path: Path, config_path: Path) -> Path:
    """Resolve an application path relative to its configuration file."""
    return path if path.is_absolute() else config_path.parent / path


def _config_feed_headers(config: AppConfig) -> dict[str, dict[str, str]]:
    """Index per-feed custom request headers by normalized feed URL."""
    return {
        normalize_feed_url(feed.url): dict(feed.resolved_headers)
        for feed in config.feeds
        if feed.resolved_headers
    }


def _config_curl_urls(config: AppConfig) -> set[str]:
    """Collect normalized feed URLs whose fetcher uses the system curl binary."""
    return {normalize_feed_url(feed.url) for feed in config.feeds if feed.fetcher == "curl"}
