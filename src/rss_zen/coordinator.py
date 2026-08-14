"""Deterministic per-topic deadline coordination without external provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rss_zen.db import Database, TopicProfileRecord
from rss_zen.edition import EditionBuilder
from rss_zen.models import TopicConfig
from rss_zen.topics import reconcile_config_topics, topic_input, validate_config_topics


@dataclass(frozen=True)
class CoordinatedTopicResult:
    """One topic decision for a coordinator run."""

    topic: str
    local_date: str
    deadline_at: str
    action: str
    late: bool
    article_count: int | None = None
    edition_run_id: int | None = None
    delivery_id: int | None = None


class DeadlineCoordinator:
    """Build each configured edition once after its preparation window opens."""

    def __init__(
        self,
        database: Database,
        *,
        target_language: str,
        output_directory: Path,
        target_ref: str,
    ) -> None:
        self._database = database
        self._target_language = target_language
        self._output_directory = output_directory
        self._target_ref = target_ref

    def run(
        self, topics: list[TopicConfig], *, now: str, dry_run: bool
    ) -> tuple[CoordinatedTopicResult, ...]:
        """Preview or build all due enabled topics using an injected normalized UTC clock."""
        instant = _utc_timestamp(now)
        persisted: dict[str, TopicProfileRecord] = {}
        if dry_run:
            validate_config_topics(self._database, topics)
        else:
            persisted = {
                record.key: record
                for record in reconcile_config_topics(self._database, topics).topics
            }
        results: list[CoordinatedTopicResult] = []
        for config in topics:
            if not config.enabled:
                continue
            local_day, deadline, opens = _schedule(config, instant)
            if instant < opens:
                results.append(
                    CoordinatedTopicResult(
                        config.key,
                        local_day.isoformat(),
                        deadline.isoformat(),
                        "not_due",
                        late=False,
                    )
                )
                continue
            topic = persisted.get(config.key) if not dry_run else _preview_record(config)
            if topic is None:
                raise RuntimeError("configured topic was not reconciled")
            builder = EditionBuilder(
                self._database,
                target_language=self._target_language,
                output_directory=self._output_directory,
            )
            if dry_run:
                preview = builder.preview(
                    topic,
                    local_date=local_day.isoformat(),
                    deadline_at=deadline.isoformat(),
                )
                results.append(
                    CoordinatedTopicResult(
                        config.key,
                        local_day.isoformat(),
                        deadline.isoformat(),
                        "would_build",
                        late=instant > deadline,
                        article_count=preview.article_count,
                    )
                )
            else:
                built = builder.build(
                    topic,
                    local_date=local_day.isoformat(),
                    deadline_at=deadline.isoformat(),
                    target_ref=self._target_ref,
                )
                results.append(
                    CoordinatedTopicResult(
                        config.key,
                        local_day.isoformat(),
                        deadline.isoformat(),
                        "built",
                        late=instant > deadline,
                        article_count=built.article_count,
                        edition_run_id=built.edition.id,
                        delivery_id=built.delivery.id,
                    )
                )
        return tuple(results)


def _schedule(topic: TopicConfig, instant: datetime) -> tuple[date, datetime, datetime]:
    zone = ZoneInfo(topic.timezone)
    local_instant = instant.astimezone(zone)
    local_day = local_instant.date()
    deadline = datetime.combine(
        local_day, time.fromisoformat(topic.delivery_deadline), tzinfo=zone
    ).astimezone(UTC)
    opens = deadline - timedelta(minutes=topic.preparation_minutes)
    return local_day, deadline, opens


def _preview_record(topic: TopicConfig) -> TopicProfileRecord:
    item = topic_input(topic)
    return TopicProfileRecord(
        id=0,
        key=item.key,
        version=item.version,
        name=item.name,
        timezone=item.timezone,
        delivery_deadline=item.delivery_deadline,
        lookback_hours=item.lookback_hours,
        selection=item.selection,
        safety_limits=item.safety_limits,
        enabled=item.enabled,
    )


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must include a UTC offset")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("now must be a normalized UTC timestamp")
    return normalized
