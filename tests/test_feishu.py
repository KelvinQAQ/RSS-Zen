from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rss_zen.db import Database, TopicProfileInput
from rss_zen.delivery import DeliveryWorker
from rss_zen.errors import AppError
from rss_zen.feishu import FeishuClient


def _pending_delivery(tmp_path: Path):
    database = Database(tmp_path / "rss-zen.sqlite3")
    database.initialize()
    topic = database.create_topic_profile(
        TopicProfileInput(
            key="indo-pacific",
            version=1,
            name="印太安全",
            timezone="Asia/Shanghai",
            delivery_deadline="07:30",
            lookback_hours=24,
            selection={"keywords": ["Taiwan"]},
            safety_limits={"max_candidates": 10, "max_rendered_bytes": 100_000},
        )
    )
    edition = database.create_edition_run(
        topic_profile_id=topic.id,
        local_date="2026-08-14",
        deadline_at="2026-08-13T23:30:00+00:00",
    )
    for status in ("refreshing", "selecting"):
        edition = database.transition_edition_run(edition.id, status=status)
    database.freeze_edition_items(edition.id, ())
    artifact = tmp_path / "editions" / "daily.md"
    artifact.parent.mkdir()
    artifact.write_text("# 印太安全 — 2026-08-14\n\n今日无符合主题的更新。\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    edition = database.transition_edition_run(
        edition.id,
        status="rendered",
        artifact_path=artifact,
        artifact_sha256=digest,
    )
    delivery = database.create_delivery_outbox_item(
        edition_run_id=edition.id,
        channel="feishu",
        target_ref="chat:oc_approved",
        idempotency_key="2026-08-14:indo-pacific:v1:feishu",
        artifact_path=artifact,
        payload_sha256=digest,
    )
    return database, edition, delivery


def test_feishu_client_caches_token_uploads_markdown_and_sends_idempotent_messages(
    tmp_path: Path,
) -> None:
    _, _, delivery = _pending_delivery(tmp_path)
    requests: list[httpx.Request] = []
    token_calls = 0
    message_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, message_calls
        requests.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            token_calls += 1
            assert json.loads(request.content) == {"app_id": "cli_test", "app_secret": "secret"}
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        assert request.headers["Authorization"] == "Bearer tenant-token"
        if request.url.path.endswith("/im/v1/files"):
            assert "multipart/form-data" in request.headers["Content-Type"]
            assert b"daily.md" in request.content
            assert b"file_type" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_123"}})
        if request.url.path.endswith("/im/v1/messages"):
            message_calls += 1
            assert request.url.params["receive_id_type"] == "chat_id"
            payload = json.loads(request.content)
            assert payload["receive_id"] == "oc_approved"
            assert payload["uuid"]
            content = json.loads(payload["content"])
            if payload["msg_type"] == "interactive":
                assert content["header"]["title"]["content"] == "RSS-Zen 每日新闻汇编"
            else:
                assert payload["msg_type"] == "file"
                assert content == {"file_key": "file_123"}
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": f"om_{message_calls}"}}
            )
        raise AssertionError(request.url)

    client = FeishuClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://open.feishu.test"),
        app_id="cli_test",
        app_secret="secret",
        now=lambda: 1000.0,
    )

    first = client.deliver(delivery)
    second = client.deliver(delivery)

    assert first.primary_message_id == "om_1"
    assert first.artifact_message_id == "om_2"
    assert second.primary_message_id == "om_3"
    assert token_calls == 1
    assert message_calls == 4
    first_message_payloads = [
        json.loads(request.content)
        for request in requests
        if request.url.path.endswith("/im/v1/messages")
    ]
    assert first_message_payloads[0]["uuid"] == first_message_payloads[2]["uuid"]
    assert first_message_payloads[1]["uuid"] == first_message_payloads[3]["uuid"]


def test_feishu_client_refreshes_expiring_token(tmp_path: Path) -> None:
    _, _, delivery = _pending_delivery(tmp_path)
    clock = [1000.0]
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            token_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"t-{token_calls}", "expire": 10},
            )
        if request.url.path.endswith("/im/v1/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "f"}})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om"}})

    client = FeishuClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test"),
        app_id="app",
        app_secret="secret",
        now=lambda: clock[0],
    )
    client.deliver(delivery)
    clock[0] = 1008.0
    client.deliver(delivery)
    assert token_calls == 1
    clock[0] = 1009.0
    client.deliver(delivery)
    assert token_calls == 2


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [("timeout", "feishu_timeout"), ("unavailable", "feishu_unavailable")],
)
def test_feishu_client_maps_timeout_and_5xx_to_retryable_errors(
    tmp_path: Path, failure: str, expected_code: str
) -> None:
    _, _, delivery = _pending_delivery(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 60})
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout detail", request=request)
        return httpx.Response(503, content=b"private upstream response")

    client = FeishuClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test"),
        app_id="app",
        app_secret="secret",
    )
    with pytest.raises(AppError) as excinfo:
        client.deliver(delivery)
    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is True
    assert "private" not in str(excinfo.value)


