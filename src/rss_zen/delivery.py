"""Bounded durable delivery-worker orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from rss_zen.db import Database, DeliveryOutboxRecord
from rss_zen.errors import AppError
from rss_zen.feishu import FeishuDeliveryReceipt


class DeliveryAdapter(Protocol):
    """One external delivery implementation."""

    def deliver(self, delivery: DeliveryOutboxRecord) -> FeishuDeliveryReceipt:
        """Deliver one leased outbox item or raise a safe AppError."""


@dataclass(frozen=True)
class DeliveryWorkerResult:
    """Aggregate result for one bounded worker invocation."""

    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    terminal: int = 0


class DeliveryWorker:
    """Claim and process a bounded outbox batch without sleeping or internal loops."""

    def __init__(
        self,
        database: Database,
        adapter: DeliveryAdapter,
        *,
        worker_id: str,
        max_attempts: int = 5,
        batch_size: int = 10,
        lease_minutes: int = 5,
        max_backoff_minutes: int = 60,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and bounded")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if batch_size < 1 or batch_size > 100:
            raise ValueError("batch_size must be between 1 and 100")
        if lease_minutes < 1 or lease_minutes > 60:
            raise ValueError("lease_minutes must be between 1 and 60")
        if max_backoff_minutes < 1:
            raise ValueError("max_backoff_minutes must be positive")
        self._database = database
        self._adapter = adapter
        self._worker_id = worker_id
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        self._lease_minutes = lease_minutes
        self._max_backoff_minutes = max_backoff_minutes

    def run_once(self, *, now: str) -> DeliveryWorkerResult:
        """Process one due batch and persist every outcome before returning."""
        started_at = _utc_timestamp(now)
        lease_expires_at = (started_at + timedelta(minutes=self._lease_minutes)).isoformat()
        deliveries = self._database.claim_due_deliveries(
            worker_id=self._worker_id,
            now=started_at.isoformat(),
            lease_expires_at=lease_expires_at,
            limit=self._batch_size,
        )
        delivered = 0
        retried = 0
        terminal = 0
        for delivery in deliveries:
            try:
                receipt = self._adapter.deliver(delivery)
            except AppError as error:
                if error.retryable and delivery.attempt_count < self._max_attempts:
                    next_attempt = started_at + timedelta(
                        minutes=min(
                            2 ** (delivery.attempt_count - 1), self._max_backoff_minutes
                        )
                    )
                    self._database.record_delivery_retry(
                        delivery.id,
                        worker_id=self._worker_id,
                        error_code=error.code,
                        next_attempt_at=next_attempt.isoformat(),
                    )
                    retried += 1
                else:
                    terminal_code = (
                        "delivery_attempts_exhausted"
                        if error.retryable and delivery.attempt_count >= self._max_attempts
                        else error.code
                    )
                    self._database.record_delivery_terminal(
                        delivery.id,
                        worker_id=self._worker_id,
                        error_code=terminal_code,
                    )
                    terminal += 1
                continue
            self._database.record_delivery_success(
                delivery.id,
                worker_id=self._worker_id,
                provider_message_id=receipt.primary_message_id,
            )
            delivered += 1
        return DeliveryWorkerResult(
            claimed=len(deliveries),
            delivered=delivered,
            retried=retried,
            terminal=terminal,
        )


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("now must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must include a UTC offset")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("now must be a normalized UTC timestamp")
    return normalized
