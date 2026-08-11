"""Profile-driven Markdown export from SQLite article records."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rss_zen.db import Database, ExportArticleRecord
from rss_zen.markdown import (
    apply_preprocessing,
    escape_inline_text,
    escape_link_label,
    safe_markdown_url,
)
from rss_zen.models import ExportProfile


@dataclass(frozen=True)
class ExportResult:
    """The completed Markdown file and its audit record."""

    output_path: Path
    article_count: int
    export_run_id: int


class MarkdownExporter:
    """Render one named export profile without evaluating arbitrary templates."""

    def __init__(self, database: Database, *, target_language: str = "zh-CN") -> None:
        self._database = database
        self._target_language = target_language

    def export_profile(self, profile: ExportProfile) -> ExportResult:
        """Query, render, atomically write, and record one Markdown export."""
        filters = profile.filters
        articles = self._database.list_export_articles(
            target_language=self._target_language,
            translation_status=filters.translation_status,
            sources=tuple(filters.sources),
            categories=tuple(filters.categories),
            published_after=filters.published_after,
            published_before=filters.published_before,
            require_full_text=filters.require_full_text,
            sort_by=profile.sort_by,
            sort_descending=profile.sort_descending,
        )
        rendered = _render_document(profile, articles)
        _atomic_write(profile.output_path, rendered)
        export_run = self._database.record_export_run(
            profile_name=profile.name,
            output_path=profile.output_path,
            filters={
                "sources": filters.sources,
                "categories": filters.categories,
                "published_after": filters.published_after,
                "published_before": filters.published_before,
                "translation_status": filters.translation_status,
                "require_full_text": filters.require_full_text,
            },
            article_count=len(articles),
            status="succeeded",
        )
        return ExportResult(profile.output_path, len(articles), export_run.id)


def _render_document(profile: ExportProfile, articles: list[ExportArticleRecord]) -> str:
    lines = [f"# {profile.title}", "", "## 目录", ""]
    for record in articles:
        title = _field_values(record, profile)["title"] or "Untitled article"
        lines.append(f"- [{escape_link_label(title)}](#article-{record.article.id})")
    for record in articles:
        values = _field_values(record, profile)
        title = escape_inline_text(values["title"] or "Untitled article")
        lines.extend(["", f'<a id="article-{record.article.id}"></a>', "", f"## {title}", ""])
        for field in profile.fields:
            value = apply_preprocessing(values[field], field, profile.preprocess)
            if not value:
                continue
            lines.extend(_render_field(field, value))
    return "\n".join(lines).rstrip() + "\n"


def _field_values(record: ExportArticleRecord, profile: ExportProfile) -> dict[str, str]:
    article = record.article
    translation = record.translation
    extraction = record.extraction
    return {
        "title": translation.title or article.title,
        "summary": translation.summary or "",
        "content": _content_value(record, profile.content_fallback),
        "source_name": record.feed_name,
        "published_at": article.published_at or "",
        "author": article.author or "",
        "categories": ", ".join(article.categories),
        "url": safe_markdown_url(article.canonical_url),
        "source_language": article.source_language or article.detected_language or "",
        "extraction_status": extraction.status if extraction else "not_requested",
    }


def _content_value(record: ExportArticleRecord, fallback: list[str]) -> str:
    for source in fallback:
        if source == "full_text" and record.extraction and record.extraction.translated_content:
            return record.extraction.translated_content
        if source == "rss_content" and record.translation.content:
            return record.translation.content
        if source == "summary" and record.translation.summary:
            return record.translation.summary
    return ""


def _render_field(field: str, value: str) -> list[str]:
    labels = {
        "title": "标题",
        "summary": "摘要",
        "content": "正文",
        "source_name": "来源",
        "published_at": "发布日期",
        "author": "作者",
        "categories": "分类",
        "url": "原文链接",
        "source_language": "原始语言",
        "extraction_status": "全文状态",
    }
    if field in {"content", "summary"}:
        return [f"### {labels[field]}", "", value, ""]
    return [f"- {labels[field]}: {escape_inline_text(value)}"]


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
