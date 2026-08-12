---
name: rss-zen
version: 2.0.0
description: RSS-Zen 本地多语言 RSS 工作流操作技能。同步 RSS/Atom 订阅源到 SQLite，翻译为简体中文（支持 Google/LibreTranslate/MyMemory/OpenAI 兼容提供商），通过 AnySearch 获取全文，导出 Markdown 合集。触发词：订阅源、RSS、feed、同步/刷新订阅、翻译文章、全文提取、导出/生成 Markdown 合集、近 N 日文章、印太/台海新闻合集、处理状态/status、备份数据库、doctor 诊断、rss-zen 命令。当用户需要同步订阅源、翻译文章、提取全文、导出 Markdown、查看处理状态、诊断故障（doctor）、备份数据库，或修改 rss-zen.toml 配置时使用。
compatibility: Python 3.13+，需要 uv；项目根目录含 rss-zen.toml（生产配置不入库）
---

# RSS-Zen 工作流

本地、跨平台的多语言 RSS 订阅工作流：SQLite 存储 → 翻译成简体中文 → AnySearch 全文提取 → Markdown 导出。

## 环境

- 需要 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。
- 首次使用：`uv sync --locked`（从 `uv.lock` 锁定依赖）。
- 配置文件默认 `rss-zen.toml`（TOML 或 YAML，schema 相同），参考 `example.rss-zen.toml`。
- 所有命令用 `uv run rss-zen <command> --config <config>` 执行。

## 状态前置检查（重要约定）

**在运行任何耗时操作（sync/extract/translate/export）之前，先看数据现状：**

```bash
uv run rss-zen status --json --config rss-zen.toml
```

解析要点：
- `counts.pending_translation` / `counts.failed_translation` / `counts.failed_extraction` 决定是否需要补翻/重试。
- `last_sync.latest_feed_success` 是数据新鲜度指标：如果很久没同步，先 `sync` 再导出。
- `last_sync.stale_feeds` 表示启用的源从未成功同步过（可能源已失效）。

**排障第一命令是 `doctor`（不修改任何状态）：**

```bash
uv run rss-zen doctor --config rss-zen.toml        # 文本
uv run rss-zen doctor --json --config rss-zen.toml # 机器可读 {healthy, checks[]}
```

它检查：配置 schema、密钥环境变量是否设置（只报 set/unset 不泄露值）、数据库完整性、
启用的源是否曾成功、处理计数、备份新鲜度。

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
uv run rss-zen translate --status failed --config rss-zen.toml   # 批量重试所有失败
uv run rss-zen translate --status pending --config rss-zen.toml  # 批量重试所有待处理

# 4) 全文提取（显式操作，普通 sync 不做提取）
uv run rss-zen extract --article-id 42 --config rss-zen.toml
uv run rss-zen extract --source "Feed name" --config rss-zen.toml
uv run rss-zen extract --since 2d --config rss-zen.toml          # 近 2 天发布的文章
uv run rss-zen extract --without-extraction --config rss-zen.toml # 补提取缺全文的

# 5) 导出 Markdown 合集（按配置的 export profile）
uv run rss-zen export --config rss-zen.toml        # 无参数：列出可用 profiles
uv run rss-zen export daily --config rss-zen.toml  # 导出指定 profile

# 5b) 临时按时间导出（不改配置文件，--since/--until 支持 2d/12h/1w 或 ISO 时间）
uv run rss-zen export daily --since 2d --config rss-zen.toml

# 5c) 列出文章（含翻译/提取状态，--since/--until 同支持 2d 格式）
uv run rss-zen list --since 2d --config rss-zen.toml
uv run rss-zen list --source "Feed" --status succeeded --limit 20 --config rss-zen.toml

# 6) 查看状态 / 诊断 / 备份
uv run rss-zen status --config rss-zen.toml
uv run rss-zen doctor --config rss-zen.toml
uv run rss-zen backup --config rss-zen.toml
```

常用辅助脚本：`scripts/workflow.sh`（一键 sync → 可选补翻/提取 → export → status）、
`.pi/skills/rss-zen/scripts/recent.sh <days>`（一键导出近 N 日文章）。

## 任务 → 命令速查表（Agent 优先用命令，不要直接改配置/SQL）

| 想做什么 | 命令 |
|---------|------|
| 同步订阅源 | `uv run rss-zen sync --config rss-zen.toml` |
| 查近 N 日文章 | `uv run rss-zen list --since 2d --config rss-zen.toml` |
| 查某源的文章 | `uv run rss-zen list --source "Feed name" --config rss-zen.toml` |
| 查翻译状态 | `uv run rss-zen list --status succeeded --config rss-zen.toml` |
| 查翻译失败 | `uv run rss-zen list --status failed --config rss-zen.toml` |
| 批量补翻失败/待处理 | `uv run rss-zen translate --status failed --config rss-zen.toml` |
| 导出近 2 日文章 | `uv run rss-zen export daily --since 2d --config rss-zen.toml` |
| 列出可用导出 profile | `uv run rss-zen export --config rss-zen.toml` |
| 看处理状态 | `uv run rss-zen status --config rss-zen.toml` |
| 机器可读状态 | `uv run rss-zen status --json --config rss-zen.toml` |
| 机器可读文章列表 | `uv run rss-zen list --since 2d --json --config rss-zen.toml` |
| 一键诊断（不修改状态） | `uv run rss-zen doctor --config rss-zen.toml` |
| 重译某文章 | `uv run rss-zen translate --article-id 42 --config rss-zen.toml` |
| 全文提取（近 2 天） | `uv run rss-zen extract --since 2d --config rss-zen.toml` |
| 补提取缺全文的 | `uv run rss-zen extract --without-extraction --config rss-zen.toml` |
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
# headers = { "User-Agent" = "FreshRSS/1.23.1 (Linux)" }   # 可选：自定义请求头
# fetcher = "curl"  # 可选：改用系统 curl 抓取（反爬源用，见下）

[[exports]]
name = "daily"
output_path = "exports/daily.md"
fields = ["source_name", "published_at", "url", "content"]
content_fallback = ["full_text", "rss_content", "summary"]
```

