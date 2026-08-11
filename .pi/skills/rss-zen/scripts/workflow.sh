#!/usr/bin/env bash
# RSS-Zen 一键工作流：init → sync → export → status
# 用法: ./scripts/workflow.sh [config.toml] [export_profile]
# 默认 config=rss-zen.toml, export_profile=daily
set -euo pipefail

CONFIG="${1:-rss-zen.toml}"
PROFILE="${2:-daily}"

if [ ! -f "$CONFIG" ]; then
  echo "错误: 配置文件不存在: $CONFIG" >&2
  exit 1
fi

echo "==> [1/4] 初始化数据库"
uv run rss-zen init --config "$CONFIG"

echo "==> [2/4] 同步订阅源"
uv run rss-zen sync --config "$CONFIG" || { echo "同步失败（可忽略单个 feed 错误）" >&2; }

echo "==> [3/4] 导出 Markdown 合集: $PROFILE"
uv run rss-zen export "$PROFILE" --config "$CONFIG" || true

echo "==> [4/4] 处理状态"
uv run rss-zen status --config "$CONFIG"

echo "==> 完成"
