---
name: rss-zen
version: 3.0.0
description: RSS-Zen 本地多语言 RSS 工作流操作技能。同步 RSS/Atom 订阅源到 SQLite，翻译为简体中文（支持 Google/LibreTranslate/MyMemory/OpenAI 兼容提供商），通过 AnySearch 获取全文，导出 Markdown 合集；支持订阅源管控、主题刊次编排、飞书投递。触发词：订阅源、RSS、feed、同步/刷新订阅、翻译文章、全文提取、导出/生成 Markdown 合集、近 N 日文章、印太/台海新闻合集、处理状态/status、备份数据库、doctor 诊断、rss-zen 命令。当用户需要同步订阅源、翻译文章、提取全文、导出 Markdown、查看处理状态、诊断故障（doctor）、备份数据库、管理订阅源（增删/启用禁用）、查看/创建主题刊次，或修改 rss-zen.toml 配置时使用。
compatibility: Python 3.13+，需要 uv；项目根目录含 rss-zen.toml（生产配置不入库）
---

# RSS-Zen 工作流

本地、跨平台的多语言 RSS 订阅工作流：SQLite 存储 → 翻译成简体中文 → AnySearch 全文提取 → Markdown 导出 → 主题刊次编排 → 飞书投递。

## 环境

- 需要 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。
- 首次使用：`uv sync --locked`（从 `uv.lock` 锁定依赖）。
- 配置文件默认 `rss-zen.toml`（TOML 或 YAML，schema 相同），参考 `example.rss-zen.toml`。
- 所有命令用 `uv run rss-zen <command> --config <config>` 执行。
- 默认配置路径是 `rss-zen.toml`，所有命令都支持 `-c <path>` 简写。

## Agent 操作原则（舒服·高效·低耗）

1. **先看后动**：任何操作前先用 `--json` 拿到结构化状态，再决定要不要动、动多少。
2. **`--dry-run` 先行**：批量操作（sync/translate/extract/export/retention/deadline-run）先 dry-run 看影响范围。
3. **预算意识**：`limits` 配置了每轮请求上限，长任务用 `--limit` 和 `--resume` 分批做，不要一次跑爆。
4. **用 CLI 不用手改**：订阅源增删禁用走 `feed-*` 命令并留审计；不要直接编辑配置文件的 `[[feeds]]` 或写 SQL。
5. **出错不瞎重试**：先 `doctor` 定位问题，再针对性处理；失败翻译用 `--status failed` 重试而不是全量重跑。

## 状态前置检查（第一步永远是这个）

**机器可读总览（Agent 优先用）：**

```bash
uv run rss-zen status --json -c rss-zen.toml
```

解析要点：
- `counts.pending_translation` / `counts.failed_translation` / `counts.failed_extraction` → 决定是否需要补翻/重试。
- `last_sync.latest_feed_success` → 数据新鲜度：如果很久没同步，先 `sync` 再导出。
- `last_sync.stale_feeds` → 启用的源从未成功同步过（可能源已失效）。

**排障第一命令（只读、不修改任何状态）：**

```bash
uv run rss-zen doctor --json -c rss-zen.toml   # 机器可读 {healthy, checks[]}
uv run rss-zen doctor -c rss-zen.toml          # 人类可读
```

检查项包括：配置 schema、密钥环境变量（只报 set/unset 不泄露值）、数据库完整性、启用源成功率、
处理计数、备份新鲜度、仓库（topic/edition）表状态、Feishu 投递配置。

## 完整工作流（日常操作顺序）

