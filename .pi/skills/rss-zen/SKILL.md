---
name: rss-zen
description: RSS-Zen 本地多语言 RSS 工作流操作技能。同步 RSS/Atom 订阅源到 SQLite，翻译为简体中文（支持 Google/LibreTranslate/MyMemory/OpenAI 兼容提供商），通过 AnySearch 获取全文，导出 Markdown 合集。当用户需要同步订阅源、翻译文章、提取全文、导出 Markdown、查看处理状态、备份数据库、排查同步/翻译/导出故障，或修改 rss-zen.toml 配置时使用。
---

# RSS-Zen 工作流

本地、跨平台的多语言 RSS 订阅工作流：SQLite 存储 → 翻译成简体中文 → AnySearch 全文提取 → Markdown 导出。

## 环境

- 需要 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。
- 首次使用：`uv sync --locked`（从 `uv.lock` 锁定依赖）。
- 配置文件默认 `rss-zen.toml`（TOML 或 YAML，schema 相同），参考 `example.rss-zen.toml`。
- 所有命令用 `uv run rss-zen <command> --config <config>` 执行。

## 完整工作流（日常操作顺序）

```bash
# 0) 首次：同步依赖 + 从示例复制配置并编辑
uv sync --locked
cp example.rss-zen.toml rss-zen.toml

# 1) 初始化数据库（建表 + 导入配置里的 feeds）
uv run rss-zen init --config rss-zen.toml

# 2) 同步所有订阅源（或单个）
uv run rss-zen sync --config rss-zen.toml
uv run rss-zen sync --source "Feed name" --config rss-zen.toml

# 3) 翻译（正常 sync 时自动翻译；也可显式重试）
uv run rss-zen translate --article-id 42 --config rss-zen.toml
uv run rss-zen translate --source "Feed name" --config rss-zen.toml

# 4) 全文提取（显式操作，普通 sync 不做提取）
uv run rss-zen extract --article-id 42 --config rss-zen.toml
uv run rss-zen extract --source "Feed name" --config rss-zen.toml

# 5) 导出 Markdown 合集（按配置的 export profile）
uv run rss-zen export daily --config rss-zen.toml

# 5b) 临时按时间导出（不改配置文件，--since/--until 支持 2d/12h/1w 或 ISO 时间）
uv run rss-zen export daily --since 2d --config rss-zen.toml

# 5c) 列出文章（含翻译/提取状态，--since/--until 同支持 2d 格式）
uv run rss-zen list --since 2d --config rss-zen.toml
uv run rss-zen list --source "Feed" --status succeeded --limit 20 --config rss-zen.toml

# 6) 查看状态 / 备份
uv run rss-zen status --config rss-zen.toml
uv run rss-zen backup --config rss-zen.toml
```

常用辅助脚本：`scripts/workflow.sh`（一键 init → sync → export → status）、`scripts/recent.sh <days>`（一键导出近 N 日文章）。

## 任务 → 命令速查表（Agent 优先用命令，不要直接改配置/SQL）

| 想做什么 | 命令 |
|---------|------|
| 同步订阅源 | `uv run rss-zen sync --config rss-zen.toml` |
| 查近 N 日文章 | `uv run rss-zen list --since 2d --config rss-zen.toml` |
| 查某源的文章 | `uv run rss-zen list --source "Feed name" --config rss-zen.toml` |
| 查翻译状态 | `uv run rss-zen list --status succeeded --config rss-zen.toml` |
| 查翻译失败 | `uv run rss-zen list --status failed --config rss-zen.toml` |
| 导出近 2 日文章 | `uv run rss-zen export daily --since 2d --config rss-zen.toml` |
| 看处理状态 | `uv run rss-zen status --config rss-zen.toml` |
| 机器可读状态 | `uv run rss-zen status --json --config rss-zen.toml` |
| 机器可读文章列表 | `uv run rss-zen list --since 2d --json --config rss-zen.toml` |
| 重译某文章 | `uv run rss-zen translate --article-id 42 --config rss-zen.toml` |
| 全文提取 | `uv run rss-zen extract --source "Feed name" --config rss-zen.toml` |
| 备份数据库 | `uv run rss-zen backup --config rss-zen.toml` |

时间格式：`--since`/`--until` 支持相对时长（`2d`=2天、`12h`=12小时、`1w`=1周）或 ISO 时间。用 `--json` 可拿到结构化输出供 Agent 精确解析。

不要直接改 `rss-zen.toml` 或手动跑 SQLite 查询，除非用户明确要求——用上面的 CLI 命令更安全、可移植。对于“近 N 日文章”这类临时任务，用 `export ... --since` 的临时过滤，不要改配置文件里的 `published_after`。

## 配置要点（rss-zen.toml）

```toml
[database]
path = "data/rss-zen.sqlite3"      # 相对路径基于配置文件所在目录

[translation]
target_language = "zh-CN"

[[translation.providers]]          # 按声明顺序尝试，失败回退到下一个
name = "google"
kind = "google"                    # 免费，走 deep_translator 的 Google 网页接口，无需 API key
# 其他 kind：libretranslate / mymemory / openai_compatible

[[feeds]]
name = "Example feed"
url = "https://example.invalid/rss.xml"
categories = ["defense"]
poll_interval_minutes = 15
language = "en"

[[exports]]
name = "daily"
output_path = "exports/daily.md"
fields = ["source_name", "published_at", "url", "content"]
content_fallback = ["full_text", "rss_content", "summary"]
```

要点：
- 翻译 provider 至少配置 1 个；`endpoint` 必须 HTTPS；`openai_compatible` 需要 `model`。
- `google` kind 无需 endpoint 和 API key；长文本自动按 5000 字符分块。
- 密钥通过环境变量注入（`api_key_env`），不写进配置文件/数据库。
- 导出的 `translation_status` 过滤默认 `"succeeded"`——未翻译成功的文章不会出现在导出里。

## 故障排查

| 症状 | 原因与处理 |
|------|-----------|
| `invalid configuration at ...` | 配置 schema 校验失败，按报错路径修正（如 provider 缺 endpoint/model） |
| `translation_network_error` | 翻译接口不可达或超时；检查网络、endpoint、API key 环境变量 |
| `translation_http_429` | MyMemory 等免费接口限流；改用 `google` kind 或等待配额恢复 |
| `translation_provider_error` | Google 网页接口偶发失败；重试（retryable） |
| `anysearch_exact_source_not_found` | AnySearch 未返回与文章 URL 完全匹配的结果；提取被保守拒绝 |
| 导出 articles=0 | 文章翻译状态非 `succeeded`；检查 `status`，用 `translate` 补翻或调整导出 filter |
| feed 同步失败 | 单个 feed 失败被隔离，不影响其他 feed；用 `status` 看 `last_error_code` |

## 数据库（参考）

SQLite 表结构、字段与查询示例见 [references/database.md](references/database.md)。核心表：`feeds`、`articles`、`translations`、`extractions`、`export_runs`。

## 开发与测试

```bash
uv run pytest -q                    # 全部测试
uv run ruff check src/ tests/       # lint
```