def test_feishu_client_maps_safe_retryable_and_terminal_errors(tmp_path: Path) -> None:
    _, _, delivery = _pending_delivery(tmp_path)

    def rate_limited(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 60})
        return httpx.Response(429, json={"code": 99991400, "msg": "sensitive detail"})

    client = FeishuClient(
        httpx.Client(transport=httpx.MockTransport(rate_limited), base_url="https://test"),
        app_id="app",
        app_secret="secret",
    )
    with pytest.raises(AppError) as excinfo:
        client.deliver(delivery)
    assert excinfo.value.code == "feishu_rate_limited"
    assert excinfo.value.retryable is True
    assert "sensitive detail" not in str(excinfo.value)

    def invalid_target(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 60})
        if request.url.path.endswith("/im/v1/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "f"}})
        return httpx.Response(400, json={"code": 230001, "msg": "private target detail"})

    client = FeishuClient(
        httpx.Client(transport=httpx.MockTransport(invalid_target), base_url="https://test"),
        app_id="app",
        app_secret="secret",
    )
    with pytest.raises(AppError) as excinfo:
        client.deliver(delivery)
    assert excinfo.value.code == "feishu_request_rejected"
    assert excinfo.value.retryable is False
    assert "private target detail" not in str(excinfo.value)

    delivery.artifact_path.write_text("changed", encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        client.deliver(delivery)
    assert excinfo.value.code == "delivery_artifact_changed"
    assert excinfo.value.retryable is False


def test_delivery_worker_persists_retry_success_terminal_and_does_not_resend(
    tmp_path: Path,
) -> None:
    database, edition, delivery = _pending_delivery(tmp_path)

    class RetryThenSuccess:
        calls = 0

        def deliver(self, _delivery):
            from rss_zen.feishu import FeishuDeliveryReceipt

            self.calls += 1
            if self.calls == 1:
                raise AppError("feishu_unavailable", "Feishu is temporarily unavailable", True)
            return FeishuDeliveryReceipt("om_card", "om_file")

    adapter = RetryThenSuccess()
    worker = DeliveryWorker(database, adapter, worker_id="worker-1", max_attempts=3)
    first = worker.run_once(now="2026-08-14T00:00:00+00:00")
    assert first.retried == 1
    assert database.get_delivery_outbox_item(delivery.id).status == "retry_wait"
    assert database.get_edition_run(edition.id).status == "queued"

    assert worker.run_once(now="2026-08-14T00:00:59+00:00").claimed == 0
    second = worker.run_once(now="2026-08-14T00:01:00+00:00")
    assert second.delivered == 1
    assert database.get_delivery_outbox_item(delivery.id).provider_message_id == "om_card"
    assert database.get_edition_run(edition.id).status == "delivered"
    assert worker.run_once(now="2026-08-14T01:00:00+00:00").claimed == 0
    assert adapter.calls == 2
    usage = database.usage_totals(
        local_date="2026-08-14", category="delivery", provider="feishu"
    )
    assert usage.attempts == 2

    database2, edition2, delivery2 = _pending_delivery(tmp_path / "terminal")

    class TerminalAdapter:
        def deliver(self, _delivery):
            raise AppError("feishu_request_rejected", "request rejected", False)

    terminal_worker = DeliveryWorker(
        database2, TerminalAdapter(), worker_id="worker-2", max_attempts=3
    )
    result = terminal_worker.run_once(now="2026-08-14T00:00:00+00:00")
    assert result.terminal == 1
    assert database2.get_delivery_outbox_item(delivery2.id).error_code == "feishu_request_rejected"
    assert database2.get_edition_run(edition2.id).status == "terminal"


def test_delivery_worker_terminates_after_retry_budget_is_exhausted(tmp_path: Path) -> None:
    database, edition, delivery = _pending_delivery(tmp_path)

    class AlwaysUnavailable:
        def deliver(self, _delivery):
            raise AppError("feishu_unavailable", "temporarily unavailable", True)

    worker = DeliveryWorker(
        database,
        AlwaysUnavailable(),
        worker_id="worker-1",
        max_attempts=2,
        max_backoff_minutes=1,
    )
    assert worker.run_once(now="2026-08-14T00:00:00+00:00").retried == 1
    result = worker.run_once(now="2026-08-14T00:01:00+00:00")

    assert result.terminal == 1
    stored = database.get_delivery_outbox_item(delivery.id)
    assert stored.attempt_count == 2
    assert stored.error_code == "delivery_attempts_exhausted"
    assert database.get_edition_run(edition.id).status == "terminal"