```bash
# 0) 首次：同步依赖 + 从示例复制配置并编辑
uv sync --locked
cp example.rss-zen.toml rss-zen.toml

# 1) 初始化数据库（建表 + 导入配置里的 feeds）
uv run rss-zen init -c rss-zen.toml

# 2) 同步所有订阅源（或单个）
uv run rss-zen sync -c rss-zen.toml
uv run rss-zen sync -s "Feed name" -c rss-zen.toml

# 3) 翻译（sync 时自动翻译；显式重试用 --status）
uv run rss-zen translate --article-id 42 -c rss-zen.toml
uv run rss-zen translate -s "Feed name" -c rss-zen.toml
uv run rss-zen translate --status failed --dry-run -c rss-zen.toml   # 先看有多少失败的
uv run rss-zen translate --status failed -c rss-zen.toml             # 批量重试失败
uv run rss-zen translate --status pending -c rss-zen.toml            # 批量重试待处理

# 4) 全文提取（显式操作，普通 sync 不做提取）
uv run rss-zen extract --since 2d --dry-run -c rss-zen.toml          # 先看范围
uv run rss-zen extract --since 2d -c rss-zen.toml                    # 近 2 天的文章
uv run rss-zen extract --without-extraction -c rss-zen.toml          # 补提取缺全文的

# 5) 导出 Markdown 合集（按配置的 export profile）
uv run rss-zen export -c rss-zen.toml                # 无参数：列出可用 profiles
uv run rss-zen export daily -c rss-zen.toml          # 导出指定 profile
uv run rss-zen export daily --since 2d -c rss-zen.toml  # 临时按时间过滤（不改配置）

# 5c) 列出文章（含翻译/提取状态）
uv run rss-zen list --since 2d --json -c rss-zen.toml
uv run rss-zen list -s "Feed" --status succeeded -n 20 -c rss-zen.toml

# 6) 查看状态 / 诊断 / 备份
uv run rss-zen status --json -c rss-zen.toml
uv run rss-zen doctor --json -c rss-zen.toml
uv run rss-zen backup -c rss-zen.toml
```

常用辅助脚本：`scripts/workflow.sh`（一键 sync → 可选补翻/提取 → export → status）、
`.pi/skills/rss-zen/scripts/recent.sh <days>`（一键导出近 N 日文章）。

## 任务 → 命令速查表（Agent 优先用 JSON 输出）

