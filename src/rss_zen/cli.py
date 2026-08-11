"""Command-line entry points for RSS-Zen."""

import signal
from collections.abc import Callable
from pathlib import Path

import httpx
import typer

from rss_zen.backup import backup_database
from rss_zen.config import load_config
from rss_zen.db import Database
from rss_zen.errors import AppError
from rss_zen.export import MarkdownExporter
from rss_zen.extraction import AnySearchExtractor, ExtractionService
from rss_zen.feeds import import_opml_file, reconcile_config_feeds
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
        feeds = database.list_feeds()
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
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Retry translation for explicitly selected source articles."""
    selected_ids = tuple(article_ids or [])
    if not (selected_ids or source):
        _handle_app_error(
            AppError(
                "translation_selector_required",
                "select articles with --article-id or --source",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        articles = database.list_articles(article_ids=selected_ids, source=source)
        if not articles:
            raise AppError("article_not_found", "no article matches the requested selector")
        with httpx.Client(timeout=30.0) as client:
            service = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
            )
            outcomes = [
                service.translate_article(article)
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
    without_extraction: bool = typer.Option(False, "--without-extraction"),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Retrieve full text for selected articles."""
    selected_ids = tuple(article_ids or [])
    if not (selected_ids or source or without_extraction):
        _handle_app_error(
            AppError(
                "extraction_selector_required",
                "select articles with --article-id, --source, or --without-extraction",
            )
        )
        return
    try:
        database, config = _database_from_config(config_path)
        with httpx.Client(timeout=30.0) as client:
            translator = build_translation_service(
                database,
                config.translation,
                client,
                max_attempts=config.service.translation_max_attempts,
                max_backoff_minutes=config.service.retry_max_backoff_minutes,
            )
            service = ExtractionService(
                database,
                AnySearchExtractor(config.anysearch, client),
                translator=translator,
            )
            results = service.extract_selected(
                article_ids=selected_ids,
                source=source,
                without_extraction=without_extraction,
            )
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


@app.command("export")
def export_articles(
    profile_name: str = typer.Argument(...),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Export articles using a named Markdown profile."""
    try:
        database, config = _database_from_config(config_path)
        profile = next((item for item in config.exports if item.name == profile_name), None)
        if profile is None:
            raise AppError("export_profile_not_found", f"unknown export profile: {profile_name}")
        output_path = profile.output_path
        if not output_path.is_absolute():
            profile = profile.model_copy(update={"output_path": config_path.parent / output_path})
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


@app.command()
def backup(
    backup_directory: Path = typer.Option(Path("backups"), "--backup-directory", "-o"),
    retention_days: int = typer.Option(30, "--retention-days", min=1),
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
) -> None:
    """Create one verified local SQLite backup and apply retention."""
    try:
        database, _config = _database_from_config(config_path)
        target = backup_directory
        if not target.is_absolute():
            target = config_path.parent / target
        result = backup_database(database.path, target, retention_days=retention_days)
    except AppError as error:
        _handle_app_error(error)
        return
    typer.echo(f"backup={result} retention_days={retention_days}")


@app.command()
def status(
    config_path: Path = typer.Option(Path("rss-zen.toml"), "--config", "-c"),
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
