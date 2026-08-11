"""Foreground scheduling for the long-running RSS-Zen service."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Protocol

from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler

from rss_zen.db import Database
from rss_zen.sync import FeedSyncService
from rss_zen.translation import TranslationService


class SchedulerBackend(Protocol):
    """Minimal scheduler surface used by FeedScheduler."""

    def add_job(
        self,
        func: object,
        trigger: str,
        *,
        minutes: int,
        id: str,
        replace_existing: bool,
    ) -> object:
        """Schedule one interval job."""

    def start(self) -> object:
        """Run the scheduler until it is shut down."""

    def shutdown(self, *, wait: bool = True) -> object:
        """Stop the scheduler and optionally wait for active work."""


class FeedScheduler:
    """Schedule and execute per-feed sync operations in the foreground process."""

    def __init__(
        self,
        database: Database,
        sync_service: FeedSyncService,
        *,
        default_interval_minutes: int,
        translation_service: TranslationService | None = None,
        translation_retry_interval_minutes: int = 5,
        scheduler: SchedulerBackend | None = None,
    ) -> None:
        self._database = database
        self._sync_service = sync_service
        self._default_interval_minutes = default_interval_minutes
        self._translation_service = translation_service
        self._translation_retry_interval_minutes = translation_retry_interval_minutes
        self._scheduler: SchedulerBackend = scheduler or BlockingScheduler()
        self._logger = logging.getLogger("rss_zen.scheduler")
        self._stopping = False

    def serve(self) -> None:
        """Schedule jobs, perform an initial sync, then block in the scheduler."""
        self.schedule()
        self.run_once()
        self._scheduler.start()

    def shutdown(self) -> None:
        """Request a graceful shutdown that lets APScheduler finish active work."""
        if self._stopping:
            return
        self._stopping = True
        with suppress(SchedulerNotRunningError):
            # A shutdown signal may arrive during the initial sync, before the
            # scheduler has started; there is nothing left to stop in that case.
            self._scheduler.shutdown(wait=True)

    def schedule(self) -> None:
        """Register one interval job for each enabled feed."""
        for feed in self._enabled_feeds():
            interval = feed.poll_interval_minutes or self._default_interval_minutes
            self._scheduler.add_job(
                lambda url=feed.url: self._sync_url(url),
                "interval",
                minutes=interval,
                id=f"feed-{feed.id}",
                replace_existing=True,
            )
        if self._translation_service is not None:
            self._scheduler.add_job(
                self._retry_translations,
                "interval",
                minutes=self._translation_retry_interval_minutes,
                id="translation-retries",
                replace_existing=True,
            )

    def run_once(self) -> None:
        """Synchronize every enabled feed immediately."""
        if self._stopping:
            return
        self._sync_service.sync_all(self._enabled_feeds())
        self._retry_translations()

    def _retry_translations(self) -> None:
        if self._stopping or self._translation_service is None:
            return
        try:
            self._translation_service.retry_due()
        except Exception as error:
            self._logger.exception("translation_retry_job_failed", extra={"error": str(error)})

    def _sync_url(self, url: str) -> None:
        if self._stopping:
            return
        feed = self._database.get_feed_by_url(url)
        if feed is None or not feed.enabled:
            return
        results = self._sync_service.sync_all([feed])
        if results and results[0].error_code:
            self._logger.warning(
                "scheduled_sync_failed",
                extra={"feed_id": feed.id, "error_code": results[0].error_code},
            )

    def _enabled_feeds(self):
        return [feed for feed in self._database.list_feeds() if feed.enabled]
