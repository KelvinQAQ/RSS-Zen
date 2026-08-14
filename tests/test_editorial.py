from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from rss_zen.db import Database
from rss_zen.editorial import EditorialRequest, EditorialService, PiEditorialRunner
from rss_zen.errors import AppError
from rss_zen.models import EditorialSettings


def _request() -> EditorialRequest:
    return EditorialRequest("indo-pacific", "2026-08-14", (2, 1), "# Deterministic\n")


def _event(payload: dict, *, input_tokens: int = 10, output_tokens: int = 5) -> str:
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "usage": {"input": input_tokens, "output": output_tokens},
    }
    return json.dumps({"type": "message_end", "message": message}) + "\n"


def test_pi_runner_uses_ephemeral_no_tools_command_and_validates_output() -> None:
    captured = []

    def fake(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_event(
                {
                    "schema_version": 1,
                    "title": "每日汇编",
                    "introduction": "导语",
                    "ordered_article_ids": [1, 2],
                }
            ),
            stderr="",
        )

    settings = EditorialSettings(enabled=True, provider="openai", model="gpt-test")
    result = PiEditorialRunner(settings, run=fake).edit(_request())

    command = captured[0][0]
    for flag in (
        "--no-tools",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
    ):
        assert flag in command
    assert result.ordered_article_ids == (1, 2)
    assert result.input_tokens == 10
    assert result.output_tokens == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "title": "x", "introduction": "", "ordered_article_ids": [1]},
        {"schema_version": 1, "title": "x", "introduction": "", "ordered_article_ids": [1, 1]},
        {"schema_version": 2, "title": "x", "introduction": "", "ordered_article_ids": [1, 2]},
    ],
)
def test_pi_runner_rejects_schema_and_article_id_drift(payload: dict) -> None:
    def fake(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=_event(payload), stderr="")

    with pytest.raises(AppError) as excinfo:
        PiEditorialRunner(EditorialSettings(enabled=True), run=fake).edit(_request())
    assert excinfo.value.code == "editorial_output_invalid"


def test_pi_runner_maps_timeout_malformed_events_and_token_excess_safely() -> None:
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(AppError) as excinfo:
        PiEditorialRunner(EditorialSettings(enabled=True), run=timeout).edit(_request())
    assert excinfo.value.code == "editorial_timeout"

    def malformed(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not-json\n", stderr="private")

    with pytest.raises(AppError) as excinfo:
        PiEditorialRunner(EditorialSettings(enabled=True), run=malformed).edit(_request())
    assert excinfo.value.code == "editorial_events_invalid"
    assert "private" not in str(excinfo.value)

    def excessive(command, **kwargs):
        payload = {
            "schema_version": 1,
            "title": "x",
            "introduction": "",
            "ordered_article_ids": [1, 2],
        }
        return subprocess.CompletedProcess(
            command, 0, stdout=_event(payload, output_tokens=50), stderr=""
        )

    settings = EditorialSettings(enabled=True, max_output_tokens=10)
    with pytest.raises(AppError) as excinfo:
        PiEditorialRunner(settings, run=excessive).edit(_request())
    assert excinfo.value.code == "editorial_token_budget_exhausted"


def test_editorial_service_records_tokens_and_falls_back_on_runner_error(tmp_path) -> None:
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()

    class Successful:
        def edit(self, request):
            from rss_zen.editorial import EditorialResult

            return EditorialResult("Title", "Intro", request.article_ids, 12, 7)

    success = EditorialService(database, Successful()).attempt(
        _request(), now=datetime(2026, 8, 14, tzinfo=UTC)
    )
    assert success.fallback is False
    totals = database.usage_totals(local_date="2026-08-14", category="pi", provider="pi")
    assert totals.attempts == 1
    assert totals.input_tokens == 12
    assert totals.output_tokens == 7

    class Failing:
        def edit(self, request):
            raise AppError("editorial_timeout", "timeout")

    failed = EditorialService(database, Failing()).attempt(
        _request(), now=datetime(2026, 8, 14, 1, tzinfo=UTC)
    )
    assert failed.fallback is True
    assert failed.error_code == "editorial_timeout"
    assert (
        database.usage_totals(local_date="2026-08-14", category="pi", provider="pi").attempts == 2
    )


def test_editorial_disabled_and_unsafe_executable_are_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        PiEditorialRunner(EditorialSettings()).edit(_request())
    assert excinfo.value.code == "editorial_disabled"

    with pytest.raises(ValueError, match="basename"):
        EditorialSettings(executable="/tmp/pi;evil")
