#!/usr/bin/env bash
# RSS-Zen 一键导出近 N 日文章（不修改配置文件）
# 用法: ./scripts/recent.sh <days> [profile] [config]
# 默认 profile=daily, config=rss-zen.toml
set -euo pipefail

DAYS="${1:?用法: recent.sh <days> [profile] [config]}"
PROFILE="${2:-daily}"
CONFIG="${3:-rss-zen.toml}"

if ! command -v uv >/dev/null 2>&1; then
  echo "错误: 需要 uv (https://docs.astral.sh/uv)" >&2
  exit 1
fi

echo "==> 导出近 ${DAYS} 日文章 (profile=$PROFILE)"
uv run rss-zen export "$PROFILE" --since "${DAYS}d" --config "$CONFIG"

echo "==> 近 ${DAYS} 日文章列表"
uv run rss-zen list --since "${DAYS}d" --config "$CONFIG"