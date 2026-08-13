from __future__ import annotations

import pytest

from rss_zen.budget import RunBudget
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