| 想做什么 | 命令 | 备注 |
|---------|------|------|
| 同步订阅源 | `uv run rss-zen sync -c rss-zen.toml` | sync 自动触发翻译，翻译受预算限制 |
| 同步单个源 | `uv run rss-zen sync -s "Feed名" -c rss-zen.toml` | 按源名或 URL 匹配 |
| 列出所有源（含状态） | `uv run rss-zen feed-list -c rss-zen.toml` | JSON 输出，含 enabled/last_success/last_error |
| 探活新源（探测可行性） | `uv run rss-zen feed-probe --url <URL> -c rss-zen.toml` | 返回 probe-token，用于 feed-add |
| 添加新源（走审计） | `uv run rss-zen feed-add --probe-token <tok> --url <URL> --name <名> -c rss-zen.toml` | 不要手动改配置文件 |
| 禁用源（保留历史） | `uv run rss-zen feed-disable --feed-id <ID> -c rss-zen.toml` | 走审计，不删除历史数据 |
| 查看审计日志 | `uv run rss-zen audit-list --limit 50 -c rss-zen.toml` | 所有变更操作都会留痕 |
| 查近 N 日文章 | `uv run rss-zen list --since 2d --json -c rss-zen.toml` | 支持 2d/12h/1w/ISO |
| 查某源的文章 | `uv run rss-zen list -s "Feed名" --json -c rss-zen.toml` | |
| 查翻译失败的文章 | `uv run rss-zen list --status failed --json -c rss-zen.toml` | |
| 批量补翻失败 | `uv run rss-zen translate --status failed -c rss-zen.toml` | 先 `--dry-run` 看数量 |
| 批量补翻待处理 | `uv run rss-zen translate --status pending -c rss-zen.toml` | 受 max_provider_requests_per_run 限制 |
| 导出近 2 日文章 | `uv run rss-zen export daily --since 2d -c rss-zen.toml` | 临时过滤，不改配置 |
| 列出可用导出 profile | `uv run rss-zen export -c rss-zen.toml` | |
| 看处理状态 | `uv run rss-zen status --json -c rss-zen.toml` | |
| 一键诊断 | `uv run rss-zen doctor --json -c rss-zen.toml` | 不修改任何状态 |
| 重译某文章 | `uv run rss-zen translate --article-id 42 -c rss-zen.toml` | |
| 全文提取（近 2 天） | `uv run rss-zen extract --since 2d -c rss-zen.toml` | 先 `--dry-run` 看范围 |
| 补提取缺全文的 | `uv run rss-zen extract --without-extraction -c rss-zen.toml` | 受 max_extract_articles_per_run 限制 |
| 备份数据库 | `uv run rss-zen backup -c rss-zen.toml` | 自动按保留策略清理旧备份 |
| 保留策略预览 | `uv run rss-zen retention --dry-run -c rss-zen.toml` | 看会删多少旧数据 |
| 执行保留清理 | `uv run rss-zen retention apply -c rss-zen.toml` | 执行前自动备份 |
| 数据库维护 | `uv run rss-zen maintenance vacuum -c rss-zen.toml` | 手动触发，服务不自动做 |
| 列出主题 | `uv run rss-zen topic-list -c rss-zen.toml` | JSON 输出，不含编辑内容 |
| 列出刊次状态 | `uv run rss-zen edition-list --limit 20 -c rss-zen.toml` | 不含内容和投递目标 |
| 预览/构建刊次 | `uv run rss-zen edition-build --topic <key> --dry-run --json -c rss-zen.toml` | 去掉 --dry-run 才持久化 |
| 到期刊次批量处理 | `uv run rss-zen deadline-run --dry-run --json -c rss-zen.toml` | 处理所有窗口期内的主题 |
| 重新投递刊次 | `uv run rss-zen edition-redeliver --edition-id <ID> -c rss-zen.toml` | 不改产物，重入投递队列 |
| 投递批次查看/处理 | `uv run rss-zen delivery-run --dry-run --json -c rss-zen.toml` | 飞书投递批次 |

时间格式：`--since`/`--until` 支持相对时长（`2d`=2天、`12h`=12小时、`1w`=1周）或 ISO 时间。

**低耗原则**：长任务用 `--limit` 控制单轮数量，用 `--resume` 断点续跑。
预算上限（`max_provider_requests_per_run` / `max_extract_articles_per_run` 等）是硬约束，
超出会自动截断，不会静默超额。

## 配置要点（rss-zen.toml）

```toml
[database]
path = "data/rss-zen.sqlite3"      # 相对路径基于配置文件所在目录

[limits]                           # ⭐ Agent 低耗关键：硬上限，超出自动截断
max_feed_response_bytes = 10000000
max_entries_per_feed = 500
max_provider_requests_per_run = 200      # 每轮翻译/提取最大请求数
max_extract_articles_per_run = 20        # 每轮提取文章数上限
max_translate_articles_per_run = 50      # 每轮翻译文章数上限
max_background_provider_requests_per_day = 200  # 后台服务日预算

[translation]
target_language = "zh-CN"

[[translation.providers]]          # 按声明顺序尝试，失败回退到下一个
name = "google"
kind = "google"                    # 免费，无需 API key
# 其他 kind：libretranslate / mymemory / openai_compatible

[[feeds]]
name = "Example feed"
url = "https://example.invalid/rss.xml"
categories = ["defense"]
poll_interval_minutes = 15
language = "en"
# headers = { "User-Agent" = "FreshRSS/1.23.1 (Linux)" }   # 反爬源自定义请求头
# fetcher = "curl"  # 反爬源改用系统 curl 抓取（Nitter等）

[[exports]]
name = "daily"
output_path = "exports/daily.md"
fields = ["source_name", "published_at", "url", "content"]
content_fallback = ["full_text", "rss_content", "summary"]

[[topics]]                          # 主题刊次（可选）
key = "indo-pacific"
name = "印太安全"
timezone = "Asia/Shanghai"
delivery_deadline = "07:30"
lookback_hours = 24
[topics.selection]
keywords = ["Taiwan", "Indo-Pacific"]
keyword_match = "any"
dedupe_by_title = true

[feishu]                           # 飞书投递（可选，默认关闭）
enabled = false
budget_mode = "observe"            # observe 模式只记账不发送
```

