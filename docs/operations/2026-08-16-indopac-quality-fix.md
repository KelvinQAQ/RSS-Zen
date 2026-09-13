# 2026-08-16 印太日报质量修复记录

**状态:** 已上线
**涉及版本:** topic v2 → v6, release 20260816T013744Z

## 背景

用户反馈印太日报（飞书每日推送）内容偏少、主题覆盖率失衡：12 篇文章中 9 篇聚焦台湾/台海，
日本、印度、东南亚、智库分析等主题完全缺失。经排查发现三层根因并逐一修复。

## 根因分析

### 1. 全文提取管道从未生效（最根本）

- `extractions` 表 0 条记录；24h 文章池 761 篇中 99% 无正文（只有 RSS 摘要）
- **直接原因**：`AnySearchExtractor` 使用 `POST /v1/search` 搜索 URL，并要求返回结果 URL 与
  目标**精确一致**——实测必失败（`anysearch_exact_source_not_found`），因为搜索结果 URL 带
  `utm_source` 等参数或规范化差异
- **正确接口**：AnySearch 提供 JSON-RPC 2.0 `POST /mcp` + `tools/call` + `extract` tool，
  直接按 URL 取整页正文（实测成功，一次返回 6-25KB 正文）
- 系统无 extract 定时任务（systemd 只有 serve/export/delivery/deadline）

### 2. 主题匹配只看标题（groups 模式）

- `_filter_by_keywords` 的 groups 分支 `include_summary=False`，区域词只匹配标题
- 大量"正文隐含印太"的文章（IAF/RMAF 演习、美防务工业涉日视角、汉光军备生产）标题无地域词，
  即使正文在库也无法命中

### 3. 词表工程缺口

- 主题词只有简体"军"无繁体"軍"，台湾媒体标题（繁体）漏选
- 区域词缺 Japan/日本、India/印度、Russia/俄罗斯、韩国等 → 印太其他国家文章全部漏选
- 区域词缺 "China/中国/US-China/中美" → "中国科技/中美战略"类文章漏选
- 裸 "China" 入主题词导致泛中国新闻噪音（太阳能、养蚕、卡拉OK等）

### 4. 智库源部署缺口

- 生产配置只有 29 个 feed，本地仓库 151 个 feed 中 **107 个 Nitter 智库源全部未部署**
- 生产环境仅 9 个智库源，且 **Lowy URL 错误**（`/feed` 空、正确是 `/the-interpreter/rss.xml`）、
  **CSIS 主站 RSS 是 2016 年死源**
- 智库源低频发布（周更为主），24h lookback 窗口几乎抓不到新文

## 修复内容

### 代码（本地仓库 `v0.3.0` 分支，已提交）

| 文件 | 改动 |
|---|---|
| `src/rss_zen/extraction.py` | extractor 改用 `/mcp` JSON-RPC `extract` tool；错误处理适配 `error` envelope |
| `src/rss_zen/export.py` | groups 模式：区域词标题未命中时回退正文（含 extractions 表内容） |
| `src/rss_zen/edition.py` | 渲染"今日目录"（TOC）+ 锚点链接 |
| `tests/test_extraction.py` | 更新为 /mcp 契约测试 |
| `deploy/systemd/rss-zen-extract.{service,timer}` | 每日 05:45 提取 48h 内无正文文章（07:30 日报 deadline 前） |

### 生产配置（`/etc/rss-zen/rss-zen.toml`）

- **topic v2 → v6**（不可变版本机制，每次改动需升版）：
  - 区域词 24 → 90：补日/印/俄/韩/东南亚/中国本体/中美框架
  - 主题词 36 → 50：去裸 China（消除噪音）+ 补战略框架词（China threat/US-China/Sino-/中美等）
- **feeds 29 → 37**：
  - 修复 Lowy URL → `/the-interpreter/rss.xml`（旧 feed id=3 已禁用）
  - 新增 8 个验证有效的原生 RSS 智库源：RAND、Arms Control、MERICS、China-US Focus、
    USIP、IDSA、Quincy、Defense Priorities

### 部署修复（release 构建 bug）

- 部署脚本 `build_release` 中 `mv` staging → releases 后，venv editable `.pth` 和入口脚本
  shebang 仍指向已删除的 `.release-tmp` 路径，导致新 release 无法启动（203/EXEC）
- 已在 `deploy-linux.sh` 增加路径重写修复（stash 中恢复的历史改动）；本次手工修正了新 release

## 效果验证

| 指标 | 修复前 (v2) | 修复后 (v6) |
|---|---|---|
| 日报文章数 | 12 | 36~39 |
| 来源数 | 5 | 10+ |
| 台湾媒体占比 | 75% | ~31% |
| 用户指定 5 篇目标 | 全部漏选 | 全部入选 |
| 噪音（社会新闻） | 少量 | ≈0 |

- 提取管道实测：`rss-zen extract --article-id X` → succeeded（6332 字符正文 + 4380 翻译）
- extract timer 已启用（次日 05:45 首跑）；doctor healthy；176 测试通过

## 后续建议

1. **智库周报**：智库源低频发布，24h 窗口基本抓不到 → 建议单独出 7 天窗口的"智库深度分析"独立刊次
2. **补充失效源**：NTI/Jamestown/NBR/Pacific Forum/Brookings/CFR/Carnegie 等 RSS 直接抓取为空，
   需寻找替代 URL 或换源
3. **噪音监控**：v6 词表已大幅降噪，后续若新增主题词需回归测试防止泛化
