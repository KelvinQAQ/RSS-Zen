"""Stable, atomic JSON reports for explicit operator batch commands."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from rss_zen.errors import AppError

REPORT_SCHEMA_VERSION = 1


def write_json_report(path: Path, report: Mapping[str, object]) -> None:
    """Write one JSON report atomically without leaving partial output behind."""
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise AppError(
            "report_write_failed", "unable to write batch JSON report", cause=error
        ) from error
