#!/usr/bin/env bash
# RSS-Zen 一键工作流：sync → (可选补翻) → export → status
# 用法: ./scripts/workflow.sh [config.toml] [export_profile] [--retry]
# 默认 config=rss-zen.toml, export_profile=daily
# --retry  在 sync 后批量重试失败/待处理的翻译
set -euo pipefail

CONFIG="${1:-rss-zen.toml}"
PROFILE="${2:-daily}"
DO_RETRY=0
for arg in "${@:3}"; do
  if [[ "$arg" == "--retry" ]]; then DO_RETRY=1; fi
done

if [ ! -f "$CONFIG" ]; then
  echo "错误: 配置文件不存在: $CONFIG" >&2
  exit 1
fi

echo "==> 同步订阅源"
uv run rss-zen sync --config "$CONFIG" || { echo "同步失败（可忽略单个 feed 错误）" >&2; }

if [[ "$DO_RETRY" -eq 1 ]]; then
  echo "==> 补翻失败/待处理翻译"
  uv run rss-zen translate --status failed --config "$CONFIG" || true
  uv run rss-zen translate --status pending --config "$CONFIG" || true
fi

echo "==> 导出 Markdown 合集: $PROFILE"
uv run rss-zen export "$PROFILE" --config "$CONFIG" || true

echo "==> 处理状态"
uv run rss-zen status --config "$CONFIG"

echo "==> 完成"
