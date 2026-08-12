"""Profile-driven Markdown export from SQLite article records."""

from __future__ import annotations

import os
import re
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
            include_untranslated=filters.include_untranslated,
            sort_by=profile.sort_by,
            sort_descending=profile.sort_descending,
        )
        if filters.keywords or filters.content_keywords:
            articles = _filter_by_keywords(
                articles,
                keywords=filters.keywords,
                content_keywords=filters.content_keywords,
                require_all=filters.keyword_match == "all",
            )
        if profile.dedupe_by == "title":
            articles = _dedupe_by_title(
                articles, feed_priority=profile.feed_priority
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
                "include_untranslated": filters.include_untranslated,
                # Record the resolved keyword set for auditability.
                "keywords": sorted(set(keyword.casefold() for keyword in filters.keywords)),
                "content_keywords": sorted(
                    set(keyword.casefold() for keyword in filters.content_keywords)
                ),
                "keyword_match": filters.keyword_match,
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
    translated = translation.status == "succeeded"
    return {
        "title": (translation.title if translated else "") or article.title,
        "summary": (translation.summary if translated else "") or "",
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
    translation = record.translation
    extraction = record.extraction
    translated = translation.status == "succeeded"
    for source in fallback:
        if source == "full_text" and extraction and extraction.translated_content:
            return extraction.translated_content
        if source == "rss_content" and translated and translation.content:
            return translation.content
        if source == "summary" and translated and translation.summary:
            return translation.summary
    # Fall back to the original-language source text when no translated text exists.
    if record.article.content:
        return record.article.content
    return record.article.summary or ""


def _filter_by_keywords(
    records: list[ExportArticleRecord],
    *,
    keywords: list[str],
    content_keywords: list[str],
    require_all: bool,
) -> list[ExportArticleRecord]:
    """Keep records whose title/summary or body matches the relevance keywords.

    ``keywords`` are matched against the title and summary (in both the original
    and translated text, falling back to the original when untranslated);
    ``content_keywords`` are matched against the body text only. An article is
    kept when a title/summary keyword matches, or when a body keyword matches, so
    broad terms never scan the full body. ASCII keywords use word-boundary
    matching so that e.g. "pla" does not match "plans"; non-ASCII keywords (CJK)
    use plain substring matching.
    """
    title_patterns = [_keyword_pattern(keyword) for keyword in keywords]
    body_patterns = [_keyword_pattern(keyword) for keyword in content_keywords]
    kept: list[ExportArticleRecord] = []
    for record in records:
        title_haystacks = _record_title_haystacks(record)
        body_haystacks = _record_body_haystacks(record)
        if require_all:
            matched = all(
                any(re.search(pattern, haystack) for haystack in title_haystacks)
                for pattern in title_patterns
            ) and all(
                any(re.search(pattern, haystack) for haystack in body_haystacks)
                for pattern in body_patterns
            )
        else:
            matched = any(
                re.search(pattern, haystack)
                for pattern in title_patterns
                for haystack in title_haystacks
            ) or any(
                re.search(pattern, haystack)
                for pattern in body_patterns
                for haystack in body_haystacks
            )
        if matched:
            kept.append(record)
    return kept


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    if keyword.isascii():
        return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(keyword), re.IGNORECASE)


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().casefold())


def _dedupe_by_title(
    records: list[ExportArticleRecord], *, feed_priority: list[str]
) -> list[ExportArticleRecord]:
    """Collapse records sharing a normalized title, keeping the highest-priority feed.

    Feed priority is the position in ``feed_priority`` (lower index wins); feeds not
    listed are ordered after all listed feeds. Records are already sorted newest-first,
    so ties between equal-priority feeds keep the most recent.
    """
    priority = {name.casefold(): i for i, name in enumerate(feed_priority)}
    default_rank = len(priority)
    best: dict[str, ExportArticleRecord] = {}
    best_rank: dict[str, int] = {}
    for record in records:
        key = _norm_title(record.article.title)
        if not key:
            continue
        rank = priority.get(record.feed_name.casefold(), default_rank + 1)
        if key not in best or rank < best_rank[key]:
            best[key] = record
            best_rank[key] = rank
    return list(best.values())


def _record_title_haystacks(record: ExportArticleRecord) -> list[str]:
    article = record.article
    translation = record.translation
    translated = translation.status == "succeeded"
    haystacks = [article.title or ""]
    if translated and translation.title:
        haystacks.append(translation.title)
    if article.summary:
        haystacks.append(article.summary)
    if translated and translation.summary:
        haystacks.append(translation.summary)
    return haystacks


def _record_body_haystacks(record: ExportArticleRecord) -> list[str]:
    article = record.article
    translation = record.translation
    translated = translation.status == "succeeded"
    haystacks: list[str] = []
    if article.content:
        haystacks.append(article.content)
    if translated and translation.content:
        haystacks.append(translation.content)
    return haystacks


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
