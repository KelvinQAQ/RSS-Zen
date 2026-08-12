# RSS-Zen 错误码参考

结构化错误码（`error_code`）在所有命令失败时输出（`error [<code>]: <message>`），
`status --json` / `doctor --json` 中的错误聚合也使用同一套编码。
`retryable` 表示该错误在 `serve` 服务中会按退避策略自动重试。

## 配置与选择器

| error_code | 含义 | 处理 |
|-----------|------|------|
| `invalid_configuration` | 配置 schema 校验失败（TOML/YAML 解析错误、缺字段、endpoint 非 HTTPS 等） | 按报错路径修正 `rss-zen.toml`；可用 `rss-zen doctor` 定位 |
| `invalid_translation_status` | `translate --status` 传了非 `pending`/`failed` 的值 | 使用 `pending` 或 `failed` |
| `translation_selector_required` | translate 未提供任何选择器 | 加 `--article-id` / `--source` / `--status` |
| `extraction_selector_required` | extract 未提供任何选择器 | 加 `--article-id` / `--source` / `--since` / `--without-extraction` |
| `article_not_found` | 选择器没有匹配到文章 | 先 `rss-zen list --since 2d` 确认 id 或源名 |
| `feed_not_found` | `sync --source` 没有匹配的启用的源 | 用 `rss-zen status --json` 查看 feeds 名称/URL |
| `export_profile_not_found` | `export` 指定了不存在的 profile | `rss-zen export`（无参数）列出可用 profiles |
| `invalid_opml` | OPML 文件无法解析或缺少 xmlUrl | 检查 OPML 结构 |

## Feed 同步

| error_code | 含义 | 处理 |
|-----------|------|------|
| `feed_timeout` | feed 请求超时（重试耗尽） | 网络问题，稍后重试（retryable） |
| `feed_network_error` | 网络层失败（DNS/连接被拒等） | 检查网络；`feed_host_unresolvable` 说明域名无法解析（源可能失效） |
| `feed_http_403` / `feed_http_404` 等 | 源服务器返回 HTTP 错误 | 403=反爬/封禁，404=URL 失效；考虑更换源 |
| `feed_redirect_invalid` | 重定向缺少 Location 头 | 源异常，忽略 |
| `feed_redirect_limit` | 超过最大重定向次数 | 源存在循环重定向，考虑更换 |
| `feed_response_too_large` | 响应超过 `max_feed_response_bytes` | 提高限制或换源 |
| `feed_private_address` | feed URL 指向内网/保留地址 | 安全策略拒绝（不会发起请求） |
| `invalid_feed_url` | URL 非 HTTP(S) 或缺少 host | 修正配置中的 url |
| `feed_parse_error` | RSS/Atom 无法解析 | 源格式损坏或非 XML；用 curl 验证 |

## 翻译

| error_code | 含义 | 处理 |
|-----------|------|------|
| `translation_timeout` | 翻译请求超时 | retryable；稍后重试 |
| `translation_network_error` | 翻译接口不可达 | retryable；检查 endpoint 与网络 |
| `translation_http_429` | 免费接口限流（MyMemory 等） | 改用 `google` kind 或等待配额恢复；retryable |
| `translation_provider_error` | provider 返回错误（含 Google 网页接口偶发失败） | retryable；`translate --status failed` 批量重试 |
| `translation_invalid_response` | provider 返回无法解析的内容 | 换 provider 或重试 |

## AnySearch 全文提取

| error_code | 含义 | 处理 |
|-----------|------|------|
| `anysearch_timeout` / `anysearch_network_error` | 提取请求超时/网络失败 | retryable；稍后重试 |
| `anysearch_authentication_failed` | API key 无效 | 检查 `ANYSEARCH_API_KEY` |
| `anysearch_authorization_failed` | key 无权限 | 检查 key 权限 |
| `anysearch_quota_exhausted` | 配额耗尽 | 充值或等待重置 |
| `anysearch_rate_limited` | 触发限流 | retryable；稍后重试 |
| `anysearch_exact_source_not_found` | AnySearch 未返回与文章 URL 完全匹配的结果 | 保守拒绝，不存储不相关内容；属正常情况 |
| `anysearch_invalid_response` / `anysearch_api_error` | 服务返回异常 | 重试或联系服务方 |

## 导出 / 备份

| error_code | 含义 | 处理 |
|-----------|------|------|
| `backup_failed` | 无法创建备份（磁盘/权限/SQLite 错误） | 检查磁盘空间与目录权限 |
| `backup_integrity_failed` | 备份未通过完整性校验 | 备份已删除；重试 |
| `database_not_found` | 数据库文件不存在 | 先 `rss-zen init` |

## 诊断命令

- `rss-zen doctor` — 不修改状态，检查配置/密钥/数据库完整性/源健康/处理计数/备份新鲜度。
  `doctor --json` 返回 `{healthy, checks[]}`，`healthy=false` 时退出码 1。
- `rss-zen status --json` — 汇总计数 + `last_sync.latest_feed_success`（数据新鲜度）+ 错误聚合。
