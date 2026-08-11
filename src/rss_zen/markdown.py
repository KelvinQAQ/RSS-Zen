"""Restricted Markdown rendering helpers for export profiles."""

from __future__ import annotations

import re
from datetime import datetime

from markdownify import markdownify

from rss_zen.models import PreprocessStep


def apply_preprocessing(value: str, field: str, steps: list[PreprocessStep]) -> str:
    """Apply only declared, non-executable field transformations."""
    rendered = value
    for step in steps:
        if step.field != field:
            continue
        if step.operation == "strip_html":
            rendered = markdownify(rendered, heading_style="ATX").strip()
        elif step.operation == "collapse_whitespace":
            rendered = re.sub(r"\s+", " ", rendered).strip()
        elif step.operation == "truncate" and step.max_length is not None:
            rendered = rendered[: step.max_length]
        elif step.operation == "replace" and step.find is not None:
            rendered = rendered.replace(step.find, step.replacement or "")
        elif step.operation == "date_format" and step.format is not None:
            rendered = _format_date(rendered, step.format)
    return rendered


def escape_link_label(value: str) -> str:
    """Render untrusted text safely inside an inline Markdown link label."""
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\n", " ")
    )


def escape_inline_text(value: str) -> str:
    """Render untrusted metadata as one safe Markdown line."""
    return (
        value.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("#", "\\#")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def safe_markdown_url(value: str) -> str:
    """Allow only HTTPS links in Markdown output."""
    lowered = value.strip().casefold()
    return value if lowered.startswith("https://") else ""


def _format_date(value: str, output_format: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(output_format)
    except ValueError:
        return value
