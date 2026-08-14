"""Configuration-authoritative immutable topic reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from rss_zen.db import Database, TopicProfileInput, TopicProfileRecord
from rss_zen.models import TopicConfig


@dataclass(frozen=True)
class TopicReconciliation:
    """Configured current topic records and creation count."""

    topics: tuple[TopicProfileRecord, ...]
    created: int


def reconcile_config_topics(
    database: Database, topics: list[TopicConfig]
) -> TopicReconciliation:
    """Persist exact immutable versions; a reused version with changed data is rejected."""
    records: list[TopicProfileRecord] = []
    created = 0
    for topic in topics:
        previous = database.latest_topic_profile(topic.key)
        if previous is not None and previous.version > topic.version:
            raise ValueError("configured topic version cannot go backwards")
        item = topic_input(topic)
        record = database.create_topic_profile(item)
        if previous is None or previous.id != record.id:
            created += 1
        records.append(record)
    return TopicReconciliation(tuple(records), created)


def validate_config_topics(database: Database, topics: list[TopicConfig]) -> None:
    """Validate dry-run topic versions against persisted state without writing."""
    for topic in topics:
        previous = database.latest_topic_profile(topic.key)
        if previous is None:
            continue
        if previous.version > topic.version:
            raise ValueError("configured topic version cannot go backwards")
        if previous.version == topic.version and not _matches(previous, topic_input(topic)):
            raise ValueError("configured topic reuses a version with different content")


def _matches(record: TopicProfileRecord, item: TopicProfileInput) -> bool:
    return (
        record.key == item.key
        and record.version == item.version
        and record.name == item.name
        and record.timezone == item.timezone
        and record.delivery_deadline == item.delivery_deadline
        and record.lookback_hours == item.lookback_hours
        and dict(record.selection) == dict(item.selection)
        and dict(record.safety_limits) == dict(item.safety_limits)
        and record.enabled == item.enabled
    )


def topic_input(topic: TopicConfig) -> TopicProfileInput:
    """Convert validated operator configuration to persisted non-secret metadata."""
    safety = topic.safety_limits.model_dump()
    safety["preparation_minutes"] = topic.preparation_minutes
    return TopicProfileInput(
        key=topic.key,
        version=topic.version,
        name=topic.name,
        timezone=topic.timezone,
        delivery_deadline=topic.delivery_deadline,
        lookback_hours=topic.lookback_hours,
        selection=topic.selection.model_dump(),
        safety_limits=safety,
        enabled=topic.enabled,
    )
