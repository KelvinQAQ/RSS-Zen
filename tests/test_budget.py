from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rss_zen.budget import PersistentProviderBudget, RunBudget
from rss_zen.db import Database
from rss_zen.errors import AppError


def test_budget_tracks_exact_consumption() -> None:
    budget = RunBudget(max_requests=3, max_source_chars=10)

    budget.reserve(source_chars=4)
    budget.reserve(source_chars=6, requests=2)

    assert budget.provider_requests == 3
    assert budget.source_chars == 10
    assert budget.remaining_requests == 0
    assert budget.remaining_source_chars == 0


def test_budget_rejects_request_before_consuming_it() -> None:
    budget = RunBudget(max_requests=1, max_source_chars=10)
    budget.reserve(source_chars=3)

    with pytest.raises(AppError, match="request budget") as excinfo:
        budget.reserve(source_chars=1)

    assert excinfo.value.code == "provider_budget_exhausted"
    assert budget.provider_requests == 1
    assert budget.source_chars == 3


def test_budget_rejects_source_characters_before_consuming_them() -> None:
    budget = RunBudget(max_requests=3, max_source_chars=5)

    with pytest.raises(AppError, match="character budget") as excinfo:
        budget.reserve(source_chars=6)

    assert excinfo.value.code == "provider_budget_exhausted"
    assert budget.provider_requests == 0
    assert budget.source_chars == 0


def test_persistent_budget_survives_new_instances_and_resets_by_local_date(tmp_path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    clock = [datetime(2026, 8, 13, 15, 59, tzinfo=UTC)]  # 23:59 Asia/Shanghai

    first = PersistentProviderBudget(
        database,
        provider="provider-a",
        max_requests=2,
        max_source_chars=10,
        now=lambda: clock[0],
    )
    first.reserve(source_chars=5)
    second = PersistentProviderBudget(
        database,
        provider="provider-a",
        max_requests=2,
        max_source_chars=10,
        now=lambda: clock[0],
    )
    second.reserve(source_chars=5)
    with pytest.raises(Exception) as excinfo:
        second.reserve(source_chars=1)
    assert excinfo.value.code == "provider_daily_budget_exhausted"
    assert excinfo.value.retryable is True

    totals = database.usage_totals(
        local_date="2026-08-13", category="translation", provider="provider-a"
    )
    assert totals.requests == 2
    assert totals.source_chars == 10

    clock[0] = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)  # next Asia/Shanghai day
    second.reserve(source_chars=1)
    assert database.usage_totals(
        local_date="2026-08-14", category="translation", provider="provider-a"
    ).requests == 1


def test_budget_requires_positive_limits_and_nonnegative_reservations() -> None:
    with pytest.raises(ValueError, match="max_requests"):
        RunBudget(max_requests=0, max_source_chars=1)
    with pytest.raises(ValueError, match="max_source_chars"):
        RunBudget(max_requests=1, max_source_chars=0)

    budget = RunBudget(max_requests=1, max_source_chars=1)
    with pytest.raises(ValueError, match="source_chars"):
        budget.reserve(source_chars=-1)
    with pytest.raises(ValueError, match="requests"):
        budget.reserve(source_chars=0, requests=0)
