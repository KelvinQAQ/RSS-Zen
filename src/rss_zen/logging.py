"""Structured logging with request-scoped diagnostic context."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_log_context: ContextVar[dict[str, str] | None] = ContextVar("rss_zen_log_context", default=None)


@contextmanager
def log_context(**values: str) -> Iterator[None]:
    """Attach stable identifiers to log records produced in a block."""
    previous = _log_context.get() or {}
    token = _log_context.set({**previous, **{key: value for key, value in values.items() if value}})
    try:
        yield
    finally:
        _log_context.reset(token)


class JsonFormatter(logging.Formatter):
    """Emit machine-readable events without serializing exception secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_log_context.get() or {})
        for field in ("run_id", "feed_id", "article_id", "error_code"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, verbose: bool = False) -> None:
    """Configure RSS-Zen's process logger once."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("rss_zen")
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
