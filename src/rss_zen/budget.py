"""In-memory accounting for bounded third-party provider runs."""

from __future__ import annotations

from dataclasses import dataclass

from rss_zen.errors import AppError


@dataclass
class RunBudget:
    """Reserve provider work before it crosses an external service boundary."""

    max_requests: int
    max_source_chars: int
    provider_requests: int = 0
    source_chars: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least one")
        if self.max_source_chars < 1:
            raise ValueError("max_source_chars must be at least one")

    @property
    def remaining_requests(self) -> int:
        """Return the number of provider requests that may still be sent."""
        return self.max_requests - self.provider_requests

    @property
    def remaining_source_chars(self) -> int:
        """Return the number of source characters that may still be sent."""
        return self.max_source_chars - self.source_chars

    def summary(self) -> dict[str, int]:
        """Return JSON-safe exact runtime consumption and configured ceilings."""
        return {
            "provider_requests": self.provider_requests,
            "source_chars": self.source_chars,
            "max_provider_requests": self.max_requests,
            "max_source_chars": self.max_source_chars,
        }

    def reserve(self, *, source_chars: int, requests: int = 1) -> None:
        """Atomically reserve an exact provider request before issuing it."""
        if source_chars < 0:
            raise ValueError("source_chars must not be negative")
        if requests < 1:
            raise ValueError("requests must be at least one")
        if self.provider_requests + requests > self.max_requests:
            raise AppError(
                "provider_budget_exhausted",
                "provider request budget is exhausted",
            )
        if self.source_chars + source_chars > self.max_source_chars:
            raise AppError(
                "provider_budget_exhausted",
                "provider source-character budget is exhausted",
            )
        self.provider_requests += requests
        self.source_chars += source_chars
