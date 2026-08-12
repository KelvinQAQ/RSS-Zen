#!/usr/bin/env bash
# One-shot RSS-Zen workflow: sync -> (optional) translate retry -> (optional)
# full-text extract -> export the China/Indo-Pacific collection.
#
# Usage:
#   bash scripts/workflow.sh [--days 3] [--extract] [--config rss-zen.toml]
#
# --days N   Number of days to look back (used for export --since). Default 3.
# --extract  Also run full-text extraction for articles without extracted text
#            before exporting (enables the full_text content fallback).
set -Eeuo pipefail

CONFIG="rss-zen.toml"
DAYS=3
DO_EXTRACT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days)
            DAYS="$2"
            shift 2
            ;;
        --extract)
            DO_EXTRACT=1
            shift
            ;;
        --config)
            CONFIG="$2"
            shift 2
            ;;
        *)
            echo "unknown option: $1" >&2
            exit 2
            ;;
    esac
done

cd "$(dirname "$0")/.."

echo "==> sync feeds"
uv run rss-zen sync --config "$CONFIG"

if [[ "$DO_EXTRACT" -eq 1 ]]; then
    echo "==> extract full text for articles without it"
    uv run rss-zen extract --without-extraction --config "$CONFIG" || true
fi

echo "==> export indo-pacific collection (last ${DAYS}d)"
uv run rss-zen export indopac --since "${DAYS}d" --config "$CONFIG"

echo "==> export recent2d collection"
uv run rss-zen export recent2d --since "2d" --config "$CONFIG" || true

echo "==> status"
uv run rss-zen status --config "$CONFIG"