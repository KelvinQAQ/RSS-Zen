from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from apscheduler.schedulers.base import SchedulerNotRunningError

from rss_zen.db import Database, FeedInput
from rss_zen.scheduler import FeedScheduler


@dataclass
class FakeSyncService:
    calls: list[list[int]] = field(default_factory=list)

    def sync_all(self, feeds):
        self.calls.append([feed.id for feed in feeds])
        return []


@dataclass
class FakeScheduler:
    jobs: list[tuple[Callable[[], None], str, int, str]] = field(default_factory=list)
    started: bool = False
    shutdown_calls: list[bool] = field(default_factory=list)

    def add_job(self, func, trigger, *, minutes, id, replace_existing):
        assert trigger == "interval"
        assert replace_existing is True
        self.jobs.append((func, trigger, minutes, id))

    def start(self):
        self.started = True

    def shutdown(self, *, wait=True):
        self.shutdown_calls.append(wait)


def test_scheduler_runs_initial_sync_and_uses_per_feed_intervals(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    first = database.upsert_feed(
        FeedInput(name="First", url="https://example.test/first.xml", poll_interval_minutes=10)
    )
    _ = database.upsert_feed(
        FeedInput(name="Disabled", url="https://example.test/disabled.xml", enabled=False)
    )
    sync_service = FakeSyncService()
    fake_scheduler = FakeScheduler()
    scheduler = FeedScheduler(
        database,
        sync_service,
        default_interval_minutes=30,
        scheduler=fake_scheduler,
    )

    scheduler.serve()

    assert sync_service.calls == [[first.id]]
    assert [(minutes, job_id) for _, _, minutes, job_id in fake_scheduler.jobs] == [
        (10, f"feed-{first.id}")
    ]
    assert fake_scheduler.started is True


def test_scheduled_job_loads_latest_feed_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    feed = database.upsert_feed(FeedInput(name="Feed", url="https://example.test/feed.xml"))
    sync_service = FakeSyncService()
    fake_scheduler = FakeScheduler()
    scheduler = FeedScheduler(
        database,
        sync_service,
        default_interval_minutes=30,
        scheduler=fake_scheduler,
    )
    scheduler.schedule()

    fake_scheduler.jobs[0][0]()

    assert sync_service.calls == [[feed.id]]


@dataclass
class FakeTranslationService:
    calls: int = 0

    def retry_due(self):
        self.calls += 1
        return []


def test_scheduler_registers_translation_retry_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    database.upsert_feed(FeedInput(name="Feed", url="https://example.test/feed.xml"))
    sync_service = FakeSyncService()
    translation_service = FakeTranslationService()
    fake_scheduler = FakeScheduler()
    scheduler = FeedScheduler(
        database,
        sync_service,
        default_interval_minutes=30,
        translation_service=translation_service,
        translation_retry_interval_minutes=7,
        scheduler=fake_scheduler,
    )

    scheduler.schedule()
    retry_job = next(job for job in fake_scheduler.jobs if job[3] == "translation-retries")
    retry_job[0]()

    assert retry_job[2] == 7
    assert translation_service.calls == 1


def test_scheduler_shutdown_before_start_is_idempotent(tmp_path: Path) -> None:
    """A shutdown signal during the initial sync must not crash the service."""

    class UnstartedScheduler(FakeScheduler):
        def shutdown(self, *, wait=True):
            raise SchedulerNotRunningError("Scheduler is not running")

    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    database.upsert_feed(FeedInput(name="Feed", url="https://example.test/feed.xml"))
    scheduler = FeedScheduler(
        database,
        FakeSyncService(),
        default_interval_minutes=30,
        scheduler=UnstartedScheduler(),
    )

    scheduler.shutdown()  # must not raise
    scheduler.shutdown()  # second call is a no-op


def test_scheduler_shutdown_stops_backend_and_prevents_new_work(tmp_path: Path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    database.upsert_feed(FeedInput(name="Feed", url="https://example.test/feed.xml"))
    sync_service = FakeSyncService()
    fake_scheduler = FakeScheduler()
    scheduler = FeedScheduler(
        database, sync_service, default_interval_minutes=30, scheduler=fake_scheduler
    )

    scheduler.shutdown()
    scheduler.run_once()

    assert fake_scheduler.shutdown_calls == [True]
    assert sync_service.calls == []
