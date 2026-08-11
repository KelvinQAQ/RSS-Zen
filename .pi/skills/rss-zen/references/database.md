# RSS-Zen 数据库参考

SQLite 数据库默认路径：配置文件里 `[database].path`（相对路径基于配置文件所在目录），例如 `data/rss-zen.sqlite3`。

## 表结构

### feeds（订阅源）

| 字段 | 说明 |
|------|------|
| id | 主键 |
| name / url | 名称 / 唯一 URL |
| categories_json | JSON 数组分类 |
| language | 源语言（如 `en`） |
| poll_interval_minutes | 轮询间隔 |
| enabled | 1=启用 |
| origin | 来源（config/opml 等） |
| etag / last_modified | HTTP 条件请求缓存 |
| last_checked_at / last_success_at | 检查/成功时间 |
| last_error_code / last_error_message | 最近错误 |

### articles（文章）

| 字段 | 说明 |
|------|------|
| id | 主键 |
| feed_id | 关联 feeds |
| guid | RSS GUID（身份优先） |
| canonical_url | 规范 URL（身份回退） |
| title / summary / content | 原始内容 |
| author / categories_json | 元信息 |
| published_at / source_updated_at | 时间 |
| detected_language / source_language | 语言 |
| content_hash | 内容哈希（判断是否更新/重译） |
| first_seen_at / last_seen_at | 首次/最近出现 |

唯一约束：`(feed_id, canonical_url)`；`(feed_id, guid)` 唯一索引（guid 非空时）。

### translations（翻译）

| 字段 | 说明 |
|------|------|
| id / article_id | 主键 / 关联文章 |
| target_language | 目标语言（如 zh-CN） |
| title / summary / content | 翻译结果 |
| provider_name / provider_model | 使用的提供商 |
| status | `succeeded` / `failed` / `pending` 等 |
| source_hash | 翻译时的源哈希 |
| error_code / error_message | 失败信息 |
| attempt_count / next_retry_at / last_attempt_at | 重试状态 |
| terminal | 是否终止重试 |

唯一约束：`(article_id, target_language)`。

### extractions（全文提取）

| 字段 | 说明 |
|------|------|
| id / article_id | 主键 / 关联文章 |
| provider_name | anysearch |
| source_url | 请求的源 URL |
| content | 提取的原始全文 |
| translated_content | 提取文本的翻译（可选） |
| translation_provider_name | 提取翻译用提供商 |
| status | `succeeded` / `failed` / `translation_failed` |
| request_id | AnySearch 请求 ID（排查用） |
| error_code / error_message | 失败信息 |

### export_runs（导出记录）

profile_name、output_path、filters_json、article_count、status、created_at。

### sync_runs（同步记录）

started_at、completed_at、status、feed_count、article_count、error_code、error_message。

## 常用查询

```sql
-- 查看某订阅源的文章数与翻译状态
SELECT a.id, a.title, t.status, t.provider_name
FROM articles a
JOIN translations t ON t.article_id = a.id
WHERE a.feed_id = 1
ORDER BY a.id DESC LIMIT 20;

-- 统计翻译状态
SELECT status, count(*) FROM translations GROUP BY status;

-- 查看最近失败的提取
SELECT article_id, status, error_code FROM extractions
WHERE status != 'succeeded' ORDER BY id DESC LIMIT 10;

-- 查看 feeds 的错误
SELECT id, name, last_error_code, last_error_message FROM feeds
WHERE last_error_code IS NOT NULL;
```
