"""Command-line entry points for RSS-Zen."""

import json
import re
import shutil
import signal
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer

from rss_zen.backup import backup_database
from rss_zen.budget import RunBudget
from rss_zen.config import load_config
from rss_zen.coordinator import DeadlineCoordinator
from rss_zen.db import Database
from rss_zen.delivery import DeliveryWorker
from rss_zen.edition import EditionBuilder
from rss_zen.errors import AppError, ConfigurationError
from rss_zen.export import MarkdownExporter
from rss_zen.extraction import AnySearchExtractor, ExtractionService
from rss_zen.feeds import import_opml_file, normalize_feed_url, reconcile_config_feeds
from rss_zen.feishu import FeishuClient
from rss_zen.http_client import FeedHttpClient
from rss_zen.logging import configure_logging
from rss_zen.models import AppConfig
from rss_zen.network import FeedUrlPolicy
from rss_zen.reporting import REPORT_SCHEMA_VERSION, write_json_report
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


def _existing_database_from_config(config_path: Path) -> tuple[Database, AppConfig]:
    """Load config and require an existing database without creating or migrating it."""
    config = load_config(config_path)
    database_path = config.database.path
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    if not database_path.is_file():
        raise AppError("database_not_initialized", "database does not exist; run rss-zen init")
    return Database(database_path), config


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


def _write_batch_report(path: Path | None, report: dict[str, object]) -> None:
    """Write an optional batch report, with ``-`` denoting stdout."""
    if path is None:
        return
    if str(path) == "-":
        _json_echo(report)
        return
    write_json_report(path, report)


def _resume_batch_run(database: Database, run_id: int, command: str):
    """Load a compatible checkpoint with safe CLI-facing error codes."""
    try:
        return database.require_batch_run_command(run_id, command)
    except KeyError as error:
        raise AppError("batch_run_not_found", f"batch run does not exist: {run_id}") from error
    except ValueError as error:
        raise AppError("invalid_batch_resume", str(error)) from error


def _run_budget(
    config: AppConfig,
    *,
    max_requests: int | None,
    max_source_chars: int | None,
) -> RunBudget:
    """Build a batch budget; CLI overrides may tighten but never expand policy."""
    configured_requests = config.limits.max_provider_requests_per_run
    configured_chars = config.limits.max_source_chars_per_run
    if max_requests is not None and max_requests > configured_requests:
        raise AppError(
            "invalid_budget_override",
            "budget override cannot exceed configured max_provider_requests_per_run",
        )
    if max_source_chars is not None and max_source_chars > configured_chars:
        raise AppError(
            "invalid_budget_override",
            "budget override cannot exceed configured max_source_chars_per_run",
        )
    return RunBudget(
        max_requests=max_requests or configured_requests,
        max_source_chars=max_source_chars or configured_chars,
    )


