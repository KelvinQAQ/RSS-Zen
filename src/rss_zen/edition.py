"""Deterministic topic-edition selection, rendering, and durable enqueueing."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rss_zen.db import (
    Database,
    DeliveryOutboxRecord,
    EditionItemInput,
    EditionRunRecord,
    ExportArticleRecord,
    TopicProfileRecord,
)
from rss_zen.editorial import EditorialRequest, EditorialService
from rss_zen.export import _dedupe_by_title, _filter_by_keywords
from rss_zen.markdown import escape_inline_text, safe_markdown_url


@dataclass(frozen=True)
class EditionPreview:
    """A network-free, mutation-free deterministic selection estimate."""

    article_ids: tuple[int, ...]
    content_sources: tuple[str, ...]
    article_count: int
    translated_count: int
    degraded: bool
    rendered_bytes: int


@dataclass(frozen=True)
class EditionBuildResult:
    """One rendered and durably enqueued edition."""

    edition: EditionRunRecord
    delivery: DeliveryOutboxRecord
    artifact_path: Path
    artifact_sha256: str
    article_count: int
    degraded: bool


class EditionBuilder:
    """Build a deterministic edition without provider, extraction, Pi, or Feishu calls."""

    def __init__(
        self,
        database: Database,
        *,
        target_language: str,
        output_directory: Path,
        editorial_service: EditorialService | None = None,
    ) -> None:
        self._database = database
        self._target_language = target_language
        self._output_directory = output_directory
        self._editorial_service = editorial_service

    def preview(
        self,
        topic: TopicProfileRecord,
        *,
        local_date: str,
        deadline_at: str,
    ) -> EditionPreview:
        """Select and render in memory without persisting an edition, artifact, or delivery."""
        selected = self._fit_render_limit(
            topic,
            self._select(topic, deadline_at=deadline_at),
            local_date=local_date,
        )
        sources = tuple(_content_source(record) for record in selected)
        translated_count = sum(1 for record in selected if record.translation.status == "succeeded")
        degraded = any(
            source.startswith("original_") or record.translation.status != "succeeded"
            for source, record in zip(sources, selected, strict=True)
        )
        rendered = _render_edition(
            topic,
            local_date,
            selected,
            content_sources=sources,
            degraded=degraded,
        )
        return EditionPreview(
            article_ids=tuple(record.article.id for record in selected),
            content_sources=sources,
            article_count=len(selected),
            translated_count=translated_count,
            degraded=degraded,
            rendered_bytes=len(rendered.encode("utf-8")),
        )

    def build(
        self,
        topic: TopicProfileRecord,
        *,
        local_date: str,
        deadline_at: str,
        target_ref: str,
    ) -> EditionBuildResult:
        """Resume or create an edition through deterministic rendering and outbox enqueue."""
        edition = self._database.create_edition_run(
            topic_profile_id=topic.id,
            local_date=local_date,
            deadline_at=deadline_at,
        )
        artifact_path = self._artifact_path(topic, local_date)
        delivery = self._database.delivery_for_edition(edition.id, channel="feishu")
        if delivery is not None:
            return self._result_from_existing(topic, edition, delivery, target_ref=target_ref)

        if edition.status == "planned":
            edition = self._database.transition_edition_run(edition.id, status="refreshing")
        if edition.status == "refreshing":
            edition = self._database.transition_edition_run(edition.id, status="selecting")
        if edition.status == "selecting":
            selected = self._select(topic, deadline_at=deadline_at)
            rendered_records = self._fit_render_limit(topic, selected, local_date=local_date)
            items = tuple(
                EditionItemInput(record.article.id, _content_source(record))
                for record in rendered_records
            )
            self._database.freeze_edition_items(edition.id, items)
            edition = self._database.get_edition_run(edition.id)
        else:
            rendered_records = self._records_for_frozen_items(edition.id)

        frozen_sources = tuple(
            item.content_source for item in self._database.edition_items(edition.id)
        )
        if len(frozen_sources) != len(rendered_records):
            raise ValueError("frozen edition provenance does not match candidate records")
        degraded = any(
            source.startswith("original_") or record.translation.status != "succeeded"
            for source, record in zip(frozen_sources, rendered_records, strict=True)
        )
        editorial_title: str | None = None
        editorial_introduction: str | None = None
        persisted: object = None
        if self._editorial_service is not None and edition.status in {"frozen", "editorial"}:
            draft = _render_edition(
                topic,
                local_date,
                rendered_records,
                content_sources=frozen_sources,
                degraded=degraded,
            )
            persisted = self._database.begin_editorial(edition.id)
            if persisted is None:
                attempt = self._editorial_service.attempt(
                    EditorialRequest(
                        topic.key,
                        local_date,
                        tuple(record.article.id for record in rendered_records),
                        draft,
                    )
                )
                result = attempt.result
                persisted = self._database.finish_editorial(
                    edition.id,
                    title=result.title if result else None,
                    introduction=result.introduction if result else None,
                    ordered_article_ids=result.ordered_article_ids if result else (),
                    error_code=attempt.error_code,
                )
            elif persisted["status"] == "running":
                persisted = self._database.finish_editorial(
                    edition.id,
                    title=None,
                    introduction=None,
                    ordered_article_ids=(),
                    error_code="editorial_interrupted",
                )
            if persisted["status"] == "succeeded":
                order = tuple(persisted["ordered_article_ids"])
                by_id = {
                    record.article.id: (record, source)
                    for record, source in zip(rendered_records, frozen_sources, strict=True)
                }
                rendered_records = [by_id[article_id][0] for article_id in order]
                frozen_sources = tuple(by_id[article_id][1] for article_id in order)
                editorial_title = str(persisted["title"])
                editorial_introduction = str(persisted["introduction"])
            else:
                degraded = True
            edition = self._database.get_edition_run(edition.id)
        rendered = _render_edition(
            topic,
            local_date,
            rendered_records,
            content_sources=frozen_sources,
            degraded=degraded,
            editorial_title=editorial_title,
            editorial_introduction=editorial_introduction,
        )
        encoded = rendered.encode("utf-8")
        max_rendered_bytes = _positive_limit(
            topic.safety_limits, "max_rendered_bytes", maximum=10_000_000
        )
        if len(encoded) > max_rendered_bytes:
            raise ValueError("rendered edition exceeds max_rendered_bytes")
        digest = hashlib.sha256(encoded).hexdigest()

        if edition.status in {"frozen", "editorial"}:
            _atomic_write(artifact_path, rendered)
            edition = self._database.transition_edition_run(
                edition.id,
                status="degraded" if degraded else "rendered",
                translated_count=sum(
                    1 for record in rendered_records if record.translation.status == "succeeded"
                ),
                degraded_reason_code=(
                    str(persisted["error_code"])
                    if self._editorial_service is not None
                    and persisted
                    and persisted["status"] == "fallback"
                    else ("translation_incomplete" if degraded else None)
                ),
                artifact_path=artifact_path,
                artifact_sha256=digest,
            )
        elif edition.status in {"rendered", "degraded"}:
            if edition.artifact_sha256 != digest or edition.artifact_path != artifact_path:
                raise ValueError(
                    "existing edition artifact metadata does not match deterministic output"
                )
            if not artifact_path.is_file() or artifact_path.read_bytes() != encoded:
                _atomic_write(artifact_path, rendered)
        else:
            raise ValueError(f"edition cannot be built from status: {edition.status}")

        delivery = self._database.create_delivery_outbox_item(
            edition_run_id=edition.id,
            channel="feishu",
            target_ref=target_ref,
            idempotency_key=f"{local_date}:{topic.key}:v{topic.version}:feishu",
            artifact_path=artifact_path,
            payload_sha256=digest,
        )
        edition = self._database.get_edition_run(edition.id)
        return EditionBuildResult(
            edition=edition,
            delivery=delivery,
            artifact_path=artifact_path,
            artifact_sha256=digest,
            article_count=len(rendered_records),
            degraded=degraded,
        )

    def _select(self, topic: TopicProfileRecord, *, deadline_at: str) -> list[ExportArticleRecord]:
        selection = topic.selection
        deadline = _utc_timestamp(deadline_at)
        published_after = (deadline - timedelta(hours=topic.lookback_hours)).isoformat()
        include_untranslated = _boolean(selection, "include_untranslated", default=False)
        records = self._database.list_export_articles(
            target_language=self._target_language,
            translation_status="succeeded",
            sources=_string_tuple(selection, "sources"),
            categories=_string_tuple(selection, "categories"),
            published_after=published_after,
            published_before=deadline.isoformat(),
            include_untranslated=include_untranslated,
            sort_by="published_at",
            sort_descending=True,
        )
        keywords = list(_string_tuple(selection, "keywords"))
        content_keywords = list(_string_tuple(selection, "content_keywords"))
        if keywords or content_keywords:
            records = _filter_by_keywords(
                records,
                keywords=keywords,
                content_keywords=content_keywords,
                match_mode=_match_mode(selection),
            )
        if _boolean(selection, "dedupe_by_title", default=True):
            records = _dedupe_by_title(
                records, feed_priority=list(_string_tuple(selection, "feed_priority"))
            )
        return records[: _positive_limit(topic.safety_limits, "max_candidates", maximum=1000)]

    def _fit_render_limit(
        self,
        topic: TopicProfileRecord,
        records: list[ExportArticleRecord],
        *,
        local_date: str,
    ) -> list[ExportArticleRecord]:
        limit = _positive_limit(topic.safety_limits, "max_rendered_bytes", maximum=10_000_000)
        fitted = list(records)
        while fitted:
            degraded = any(
                record.translation.status != "succeeded"
                or _content_source(record).startswith("original_")
                for record in fitted
            )
            rendered_size = len(
                _render_edition(topic, local_date, fitted, degraded=degraded).encode("utf-8")
            )
            if rendered_size <= limit:
                return fitted
            fitted.pop()
        empty = _render_edition(topic, local_date, [], degraded=False).encode("utf-8")
        if len(empty) > limit:
            raise ValueError("max_rendered_bytes is too small for an empty edition")
        return fitted

    def _records_for_frozen_items(self, edition_run_id: int) -> list[ExportArticleRecord]:
        items = self._database.edition_items(edition_run_id)
        if not items:
            return []
        records = self._database.list_export_articles(
            target_language=self._target_language,
            translation_status="succeeded",
            include_untranslated=True,
            sort_by="published_at",
            sort_descending=True,
        )
        by_id = {record.article.id: record for record in records}
        try:
            return [by_id[item.article_id] for item in items]
        except KeyError as error:
            raise ValueError("frozen edition article is no longer available") from error

    def _artifact_path(self, topic: TopicProfileRecord, local_date: str) -> Path:
        return self._output_directory / f"{local_date}-{topic.key}-v{topic.version}.md"

    def _result_from_existing(
        self,
        topic: TopicProfileRecord,
        edition: EditionRunRecord,
        delivery: DeliveryOutboxRecord,
        *,
        target_ref: str,
    ) -> EditionBuildResult:
        if delivery.target_ref != target_ref:
            raise ValueError("existing edition is bound to a different target")
        if edition.artifact_path is None or edition.artifact_sha256 is None:
            raise ValueError("queued edition is missing artifact metadata")
        records = self._records_for_frozen_items(edition.id)
        sources = tuple(item.content_source for item in self._database.edition_items(edition.id))
        editorial = self._database.editorial_result(edition.id)
        title = None
        introduction = None
        if editorial and editorial["status"] == "succeeded":
            by_id = {
                record.article.id: (record, source)
                for record, source in zip(records, sources, strict=True)
            }
            order = tuple(editorial["ordered_article_ids"])
            records = [by_id[article_id][0] for article_id in order]
            sources = tuple(by_id[article_id][1] for article_id in order)
            title = str(editorial["title"])
            introduction = str(editorial["introduction"])
        rendered = _render_edition(
            topic,
            edition.local_date,
            records,
            content_sources=sources,
            degraded=edition.degraded_reason_code is not None,
            editorial_title=title,
            editorial_introduction=introduction,
        )
        encoded = rendered.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != edition.artifact_sha256 or digest != delivery.payload_sha256:
            raise ValueError("queued edition artifact hash is not reproducible")
        if not edition.artifact_path.is_file() or edition.artifact_path.read_bytes() != encoded:
            _atomic_write(edition.artifact_path, rendered)
        return EditionBuildResult(
            edition=edition,
            delivery=delivery,
            artifact_path=edition.artifact_path,
            artifact_sha256=edition.artifact_sha256,
            article_count=edition.candidate_count,
            degraded=edition.degraded_reason_code is not None,
        )


def _render_edition(
    topic: TopicProfileRecord,
    local_date: str,
    records: list[ExportArticleRecord],
    *,
    content_sources: tuple[str, ...] | None = None,
    degraded: bool,
    editorial_title: str | None = None,
    editorial_introduction: str | None = None,
) -> str:
    suffix = "（降级版）" if degraded else ""
    heading = editorial_title or f"{topic.name} — {local_date}"
    lines = [f"# {escape_inline_text(heading)}{suffix}", ""]
    if editorial_introduction:
        lines.extend([escape_inline_text(editorial_introduction), ""])
    if not records:
        lines.extend(["今日无符合主题的更新。", ""])
    sources = content_sources or tuple(_content_source(record) for record in records)
    if len(sources) != len(records):
        raise ValueError("content provenance does not match edition records")
    for record, content_source in zip(records, sources, strict=True):
        title = _title(record)
        content = _content(record, content_source)
        lines.extend(
            [
                f"## {escape_inline_text(title)}",
                "",
                f"- 来源: {escape_inline_text(record.feed_name)}",
                f"- 发布时间: {escape_inline_text(record.article.published_at or '未知')}",
                f"- 内容来源: {_content_source_label(content_source)}",
                f"- 原文链接: {safe_markdown_url(record.article.canonical_url)}",
                "",
                content,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _title(record: ExportArticleRecord) -> str:
    if record.translation.status == "succeeded" and record.translation.title:
        return record.translation.title
    return record.article.title


def _content_source(record: ExportArticleRecord) -> str:
    if record.extraction and record.extraction.translated_content:
        return "extracted_full_text"
    if record.translation.status == "succeeded" and record.translation.content:
        return "rss_content"
    if record.translation.status == "succeeded" and record.translation.summary:
        return "rss_summary"
    if record.article.content:
        return "original_rss_content"
    return "original_rss_summary"


def _content(record: ExportArticleRecord, source: str) -> str:
    if source == "extracted_full_text":
        return record.extraction.translated_content or ""
    if source == "rss_content":
        return record.translation.content or ""
    if source == "rss_summary":
        return record.translation.summary or ""
    if source == "original_rss_content":
        return record.article.content or ""
    return record.article.summary or ""


def _content_source_label(source: str) -> str:
    return {
        "extracted_full_text": "已提取全文",
        "rss_content": "RSS 全文",
        "rss_summary": "RSS 摘要",
        "original_rss_content": "RSS 全文（原文）",
        "original_rss_summary": "RSS 摘要（原文）",
    }[source]


def _positive_limit(values: object, key: str, *, maximum: int) -> int:
    if not isinstance(values, dict) or isinstance(values.get(key), bool):
        raise ValueError(f"topic safety_limits.{key} must be an integer")
    value = values.get(key)
    if not isinstance(value, int) or value < 1 or value > maximum:
        raise ValueError(f"topic safety_limits.{key} must be between 1 and {maximum}")
    return value


def _string_tuple(values: object, key: str) -> tuple[str, ...]:
    if not isinstance(values, dict):
        raise ValueError("topic selection must be an object")
    value = values.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"topic selection.{key} must be a string array")
    return tuple(value)


def _boolean(values: object, key: str, *, default: bool) -> bool:
    if not isinstance(values, dict):
        raise ValueError("topic selection must be an object")
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"topic selection.{key} must be boolean")
    return value


def _match_mode(values: object) -> str:
    if not isinstance(values, dict):
        raise ValueError("topic selection must be an object")
    value = values.get("keyword_match", "any")
    if value not in {"any", "all", "groups"}:
        raise ValueError("topic selection.keyword_match is invalid")
    return str(value)


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline_at must include a UTC offset")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("deadline_at must be a normalized UTC timestamp")
    return normalized


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
