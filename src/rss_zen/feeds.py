"""Feed reconciliation and OPML import."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit
from xml.etree import ElementTree

from rss_zen.db import Database, FeedInput
from rss_zen.errors import AppError
from rss_zen.models import FeedConfig


@dataclass(frozen=True)
class FeedChangeSet:
    """Counts from a feed reconciliation operation."""

    imported: int = 0
    updated: int = 0
    skipped: int = 0


def normalize_feed_url(url: str) -> str:
    """Normalize a feed URL enough to make local reconciliation stable."""
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise AppError("invalid_feed_url", "feed URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise AppError("invalid_feed_url", "feed URL must not include URL credentials")
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    normalized = SplitResult(parsed.scheme.lower(), netloc, path, parsed.query, "")
    return urlunsplit(normalized)


def reconcile_config_feeds(database: Database, feeds: list[FeedConfig]) -> FeedChangeSet:
    """Apply human-maintained configuration as the authoritative feed metadata."""
    imported = 0
    updated = 0
    for feed in feeds:
        url = normalize_feed_url(feed.url)
        previous = database.get_feed_by_url(url)
        database.upsert_feed(
            FeedInput(
                name=feed.name,
                url=url,
                categories=tuple(feed.categories),
                language=feed.language,
                poll_interval_minutes=feed.poll_interval_minutes,
                enabled=feed.enabled,
                origin="config",
            )
        )
        if previous is None:
            imported += 1
        else:
            updated += 1
    return FeedChangeSet(imported=imported, updated=updated)


def import_opml_file(database: Database, path: Path) -> FeedChangeSet:
    """Idempotently import RSS outline nodes from an OPML document."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        raise AppError("invalid_opml", f"unable to parse OPML file: {path}", cause=error) from error

    body = root.find("body")
    if body is None:
        raise AppError("invalid_opml", "unable to parse OPML file: document has no body")

    imported = 0
    updated = 0
    skipped = 0
    for name, url, categories in _opml_feeds(body, ()):
        if url is None:
            skipped += 1
            continue
        previous = database.get_feed_by_url(url)
        database.upsert_feed(FeedInput(name=name, url=url, categories=categories, origin="opml"))
        if previous is None:
            imported += 1
        else:
            updated += 1
    return FeedChangeSet(imported=imported, updated=updated, skipped=skipped)


def _opml_feeds(
    element: ElementTree.Element, categories: tuple[str, ...]
) -> list[tuple[str, str | None, tuple[str, ...]]]:
    entries: list[tuple[str, str | None, tuple[str, ...]]] = []
    for outline in element.findall("outline"):
        xml_url = outline.get("xmlUrl")
        outline_type = outline.get("type", "").casefold()
        label = _outline_label(outline)
        children = list(outline.findall("outline"))

        if xml_url is not None:
            if outline_type == "html":
                entries.append((label, None, categories))
            else:
                try:
                    entries.append((label, normalize_feed_url(xml_url), categories))
                except AppError:
                    entries.append((label, None, categories))

        child_categories = categories + (label,) if children and label else categories
        entries.extend(_opml_feeds(outline, child_categories))
    return entries


def _outline_label(outline: ElementTree.Element) -> str:
    return outline.get("title") or outline.get("text") or "Unnamed feed"
