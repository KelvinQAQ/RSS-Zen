"""Bounded structured Pi editorial subprocess integration."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rss_zen.db import Database
from rss_zen.errors import AppError
from rss_zen.models import EditorialSettings


@dataclass(frozen=True)
class EditorialRequest:
    topic: str
    local_date: str
    article_ids: tuple[int, ...]
    deterministic_markdown: str


@dataclass(frozen=True)
class EditorialResult:
    title: str
    introduction: str
    ordered_article_ids: tuple[int, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class EditorialAttempt:
    result: EditorialResult | None
    fallback: bool
    error_code: str | None = None


class EditorialService:
    """Convert all Agent failures into a measured deterministic-fallback decision."""

    def __init__(self, database: Database, runner: PiEditorialRunner) -> None:
        self._database = database
        self._runner = runner

    def attempt(
        self, request: EditorialRequest, *, now: datetime | None = None
    ) -> EditorialAttempt:
        instant = now or datetime.now(UTC)
        local_date = instant.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        try:
            result = self._runner.edit(request)
        except AppError as error:
            self._database.record_usage(
                local_date=local_date, category="pi", provider="pi", attempts=1
            )
            return EditorialAttempt(None, True, error.code)
        self._database.record_usage(
            local_date=local_date,
            category="pi",
            provider="pi",
            attempts=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return EditorialAttempt(result, False)


class PiEditorialRunner:
    """Run ephemeral no-tools Pi and parse one strict structured editorial response."""

    def __init__(
        self,
        settings: EditorialSettings,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._settings = settings
        self._run = run

    def edit(self, request: EditorialRequest) -> EditorialResult:
        if not self._settings.enabled:
            raise AppError("editorial_disabled", "Pi editorial processing is disabled")
        if len(request.deterministic_markdown) > self._settings.max_input_chars:
            raise AppError("editorial_input_too_large", "Pi editorial input exceeds its limit")
        prompt = _prompt(request)
        command = [
            self._settings.executable,
            "--mode",
            "json",
            "--no-tools",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
        ]
        if self._settings.provider:
            command.extend(["--provider", self._settings.provider])
        if self._settings.model:
            command.extend(["--model", self._settings.model])
        command.append(prompt)
        try:
            completed = self._run(
                command,
                capture_output=True,
                text=True,
                timeout=self._settings.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise AppError(
                "editorial_timeout", "Pi editorial processing timed out", cause=error
            ) from error
        except OSError as error:
            raise AppError(
                "editorial_unavailable", "Pi editorial executable is unavailable", cause=error
            ) from error
        if completed.returncode != 0:
            raise AppError("editorial_failed", "Pi editorial processing failed")
        if len(completed.stdout.encode("utf-8")) > self._settings.max_event_bytes:
            raise AppError(
                "editorial_events_too_large", "Pi editorial event stream exceeds its limit"
            )
        text, input_tokens, output_tokens = _final_assistant(completed.stdout)
        if len(text) > self._settings.max_output_chars:
            raise AppError("editorial_output_too_large", "Pi editorial output exceeds its limit")
        if (
            input_tokens > self._settings.max_input_tokens
            or output_tokens > self._settings.max_output_tokens
        ):
            raise AppError(
                "editorial_token_budget_exhausted", "Pi editorial token limit was exceeded"
            )
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise AppError(
                "editorial_output_invalid", "Pi editorial output is not valid JSON", cause=error
            ) from error
        return _validate_result(payload, request, input_tokens, output_tokens)


def _prompt(request: EditorialRequest) -> str:
    payload = {
        "schema_version": 1,
        "topic": request.topic,
        "local_date": request.local_date,
        "article_ids": request.article_ids,
        "deterministic_markdown": request.deterministic_markdown,
    }
    return (
        "Return only JSON with keys schema_version=1, title, introduction, ordered_article_ids. "
        "Do not add/remove article IDs and do not use tools. Input:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _final_assistant(stream: str) -> tuple[str, int, int]:
    final: tuple[str, int, int] | None = None
    for raw_line in stream.splitlines():
        try:
            event = json.loads(raw_line)
        except ValueError as error:
            raise AppError(
                "editorial_events_invalid", "Pi emitted invalid JSON events", cause=error
            ) from error
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text(message.get("content"))
        usage = message.get("usage", {})
        input_tokens = _token_count(usage, "input", "inputTokens")
        output_tokens = _token_count(usage, "output", "outputTokens")
        final = (text, input_tokens, output_tokens)
    if final is None:
        raise AppError("editorial_events_invalid", "Pi did not emit a final assistant message")
    return final


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if parts and all(isinstance(part, str) for part in parts):
            return "".join(parts)
    raise AppError("editorial_events_invalid", "Pi assistant message has invalid content")


def _token_count(usage: object, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _validate_result(
    payload: object, request: EditorialRequest, input_tokens: int, output_tokens: int
) -> EditorialResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "title",
        "introduction",
        "ordered_article_ids",
    }:
        raise AppError("editorial_output_invalid", "Pi editorial output schema is invalid")
    title = payload.get("title")
    introduction = payload.get("introduction")
    ordered = payload.get("ordered_article_ids")
    if payload.get("schema_version") != 1 or not isinstance(title, str) or not title.strip():
        raise AppError("editorial_output_invalid", "Pi editorial output fields are invalid")
    if not isinstance(introduction, str) or len(title) > 200 or len(introduction) > 4000:
        raise AppError("editorial_output_invalid", "Pi editorial output fields are invalid")
    if not isinstance(ordered, list) or any(not isinstance(value, int) for value in ordered):
        raise AppError("editorial_output_invalid", "Pi editorial article order is invalid")
    if len(ordered) != len(set(ordered)) or set(ordered) != set(request.article_ids):
        raise AppError("editorial_output_invalid", "Pi editorial article IDs do not match input")
    return EditorialResult(
        title.strip(), introduction.strip(), tuple(ordered), input_tokens, output_tokens
    )