def _execution_report(
    command: str,
    *,
    selected_articles: int,
    completed_articles: int,
    failed_articles: int,
    budget: RunBudget,
    skipped: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build an exact report from provider-boundary budget accounting."""
    summary = budget.summary()
    skipped_items = skipped or []
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "command": command,
        "dry_run": False,
        "estimate_only": False,
        "selected_articles": selected_articles,
        "completed_articles": completed_articles,
        "failed_articles": failed_articles,
        "skipped_articles": len(skipped_items),
        "source_chars": summary["source_chars"],
        "provider_requests": summary["provider_requests"],
        "limits": {
            "source_chars": summary["max_source_chars"],
            "provider_requests": summary["max_provider_requests"],
        },
        "skipped": skipped_items,
    }


def _dry_run_report(
    command: str, articles: list[object], config: AppConfig, budget: RunBudget
) -> dict[str, object]:
    """Produce a network-free lower-bound estimate for selected source records."""
    source_chars = sum(
        len(value)
        for article in articles
        for value in (article.title, article.summary, article.content)
        if value
    )
    estimated_requests = sum(
        sum(value is not None for value in (article.title, article.summary, article.content))
        for article in articles
    )
    if command == "extract":
        source_chars = sum(len(article.canonical_url) for article in articles)
        estimated_requests = len(articles)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "command": command,
        "dry_run": True,
        "estimate_only": True,
        "selected_articles": len(articles),
        "completed_articles": 0,
        "failed_articles": 0,
        "skipped_articles": 0,
        "source_chars": source_chars,
        "provider_requests": estimated_requests,
        "limits": {
            "articles": (
                config.limits.max_translate_articles_per_run
                if command == "translate"
                else config.limits.max_extract_articles_per_run
            ),
            "source_chars": budget.max_source_chars,
            "provider_requests": budget.max_requests,
        },
        "skipped": [],
    }


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
                    persistent_daily_limits=(
                        config.limits.max_background_provider_requests_per_day,
                        config.limits.max_background_source_chars_per_day,
                    ),
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
    resume: int | None = typer.Option(None, "--resume", min=1),
    checkpoint: bool = typer.Option(True, "--checkpoint/--no-checkpoint"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List selected articles without calling providers."
    ),
    report_json: Path | None = typer.Option(
        None, "--report-json", help="Write a JSON report; use - for stdout."
    ),
    max_requests: int | None = typer.Option(None, "--max-requests", min=1),
    max_source_chars: int | None = typer.Option(None, "--max-source-chars", min=1),
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
    has_selector = bool(selected_ids or source or status_filter)
    if resume is not None and has_selector:
        _handle_app_error(
            AppError("invalid_resume_selector", "--resume cannot be combined with selectors")
        )
        return
    if resume is None and not has_selector:
        _handle_app_error(
            AppError(
                "translation_selector_required",
                "select articles with --article-id, --source, --status, or --resume",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        effective_limit = limit or config.limits.max_translate_articles_per_run
        batch_run = None
        if resume is not None:
            batch_run = _resume_batch_run(database, resume, "translate")
            resumed_ids = database.batch_run_resumable_article_ids(resume)
            articles_by_id = {
                article_id: database.get_article(article_id) for article_id in resumed_ids
            }
            articles = [articles_by_id[article_id] for article_id in resumed_ids][:effective_limit]
        elif status_filter is not None:
            articles = database.list_articles_by_translation_status(
                config.translation.target_language, status=status_filter, limit=effective_limit
            )
        else:
            articles = database.list_articles(
                article_ids=selected_ids, source=source
            )[:effective_limit]
        if not articles:
            raise AppError("article_not_found", "no article matches the requested selector")
        budget = _run_budget(
            config, max_requests=max_requests, max_source_chars=max_source_chars
        )
        if resume is not None and not checkpoint:
            raise AppError("invalid_resume_checkpoint", "--resume requires checkpointing")
        if not dry_run and batch_run is None and checkpoint:
            batch_run = database.create_batch_run(
                command="translate",
                article_ids=tuple(article.id for article in articles),
                selector={
                    "article_ids": [article.id for article in articles],
                    "source": source,
                    "status": status_filter,
                },
                limits=budget.summary(),
            )
        if dry_run:
            for article in articles:
                typer.echo(f"article_id={article.id} title={article.title}")
            _write_batch_report(
                report_json, _dry_run_report("translate", articles, config, budget)
            )
            typer.echo(f"selected={len(articles)} dry_run=true")
            return
        with httpx.Client(timeout=30.0) as client:
            service = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
                budget=budget,
            )
            outcomes = []
            budget_error: AppError | None = None
            skipped: list[dict[str, object]] = []
            for index, article in enumerate(articles):
                try:
                    outcome = service.translate_article(article, force=True)
                    outcomes.append(outcome)
                    if batch_run is not None:
                        database.complete_batch_run_item(
                            batch_run.id,
                            article.id,
                            status="succeeded" if outcome.status == "succeeded" else "failed",
                            error_code=outcome.error_code,
                        )
                except AppError as error:
                    if error.code != "provider_budget_exhausted":
                        raise
                    budget_error = error
                    skipped = [
                        {"article_id": item.id, "reason": error.code}
                        for item in articles[index:]
                    ]
                    if batch_run is not None:
                        for item in articles[index:]:
                            database.complete_batch_run_item(
                                batch_run.id,
                                item.id,
                                status="skipped",
                                error_code=error.code,
                            )
                        database.update_batch_run_status(batch_run.id, status="interrupted")
                    break
    except AppError as error:
        _handle_app_error(error)
        return
    failures = sum(outcome.status != "succeeded" for outcome in outcomes)
    _write_batch_report(
        report_json,
        {
            **_execution_report(
                "translate",
                selected_articles=len(articles),
                completed_articles=len(outcomes) - failures,
                failed_articles=failures,
                budget=budget,
                skipped=skipped,
            ),
            **({"batch_run_id": batch_run.id} if batch_run is not None else {}),
        },
    )
    if batch_run is not None and budget_error is None:
        remaining = database.batch_run_resumable_article_ids(batch_run.id)
        database.update_batch_run_status(
            batch_run.id,
            status="interrupted" if remaining else ("succeeded" if failures == 0 else "failed"),
        )
    if budget_error is not None:
        _handle_app_error(budget_error)
        return
    for outcome in outcomes:
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
    resume: int | None = typer.Option(None, "--resume", min=1),
    checkpoint: bool = typer.Option(True, "--checkpoint/--no-checkpoint"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List selected articles without calling providers."
    ),
    report_json: Path | None = typer.Option(
        None, "--report-json", help="Write a JSON report; use - for stdout."
    ),
    max_requests: int | None = typer.Option(None, "--max-requests", min=1),
    max_source_chars: int | None = typer.Option(None, "--max-source-chars", min=1),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Retrieve full text for selected articles."""
    selected_ids = tuple(article_ids or [])
    has_selector = bool(selected_ids or source or without_extraction or since or until)
    if resume is not None and has_selector:
        _handle_app_error(
            AppError("invalid_resume_selector", "--resume cannot be combined with selectors")
        )
        return
    if resume is None and not has_selector:
        _handle_app_error(
            AppError(
                "extraction_selector_required",
                "select articles with --article-id, --source, --since/--until, "
                "--without-extraction, or --resume",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        effective_limit = limit or config.limits.max_extract_articles_per_run
        batch_run = None
        if resume is not None:
            batch_run = _resume_batch_run(database, resume, "extract")
            resumed_ids = database.batch_run_resumable_article_ids(resume)
            articles = [database.get_article(article_id) for article_id in resumed_ids][
                :effective_limit
            ]
        else:
            published_after = _parse_time_bound(since)
            published_before = _parse_time_bound(until)
            articles = database.list_articles(
                article_ids=selected_ids,
                source=source,
                without_extraction=without_extraction,
                published_after=published_after,
                published_before=published_before,
            )[:effective_limit]
        if not articles:
            raise AppError("article_not_found", "no article matches the requested selector")
        budget = _run_budget(
            config, max_requests=max_requests, max_source_chars=max_source_chars
        )
        if resume is not None and not checkpoint:
            raise AppError("invalid_resume_checkpoint", "--resume requires checkpointing")
        if not dry_run and batch_run is None and checkpoint:
            batch_run = database.create_batch_run(
                command="extract",
                article_ids=tuple(article.id for article in articles),
                selector={
                    "article_ids": [article.id for article in articles],
                    "source": source,
                    "without_extraction": without_extraction,
                    "since": since,
                    "until": until,
                },
                limits=budget.summary(),
            )
        if dry_run:
            for article in articles:
                typer.echo(f"article_id={article.id} title={article.title}")
            _write_batch_report(
                report_json, _dry_run_report("extract", articles, config, budget)
            )
            typer.echo(f"selected={len(articles)} dry_run=true")
            return
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
            results = []
            budget_error: AppError | None = None
            skipped: list[dict[str, object]] = []
            for index, article in enumerate(articles):
                try:
                    item_results = service.extract_articles([article])
                    results.extend(item_results)
                    if batch_run is not None:
                        result = item_results[0]
                        database.complete_batch_run_item(
                            batch_run.id,
                            article.id,
                            status="succeeded" if result.status == "succeeded" else "failed",
                            error_code=result.error_code,
                        )
                except AppError as error:
                    if error.code != "provider_budget_exhausted":
                        raise
                    budget_error = error
                    skipped = [
                        {"article_id": item.id, "reason": error.code}
                        for item in articles[index:]
                    ]
                    if batch_run is not None:
                        for item in articles[index:]:
                            database.complete_batch_run_item(
                                batch_run.id,
                                item.id,
                                status="skipped",
                                error_code=error.code,
                            )
                        database.update_batch_run_status(batch_run.id, status="interrupted")
                    break
    except AppError as error:
        _handle_app_error(error)
        return

    failures = sum(result.status != "succeeded" for result in results)
    _write_batch_report(
        report_json,
        {
            **_execution_report(
                "extract",
                selected_articles=len(articles),
                completed_articles=len(results) - failures,
                failed_articles=failures,
                budget=budget,
                skipped=skipped,
            ),
            **({"batch_run_id": batch_run.id} if batch_run is not None else {}),
        },
    )
    if batch_run is not None and budget_error is None:
        remaining = database.batch_run_resumable_article_ids(batch_run.id)
        database.update_batch_run_status(
            batch_run.id,
            status="interrupted" if remaining else ("succeeded" if failures == 0 else "failed"),
        )
    if budget_error is not None:
        _handle_app_error(budget_error)
        return
    for result in results:
        typer.echo(
            f"article_id={result.article_id} status={result.status}"
            + (f" error={result.error_code}" if result.error_code else "")
        )
    if failures:
        raise typer.Exit(code=1)


@app.command()
def retention(
    action: str | None = typer.Argument(None),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Preview configured retention or explicitly apply it after verified backup."""
    if action not in {None, "apply"}:
        _handle_app_error(AppError("invalid_retention_action", "action must be 'apply'"))
        return
    if action == "apply" and dry_run:
        _handle_app_error(
            AppError("invalid_retention_action", "apply cannot be combined with --dry-run")
        )
        return
    if action is None and not dry_run:
        _handle_app_error(
            AppError(
                "retention_apply_required",
                "use 'rss-zen retention --dry-run' or 'rss-zen retention apply'",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        settings = config.retention
        if not any(
            (
                settings.articles_days,
                settings.failed_extractions_days,
                settings.export_runs_days,
                settings.batch_runs_days,
            )
        ):
            raise AppError("retention_not_configured", "no retention periods are configured")
        now = datetime.now(UTC)

        def cutoff(days: int | None) -> str | None:
            return (now - timedelta(days=days)).isoformat() if days else None

        arguments = {
            "articles_before": cutoff(settings.articles_days),
            "failed_extractions_before": cutoff(settings.failed_extractions_days),
            "export_runs_before": cutoff(settings.export_runs_days),
            "batch_runs_before": cutoff(settings.batch_runs_days),
        }
        if dry_run:
            counts = database.retention_counts(**arguments)
            backup_path = None
        else:
            backup_directory = _resolve_config_path(config.backup.directory, config_path)
            backup_path = backup_database(
                database.path,
                backup_directory,
                retention_days=config.backup.retention_days,
                retention_count=config.backup.retention_count,
            )
            counts = database.apply_retention(**arguments)
    except AppError as error:
        _handle_app_error(error)
        return
    _json_echo(
        {
            "dry_run": dry_run,
            "backup": str(backup_path) if backup_path is not None else None,
            "articles": counts.articles,
            "failed_extractions": counts.failed_extractions,
            "export_runs": counts.export_runs,
            "batch_runs": counts.batch_runs,
        }
    )


@app.command()
def maintenance(
    action: str = typer.Argument(...),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Run explicit local SQLite maintenance; never invoked by the service automatically."""
    if action not in {"checkpoint", "vacuum"}:
        _handle_app_error(
            AppError("invalid_maintenance_action", "action must be checkpoint or vacuum")
        )
        return
    try:
        database, _config = _database_from_config(config_path)
        if action == "checkpoint":
            busy, log_frames, checkpointed_frames = database.checkpoint_wal()
            _json_echo(
                {
                    "action": action,
                    "busy": busy,
                    "log_frames": log_frames,
                    "checkpointed_frames": checkpointed_frames,
                }
            )
            return
        usage = shutil.disk_usage(database.path.parent)
        required_bytes = database.path.stat().st_size
        if usage.free < required_bytes:
            raise AppError(
                "maintenance_insufficient_disk",
                "vacuum requires free disk space at least equal to the database size",
            )
        with database._connection() as connection:
            connection.execute("VACUUM")
    except AppError as error:
        _handle_app_error(error)
        return
    typer.echo(f"action={action} database={database.path}")


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


@app.command("edition-build")
def edition_build(
    topic_key: str = typer.Option(..., "--topic"),
    local_date: str | None = typer.Option(None, "--local-date"),
    deadline_at: str | None = typer.Option(None, "--deadline-at"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Preview or durably build one topic edition without sending it."""
    try:
        if dry_run:
            database, config = _existing_database_from_config(config_path)
        else:
            config = load_config(config_path)
            if not config.feishu.enabled or config.feishu.target_ref is None:
                raise AppError(
                    "feishu_delivery_disabled",
                    "Feishu delivery must be enabled with an approved target before enqueueing",
                )
            database_path = config.database.path
            if not database_path.is_absolute():
                database_path = config_path.parent / database_path
            database = Database(database_path)
            database.initialize()
        topic = database.latest_topic_profile(topic_key)
        if topic is None:
            raise AppError("topic_not_found", "topic profile does not exist")
        resolved_date, resolved_deadline = _edition_schedule(
            topic.timezone,
            topic.delivery_deadline,
            local_date=local_date,
            deadline_at=deadline_at,
        )
        builder = EditionBuilder(
            database,
            target_language=config.translation.target_language,
            output_directory=database.path.parent / "editions",
        )
        if dry_run:
            preview = builder.preview(
                topic,
                local_date=resolved_date,
                deadline_at=resolved_deadline,
            )
            payload = {
                "schema_version": 1,
                "command": "edition-build",
                "dry_run": True,
                "topic": topic.key,
                "topic_version": topic.version,
                "local_date": resolved_date,
                "deadline_at": resolved_deadline,
                "article_count": preview.article_count,
                "translated_count": preview.translated_count,
                "degraded": preview.degraded,
                "rendered_bytes": preview.rendered_bytes,
                "content_sources": list(preview.content_sources),
            }
        else:
            result = builder.build(
                topic,
                local_date=resolved_date,
                deadline_at=resolved_deadline,
                target_ref=config.feishu.target_ref,
            )
            payload = {
                "schema_version": 1,
                "command": "edition-build",
                "dry_run": False,
                "topic": topic.key,
                "topic_version": topic.version,
                "local_date": resolved_date,
                "deadline_at": resolved_deadline,
                "edition_run_id": result.edition.id,
                "edition_status": result.edition.status,
                "delivery_id": result.delivery.id,
                "delivery_status": result.delivery.status,
                "article_count": result.article_count,
                "degraded": result.degraded,
                "artifact_path": str(result.artifact_path),
                "artifact_sha256": result.artifact_sha256,
            }
    except (AppError, ConfigurationError) as error:
        _handle_app_error(error)
        return
    except (KeyError, ValueError, sqlite3.Error) as error:
        _handle_app_error(AppError("edition_build_failed", "unable to build edition", cause=error))
        return
    if json_output:
        _json_echo(payload)
    else:
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@app.command("delivery-run")
def delivery_run(
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Inspect or process one bounded Feishu delivery batch."""
    try:
        if dry_run:
            database, config = _existing_database_from_config(config_path)
            health = database.edition_delivery_health()
            payload = {
                "schema_version": 1,
                "command": "delivery-run",
                "dry_run": True,
                "enabled": config.feishu.enabled,
                "pending": health.delivery_pending,
                "sending": health.delivery_sending,
                "retry_wait": health.delivery_retry_wait,
                "terminal": health.delivery_terminal,
            }
        else:
            database, config = _database_from_config(config_path)
            if not config.feishu.enabled:
                raise AppError("feishu_delivery_disabled", "Feishu delivery is disabled")
            if not config.feishu.app_id or not config.feishu.app_secret:
                raise AppError(
                    "feishu_credentials_missing",
                    "Feishu credential environment variables are not set",
                )
            with httpx.Client(base_url=config.feishu.base_url, timeout=30.0) as client:
                adapter = FeishuClient(
                    client,
                    app_id=config.feishu.app_id,
                    app_secret=config.feishu.app_secret,
                )
                worker = DeliveryWorker(
                    database,
                    adapter,
                    worker_id="rss-zen-delivery",
                    max_attempts=config.feishu.max_attempts,
                    batch_size=config.feishu.batch_size,
                    lease_minutes=config.feishu.lease_minutes,
                    max_backoff_minutes=config.feishu.max_backoff_minutes,
                )
                result = worker.run_once(now=datetime.now(UTC).isoformat())
            payload = {
                "schema_version": 1,
                "command": "delivery-run",
                "dry_run": False,
                "claimed": result.claimed,
                "delivered": result.delivered,
                "retried": result.retried,
                "terminal": result.terminal,
            }
    except (AppError, ConfigurationError) as error:
        _handle_app_error(error)
        return
    except (ValueError, sqlite3.Error) as error:
        _handle_app_error(
            AppError("delivery_run_failed", "unable to process delivery batch", cause=error)
        )
        return
    if json_output:
        _json_echo(payload)
    else:
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


def _edition_schedule(
    timezone: str,
    delivery_deadline: str,
    *,
    local_date: str | None,
    deadline_at: str | None,
) -> tuple[str, str]:
    zone = ZoneInfo(timezone)
    local_day = date.fromisoformat(local_date) if local_date else datetime.now(zone).date()
    if deadline_at is not None:
        parsed = datetime.fromisoformat(deadline_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("deadline_at must include a UTC offset")
        normalized = parsed.astimezone(UTC).isoformat()
        if normalized != deadline_at:
            raise ValueError("deadline_at must be a normalized UTC timestamp")
        return local_day.isoformat(), normalized
    local_time = time.fromisoformat(delivery_deadline)
    deadline = datetime.combine(local_day, local_time, tzinfo=zone).astimezone(UTC)
    return local_day.isoformat(), deadline.isoformat()


@app.command("deadline-run")
def deadline_run(
    dry_run: bool = typer.Option(False, "--dry-run"),
    now: str | None = typer.Option(None, "--now", help="Normalized UTC timestamp for testing."),
    json_output: bool = typer.Option(False, "--json"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Preview or build all configured topic editions whose preparation window is open."""
    try:
        instant = now or datetime.now(UTC).isoformat()
        if dry_run:
            database, config = _existing_database_from_config(config_path)
            target_ref = config.feishu.target_ref or "chat:dry-run"
        else:
            config = load_config(config_path)
            if not config.feishu.enabled or config.feishu.target_ref is None:
                raise AppError(
                    "feishu_delivery_disabled",
                    "Feishu delivery must be enabled with an approved target",
                )
            database_path = config.database.path
            if not database_path.is_absolute():
                database_path = config_path.parent / database_path
            database = Database(database_path)
            database.initialize()
            target_ref = config.feishu.target_ref
        coordinator = DeadlineCoordinator(
            database,
            target_language=config.translation.target_language,
            output_directory=database.path.parent / "editions",
            target_ref=target_ref,
        )
        results = coordinator.run(config.topics, now=instant, dry_run=dry_run)
        payload = {
            "schema_version": 1,
            "command": "deadline-run",
            "dry_run": dry_run,
            "now": instant,
            "topics": [asdict(result) for result in results],
        }
    except (AppError, ConfigurationError) as error:
        _handle_app_error(error)
        return
    except (KeyError, ValueError, sqlite3.Error) as error:
        _handle_app_error(
            AppError("deadline_run_failed", "unable to coordinate topic editions", cause=error)
        )
        return
    if json_output:
        _json_echo(payload)
    else:
        typer.echo(f"topics={len(payload['topics'])} dry_run={dry_run}")


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
        if not config.feishu.enabled:
            _check("feishu", "ok", "delivery is disabled")
        else:
            missing = [
                name
                for name, value in (
                    (config.feishu.app_id_env, config.feishu.app_id),
                    (config.feishu.app_secret_env, config.feishu.app_secret),
                )
                if name is not None and not value
            ]
            _check(
                "feishu",
                "warning" if missing else "ok",
                "delivery enabled; "
                + (f"missing env: {', '.join(missing)}" if missing else "credentials are set"),
            )

    # 3. Local configuration metadata and curl prerequisite.
    if config is not None:
        try:
            mode = config_path.stat().st_mode & 0o777
            _check(
                "config_permissions",
                "warning" if mode & 0o007 else "ok",
                f"mode {mode:04o}" + (" is world-readable" if mode & 0o007 else ""),
            )
        except OSError as error:
            _check("config_permissions", "warning", f"cannot inspect mode: {error}")
        if any(feed.fetcher == "curl" for feed in config.feeds):
            curl_path = shutil.which("curl")
            _check(
                "curl",
                "ok" if curl_path else "error",
                "curl executable is available" if curl_path else "curl executable not found",
            )

    # 4. Database file and integrity (without initializing/creating).
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
                    backup_directory = _resolve_config_path(config.backup.directory, config_path)
                    _health_filesystem_snapshot(database_path, backup_directory)
                    _check(
                        "database_storage",
                        "ok",
                        "database/WAL/SHM and filesystem capacity are readable",
                    )
                else:
                    _check("database", "error", f"integrity check failed: {integrity}")
            except sqlite3.Error as error:
                _check("database", "error", f"cannot open database: {error}")

    # 5. Feed and processing health from the repository (only when initialized).
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
            batch_counts = database.batch_health_counts()
            delivery_health = database.edition_delivery_health()
            batch_status = (
                "warning" if batch_counts.interrupted or batch_counts.resumable_items else "ok"
            )
            _check(
                "batches",
                batch_status,
                f"{batch_counts.running} running, {batch_counts.interrupted} interrupted, "
                f"{batch_counts.resumable_items} resumable items",
            )
            problems = counts.failed_translation_count + counts.failed_extraction_count
            _check(
                "processing",
                "ok" if problems == 0 else "warning",
                f"{counts.article_count} articles, "
                f"{counts.pending_translation_count} pending, "
                f"{counts.failed_translation_count} failed translation, "
                f"{counts.failed_extraction_count} failed extraction",
            )
            delivery_status = (
                "error"
                if delivery_health.delivery_terminal
                else (
                    "warning"
                    if delivery_health.delivery_retry_wait
                    or delivery_health.delivery_pending
                    or delivery_health.delivery_sending
                    else "ok"
                )
            )
            _check(
                "editions_delivery",
                delivery_status,
                f"{delivery_health.edition_active} active editions, "
                f"{delivery_health.edition_queued} queued/delivering, "
                f"{delivery_health.delivery_pending} pending deliveries, "
                f"{delivery_health.delivery_retry_wait} retrying, "
                f"{delivery_health.delivery_terminal} terminal",
            )
        except sqlite3.Error as error:
            _check("repository", "error", f"cannot read repository state: {error}")

    # 6. Backup freshness.
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
        warning_count = sum(check["status"] == "warning" for check in checks)
        error_count = sum(check["status"] == "error" for check in checks)
        _json_echo(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "healthy": error_count == 0,
                "warning_count": warning_count,
                "error_count": error_count,
                "checks": checks,
            }
        )
        if error_count:
            raise typer.Exit(code=1)
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
        enabled_feeds = [feed for feed in feeds if feed.enabled]
        latest_success = max(
            (feed.last_success_at for feed in feeds if feed.last_success_at), default=None
        )
        backup_directory = _resolve_config_path(config.backup.directory, config_path)
        filesystem = _health_filesystem_snapshot(database.path, backup_directory)
        health_now = datetime.now(UTC)
        stale_feeds = [
            feed
            for feed in enabled_feeds
            if _is_feed_stale(
                feed,
                default_poll_interval_minutes=config.service.default_poll_interval_minutes,
                now=health_now,
            )
        ]
        batch_counts = database.batch_health_counts()
        delivery_health = database.edition_delivery_health()
        reporting_date = health_now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        month_start = reporting_date.replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        daily_usage = database.usage_totals(local_date=reporting_date.isoformat())
        monthly_usage = database.usage_totals_period(
            start_date=month_start.isoformat(), end_date=next_month.isoformat()
        )
        _json_echo(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "database": {
                    **filesystem["database"],
                    "schema_version": database.schema_version(),
                },
                "disk": filesystem["disk"],
                "backups": filesystem["backups"],
                "feed_health": {
                    "total": len(feeds),
                    "enabled": len(enabled_feeds),
                    "never_succeeded": sum(
                        1 for feed in enabled_feeds if feed.last_success_at is None
                    ),
                    "stale": len(stale_feeds),
                },
                "batches": {
                    "running": batch_counts.running,
                    "interrupted": batch_counts.interrupted,
                    "resumable_items": batch_counts.resumable_items,
                },
                "editions": {
                    "active": delivery_health.edition_active,
                    "queued": delivery_health.edition_queued,
                    "delivered": delivery_health.edition_delivered,
                    "terminal": delivery_health.edition_terminal,
                    "degraded": delivery_health.edition_degraded,
                },
                "delivery": {
                    "enabled": config.feishu.enabled,
                    "budget_mode": config.feishu.budget_mode,
                    "pending": delivery_health.delivery_pending,
                    "sending": delivery_health.delivery_sending,
                    "retry_wait": delivery_health.delivery_retry_wait,
                    "delivered": delivery_health.delivery_delivered,
                    "terminal": delivery_health.delivery_terminal,
                    "latest_delivered_at": delivery_health.latest_delivered_at,
                },
                "usage": {
                    "timezone": "Asia/Shanghai",
                    "local_date": reporting_date.isoformat(),
                    "daily": {
                        "requests": daily_usage.requests,
                        "source_chars": daily_usage.source_chars,
                        "response_bytes": daily_usage.response_bytes,
                        "attempts": daily_usage.attempts,
                        "input_tokens": daily_usage.input_tokens,
                        "output_tokens": daily_usage.output_tokens,
                        "cost_microunits": daily_usage.cost_microunits,
                    },
                    "monthly": {
                        "requests": monthly_usage.requests,
                        "source_chars": monthly_usage.source_chars,
                        "response_bytes": monthly_usage.response_bytes,
                        "attempts": monthly_usage.attempts,
                        "input_tokens": monthly_usage.input_tokens,
                        "output_tokens": monthly_usage.output_tokens,
                        "cost_microunits": monthly_usage.cost_microunits,
                    },
                },
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


def _is_feed_stale(
    feed: object, *, default_poll_interval_minutes: int, now: datetime
) -> bool:
    """Return whether an enabled feed has never succeeded or missed two poll windows."""
    last_success = feed.last_success_at
    if last_success is None:
        return True
    try:
        timestamp = datetime.fromisoformat(last_success).astimezone(UTC)
    except (TypeError, ValueError):
        return True
    interval = feed.poll_interval_minutes or default_poll_interval_minutes
    return now - timestamp > timedelta(minutes=interval * 2)


def _health_filesystem_snapshot(database_path: Path, backup_directory: Path) -> dict[str, object]:
    """Collect local file-system health data without opening external connections."""
    usage = shutil.disk_usage(database_path.parent)
    backups = sorted(backup_directory.glob("rss-*.sqlite3")) if backup_directory.is_dir() else []
    newest = backups[-1] if backups else None
    generated_at = datetime.now(UTC)
    newest_age_seconds = (
        int((generated_at - datetime.fromtimestamp(newest.stat().st_mtime, UTC)).total_seconds())
        if newest is not None
        else None
    )
    return {
        "database": {
            "path": str(database_path),
            "size_bytes": database_path.stat().st_size,
            "wal_size_bytes": _file_size(database_path.with_name(f"{database_path.name}-wal")),
            "shm_size_bytes": _file_size(database_path.with_name(f"{database_path.name}-shm")),
        },
        "disk": {"free_bytes": usage.free, "total_bytes": usage.total},
        "backups": {
            "directory": str(backup_directory),
            "newest": newest.name if newest is not None else None,
            "newest_age_seconds": newest_age_seconds,
            "size_bytes": sum(path.stat().st_size for path in backups),
        },
    }


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


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