要点：
- 翻译 provider 至少配置 1 个；`openai_compatible` 需要 `model`；密钥通过环境变量注入（`api_key_env`）。
- `google` kind 无需 endpoint 和 API key；长文本自动按 4999 字符分块。
- **`limits` 是硬预算**：手动批量操作也受 `max_provider_requests_per_run` 限制，超出自动截断，不会超额。
- 导出 `translation_status` 过滤默认 `"succeeded"`——未翻译成功的文章不导出（除非 profile 开了 `include_untranslated`）。
- 导出 profile 支持关键词过滤（`keywords`/`content_keywords`）、标题去重（`dedupe_by=title`）。
- **反爬源（Nitter/Twitter RSS）**：用 `fetcher = "curl"` + 自定义 UA。轮询间隔 ≥60 分钟规避 429。
- **主题/刊次**：配置 `[[topics]]` 启用编排，`deadline-run` 自动按窗口期构建，`delivery-run` 投递。
- **Feishu 投递**：`budget_mode = "observe"` 时只记录不实际发送，适合先观察再开。

## 故障诊断（决策树）

**第一步永远是 `rss-zen doctor --json`** 定位问题，不要盲目重跑。
详细错误码 → 含义 → 处理方式见 [references/errors.md](references/errors.md)。

| 症状 | 检查（都走 `--json`） | 处理 |
|------|---------------------|------|
| 翻译失败重试还是失败 | `status` 看 `failed_translation` + 错误聚合 | 429 限流：换 google kind 或等冷却；网络问题稍后重试 |
| 导出 articles=0 | `list --status succeeded` 看有多少达标文章 | 补翻 `--status failed/pending`，或检查 profile 关键词过滤是否过严 |
| 某个源一直失败 | `feed-list` 看该源 `last_error` | `feed_http_403/404`、`feed_host_unresolvable` → 源失效，用 `feed-disable` 禁用 |
| 提取全部 `anysearch_exact_source_not_found` | 这是保守拒绝，属正常 | 用 `--dry-run` 先确认范围，接受 RSS 摘要或换方式 |
| 配置改了不生效 | `doctor` 的 configuration 检查 | 按报错路径修正；密钥缺失报 `env XXX is not set` |
| 数据库损坏怀疑 | `doctor` 的 database 检查（PRAGMA quick_check） | 用 `backup` 恢复最近备份 |
| 批量任务被截断 | 看返回报告的 `truncated` 字段 | 正常现象（预算上限保护），用 `--resume` 续跑下一批 |
| `feed-add` 失败 | `feed-probe` 是否成功 | 先 probe 拿到有效 token，再 add |

**关于预算截断**：`max_provider_requests_per_run` 等上限是硬约束，触发时任务正常结束（非错误），
返回报告会标注 `truncated: true` 和 `resume_from`。下一轮用 `--resume <id>` 接着跑即可。

## 订阅源管控（Agent 正确姿势）

不要手动编辑 `rss-zen.toml` 的 `[[feeds]]` 段。走 CLI 命令留审计、保数据安全。