要点：
- 翻译 provider 至少配置 1 个；`endpoint` 必须 HTTPS；`openai_compatible` 需要 `model`。
- `google` kind 无需 endpoint 和 API key；长文本自动按 5000 字符分块（实际 chunk 上限 4999，
  低于 deep_translator 的严格 `len < 5000` 校验）。
- 密钥通过环境变量注入（`api_key_env`），不写进配置文件/数据库。
- 导出的 `translation_status` 过滤默认 `"succeeded"`——未翻译成功的文章不会出现在导出里（除非 profile 开了 `include_untranslated`）。
- 导出 profile 支持关键词过滤（`keywords`/`content_keywords`）、标题去重（`dedupe_by=title` + `feed_priority`）。
- **反爬源（Nitter/Twitter 账号 RSS）**：用 `fetcher = "curl"` + 自定义 UA。
  `rss.xcancel.com/<handle>/rss` 按 UA 白名单放行（FreshRSS/TT-RSS 可过）且对 Python
  TLS 指纹敏感，httpx 会被降级为占位 feed，必须走 curl 抓取器。轮询间隔建议 ≥60 分钟以
   ️ 规避 429 限流。账号与官方 RSS 重复时以官方 RSS 为准（如 @DefenseNews↔Defense News 源）。

## 故障诊断

**第一步：`rss-zen doctor`** 定位配置/密钥/数据库/源健康问题，不要盲目重跑整个 sync。
详细错误码 → 含义 → 处理方式见 [references/errors.md](references/errors.md)。

决策树：

| 症状 | 检查 | 处理 |
|------|------|------|
| `translate --status failed` 仍失败 | `status --json` 看 `failed_translation` 计数和错误聚合 | 按 errors.md 的翻译错误处理（429 换 google kind；网络问题稍后重试） |
| 导出 articles=0 | 文章翻译状态非 `succeeded` | 用 `translate --status failed/pending` 补翻，或检查 profile 的关键词过滤是否过严 |
| 某个源一直失败 | `status --json` 看该源 `last_error` | `feed_http_403/404`、`feed_host_unresolvable` 通常意味着源失效，考虑禁用/更换 |
| 提取一直 `anysearch_exact_source_not_found` | 这是保守拒绝，属正常 | 换 AnySearch 之外的方式获取全文，或接受 RSS 摘要 |
| 配置改了但不生效 | 检查 `doctor` 的 configuration 检查 | 按报错路径修正；密钥缺失会报 `env XXX is not set` |
| 数据库损坏怀疑 | `doctor` 的 database 检查（PRAGMA quick_check） | 用 `backup` 恢复最近备份 |

## 技能协同

- **全文提取**：rss-zen 内置 AnySearch 提取（`extract` 命令），不需要单独调 anysearch skill。
- **联网搜索**（查证/找源/搜最新资讯）：用 `byted-web-search` 或 `anysearch` skill，与 rss-zen 的数据管道互补。
- **源管理**：用户要求添加/删除订阅源时，编辑 `rss-zen.toml` 的 `[[feeds]]` 段后执行
  `rss-zen init`（同步到数据库）再 `sync`；失效源设 `enabled = false` 而不是删除，保留历史数据。
- **微信/飞书交付**：导出的 `exports/*.md` 可直接作为附件或内容发送。

## 数据库（参考）

SQLite 表结构、字段与查询示例见 [references/database.md](references/database.md)。核心表：`feeds`、`articles`、`translations`、`extractions`、`export_runs`。

## 开发与测试

```bash
uv run pytest -q                    # 全部测试
uv run ruff check src/ tests/       # lint
```

> ⚠️ 提交 git 更改时严禁包含生产数据：`data/*.sqlite3`、`rss-zen.toml`、`exports/`、`*.env` 均已 gitignore；只提交源码/测试/文档/脚本。
