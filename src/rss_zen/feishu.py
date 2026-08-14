"""Feishu custom-app outbound delivery boundary."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from rss_zen.db import DeliveryOutboxRecord
from rss_zen.errors import AppError

_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_FILE_PATH = "/open-apis/im/v1/files"
_MESSAGE_PATH = "/open-apis/im/v1/messages"
_UUID_NAMESPACE = uuid.UUID("2fd5b5c1-d6e8-43ab-b20a-1cc4174fbe62")


@dataclass(frozen=True)
class FeishuDeliveryReceipt:
    """Provider IDs for the summary card and Markdown artifact message."""

    primary_message_id: str
    artifact_message_id: str


class FeishuClient:
    """Send one outbox artifact through a Feishu custom application."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        app_id: str,
        app_secret: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Feishu app credentials must be non-empty")
        self._client = client
        self._app_id = app_id
        self._app_secret = app_secret
        self._now = now
        self._token: str | None = None
        self._token_valid_until = 0.0

    def deliver(self, delivery: DeliveryOutboxRecord) -> FeishuDeliveryReceipt:
        """Upload the artifact, then send an idempotent summary card and file message."""
        chat_id = _chat_id(delivery.target_ref)
        artifact = _verified_artifact(delivery.artifact_path, delivery.payload_sha256)
        token = self._tenant_access_token()
        file_key = self._upload_markdown(token, delivery.artifact_path, artifact)
        primary_message_id = self._send_message(
            token,
            chat_id=chat_id,
            message_type="interactive",
            content={
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "RSS-Zen 每日新闻汇编"},
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "日报已生成，完整 Markdown 文件将在下一条消息发送。",
                    }
                ],
            },
            request_uuid=_delivery_uuid(delivery.idempotency_key, "card"),
        )
        artifact_message_id = self._send_message(
            token,
            chat_id=chat_id,
            message_type="file",
            content={"file_key": file_key},
            request_uuid=_delivery_uuid(delivery.idempotency_key, "file"),
        )
        return FeishuDeliveryReceipt(primary_message_id, artifact_message_id)

    def _tenant_access_token(self) -> str:
        now = self._now()
        if self._token is not None and now < self._token_valid_until:
            return self._token
        payload = self._request_json(
            "POST",
            _TOKEN_PATH,
            json_body={"app_id": self._app_id, "app_secret": self._app_secret},
            authenticated=False,
        )
        token = payload.get("tenant_access_token")
        expires = payload.get("expire")
        if not isinstance(token, str) or not token or not isinstance(expires, int) or expires < 1:
            raise AppError("feishu_response_invalid", "Feishu token response was invalid")
        # Refresh before provider expiry; short test tokens still retain at least one second.
        refresh_margin = min(300, max(1, expires // 10))
        self._token = token
        self._token_valid_until = now + max(1, expires - refresh_margin)
        return token

    def _upload_markdown(self, token: str, path: Path, content: bytes) -> str:
        payload = self._request_json(
            "POST",
            _FILE_PATH,
            token=token,
            data={"file_type": "stream", "file_name": path.name},
            files={"file": (path.name, content, "text/markdown")},
        )
        data = payload.get("data")
        file_key = data.get("file_key") if isinstance(data, dict) else None
        if not isinstance(file_key, str) or not file_key:
            raise AppError("feishu_response_invalid", "Feishu file response was invalid")
        return file_key

    def _send_message(
        self,
        token: str,
        *,
        chat_id: str,
        message_type: str,
        content: dict[str, object],
        request_uuid: str,
    ) -> str:
        payload = self._request_json(
            "POST",
            _MESSAGE_PATH,
            token=token,
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": chat_id,
                "msg_type": message_type,
                "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                "uuid": request_uuid,
            },
        )
        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise AppError("feishu_response_invalid", "Feishu message response was invalid")
        return message_id

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        authenticated: bool = True,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {token}"} if authenticated and token else {}
        try:
            response = self._client.request(
                method,
                path,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                files=files,
            )
        except httpx.TimeoutException as error:
            raise AppError(
                "feishu_timeout", "Feishu request timed out", retryable=True, cause=error
            ) from error
        except httpx.RequestError as error:
            raise AppError(
                "feishu_network_error", "Feishu request failed", retryable=True, cause=error
            ) from error
        if response.status_code == 429:
            raise AppError("feishu_rate_limited", "Feishu rate limited the request", retryable=True)
        if response.status_code >= 500:
            raise AppError(
                "feishu_unavailable", "Feishu is temporarily unavailable", retryable=True
            )
        if response.status_code in {401, 403}:
            raise AppError("feishu_auth_rejected", "Feishu rejected application authorization")
        if response.status_code >= 400:
            raise AppError("feishu_request_rejected", "Feishu rejected the request")
        try:
            payload = response.json()
        except ValueError as error:
            raise AppError(
                "feishu_response_invalid", "Feishu returned an invalid response", cause=error
            ) from error
        if not isinstance(payload, dict):
            raise AppError("feishu_response_invalid", "Feishu returned an invalid response")
        code = payload.get("code")
        if code != 0:
            retryable = code in {99991400, 99991663}
            raise AppError(
                "feishu_rate_limited" if retryable else "feishu_request_rejected",
                "Feishu rejected the request",
                retryable=retryable,
            )
        return payload


def _chat_id(target_ref: str) -> str:
    prefix = "chat:"
    if not target_ref.startswith(prefix):
        raise AppError("feishu_target_invalid", "Feishu target reference is invalid")
    chat_id = target_ref[len(prefix) :]
    if not chat_id or len(chat_id) > 255 or any(char.isspace() for char in chat_id):
        raise AppError("feishu_target_invalid", "Feishu target reference is invalid")
    return chat_id


def _verified_artifact(path: Path, expected_sha256: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AppError(
            "delivery_artifact_missing", "Delivery artifact is not readable", cause=error
        ) from error
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise AppError("delivery_artifact_changed", "Delivery artifact hash does not match")
    return content


def _delivery_uuid(idempotency_key: str, part: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{idempotency_key}:{part}"))