```bash
# 1) 查看所有源的当前状态
uv run rss-zen feed-list --json -c rss-zen.toml

# 2) 想加新源？先探测（验证可访问、是有效 feed）
uv run rss-zen feed-probe --url "https://example.com/feed.xml" -c rss-zen.toml
# 返回 probe-token，短时效

# 3) 探测成功后添加（自动写入审计日志）
uv run rss-zen feed-add \
  --probe-token "<token>" \
  --url "https://example.com/feed.xml" \
  --name "Example Feed" \
  --category "defense" \
  -c rss-zen.toml

# 4) 源失效？禁用而不是删除（保留历史数据 + 审计）
uv run rss-zen feed-disable --feed-id 42 -c rss-zen.toml

# 5) 查看所有变更审计
uv run rss-zen audit-list --limit 20 -c rss-zen.toml
```

添加后需要 `sync` 一次才会有文章。禁用后历史文章仍保留，可正常导出。

## 主题 & 刊次（编排与投递）

配置了 `[[topics]]` 时，可将文章编排为定时刊次并通过飞书投递。

```bash
# 查看有哪些主题
uv run rss-zen topic-list --json -c rss-zen.toml

# 查看近期刊次状态
uv run rss-zen edition-list --limit 10 --json -c rss-zen.toml

# 预览某主题今天的刊次（不持久化）
uv run rss-zen edition-build --topic indo-pacific --dry-run --json -c rss-zen.toml

# 实际构建刊次
uv run rss-zen edition-build --topic indo-pacific --json -c rss-zen.toml

# 批量处理所有窗口期内的主题（= 定时任务手动触发版）
uv run rss-zen deadline-run --dry-run --json -c rss-zen.toml  # 先看会处理哪些
uv run rss-zen deadline-run --json -c rss-zen.toml             # 执行

# 重新投递已完成的刊次（不改内容）
uv run rss-zen edition-redeliver --edition-id 7 -c rss-zen.toml

# 查看/处理飞书投递批次
uv run rss-zen delivery-run --dry-run --json -c rss-zen.toml
```

`budget_mode = "observe"` 时飞书投递只记账不实际发送，适合观察预算消耗后再开启。

## 低耗最佳实践

1. **`--dry-run` 必做**：批量翻译/提取/保留清理/刊次构建，先 dry-run 看影响范围。
2. **分批处理**：数据量大时用 `--limit` 控制单轮量，配合 `--resume` 续跑。
3. **精准范围**：用 `--since 2d` 而不是全量处理；用 `-s "源名"` 而不是所有源。
4. **`--status` 靶向重试**：失败了只重试失败的，不要全量重跑翻译。
5. **预算上限是朋友**：被截断不是 bug，是保护。看 `resume_from` 续跑即可。
6. **先 doctor 再动手**：异常先诊断，不要靠反复重试碰运气。
7. **禁源不删源**：失效源禁用保留历史，比删除后再发现需要恢复要便宜得多。

## 技能协同

- **全文提取**：rss-zen 内置 AnySearch 提取（`extract` 命令），不需要单独调 anysearch skill。
- **联网搜索**（查证/找源/搜最新资讯）：用 `byted-web-search` 或 `anysearch` skill，与 rss-zen 的数据管道互补。
- **源管理**：用户要求添加/删除订阅源时，用 `feed-probe` → `feed-add` / `feed-disable`，不走手动改配置。
- **微信/飞书交付**：导出的 `exports/*.md` 或刊次产物可直接作为附件或内容发送。
- **飞书投递**：启用 `[feishu]` 后由 `delivery-run` 统一管理，比手动发消息更可靠（有重试、有审计、有预算）。

## 数据库（参考）

SQLite 表结构、字段与查询示例见 [references/database.md](references/database.md)。核心表：`feeds`、`articles`、`translations`、`extractions`、`export_runs`。

## 开发与测试

```bash
uv run pytest -q                    # 全部测试
uv run ruff check src/ tests/       # lint
```

> ⚠️ 提交 git 更改时严禁包含生产数据：`data/*.sqlite3`、`rss-zen.toml`、`exports/`、`*.env` 均已 gitignore；只提交源码/测试/文档/脚本。
