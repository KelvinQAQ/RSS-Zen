#!/usr/bin/env python3
"""Reject UTF-8 BOMs in repository-controlled text files."""

from __future__ import annotations

import sys
from pathlib import Path

SUFFIXES = {".md", ".py", ".sh", ".toml", ".yaml", ".yml", ".service", ".timer", ".conf"}
EXCLUDED_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".test-tmp", ".e2e"}


def main() -> int:
    offenders = [
        path
        for path in Path(".").rglob("*")
        if path.is_file()
        and not EXCLUDED_PARTS.intersection(path.parts)
        and (path.suffix in SUFFIXES or path.name.startswith("rss-zen"))
        and path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    if offenders:
        print("UTF-8 BOM is not permitted in repository-controlled files:", file=sys.stderr)
        print("\n".join(str(path) for path in offenders), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
