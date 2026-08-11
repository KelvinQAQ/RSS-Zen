# RSS-Zen Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a cross-platform local command-line service that synchronizes multilingual RSS/Atom feeds into SQLite, translates content to Simplified Chinese, optionally extracts full text, and exports configurable Markdown article collections.

**Architecture:** A Typer CLI controls a small Python application layer. A scheduler invokes a sync pipeline that conditionally fetches and parses feeds, reconciles articles in SQLite, detects their language, and creates translations through pluggable providers. Export and full-text extraction are explicit commands backed by the same repository data. YAML and TOML configuration share one validated schema; secrets are read only from environment variables.

**Tech Stack:** Python 3.13, uv, Typer, Pydantic, PyYAML, HTTPX, feedparser, APScheduler, Beautiful Soup/Markdownify, SQLite (`sqlite3`), pytest, pytest-httpx, Ruff.

---

## Development and Debugging Standards

- Keep modules narrow and typed. Application code must not use unstructured `dict[str, Any]` outside external API parsing boundaries.
- Add a failing test before every behavior change, then make the smallest implementation that passes it. Keep fixture RSS/Atom data under `tests/fixtures/`; never call external APIs in tests.
- Use a structured application error with an error code, user-safe message, cause, and retryability. CLI output reports the user-safe message; logs retain causal context without credentials or full authorization headers.
- Include `run_id`, `feed_id`, and `article_id` in log records when available. Log lifecycle events and retries, not article bodies or API secrets.
- Validate configuration before opening the scheduler. Parameterize every SQL value, close HTTP responses, bound retries, and write exported files atomically.
- Run targeted tests after each task and run `uv run ruff check .` plus the full `uv run pytest` suite before delivery. Reproduce defects with a minimal fixture before fixing them.
- No production network calls in import-time code. Network clients, clocks, storage paths, and provider adapters are injected so tests remain deterministic.

## Initial File Layout

```text
pyproject.toml
README.md
example.rss-zen.toml
src/rss_zen/
  __init__.py
  cli.py
  config.py
  errors.py
  logging.py
  db.py
  models.py
  feeds.py
  http_client.py
  sync.py
  scheduler.py
  translation.py
  extraction.py
  export.py
  markdown.py
tests/
  conftest.py
  fixtures/
  test_config.py
  test_db.py
  test_feeds.py
  test_sync.py
  test_translation.py
  test_extraction.py
  test_export.py
  test_cli.py
docs/plans/2026-08-11-rss-zen-implementation.md
```

### Task 1: Create the Package and Test Harness

**Files:**
- Create: `pyproject.toml`
- Create: `src/rss_zen/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_cli.py`
- Create: `.gitignore`

**Step 1: Write the failing CLI smoke test**

Use Typer's `CliRunner` to assert that invoking the app with `--help` returns exit code `0` and includes `serve`, `sync`, `import-opml`, `extract`, `export`, and `status`.

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli.py -q`

Expected: failure because the package and CLI do not exist.

**Step 3: Add the smallest project definition and CLI skeleton**

Define production dependencies, pytest/Ruff development dependencies, `src` package discovery, and a `rss-zen = "rss_zen.cli:app"` script. Add an empty Typer app with the six command names, each returning a clear temporary message until its task implements it.

**Step 4: Run the test and lint**

Run: `uv run pytest tests/test_cli.py -q`

Run: `uv run ruff check .`

Expected: both pass.

### Task 2: Define Typed Models, Errors, Logging, and Config Loading

**Files:**
- Create: `src/rss_zen/errors.py`
- Create: `src/rss_zen/models.py`
- Create: `src/rss_zen/logging.py`
- Create: `src/rss_zen/config.py`
- Create: `tests/test_config.py`
- Create: `example.rss-zen.toml`

**Step 1: Write failing configuration tests**

Cover loading the same valid configuration from TOML and YAML; reject a missing `database.path`, a feed URL without HTTP(S), an absent configured environment variable, duplicate export profile names, and an unsupported translation provider. Test that a feed language override wins over automatic detection.

**Step 2: Run the config tests**

Run: `uv run pytest tests/test_config.py -q`

Expected: failure because no parser or models exist.

**Step 3: Implement validated configuration**

Create Pydantic models for application settings, feeds, translation provider chain, AnySearch search settings, and named export profiles. AnySearch settings default its base URL to `https://api.anysearch.com`, accept an optional API-key environment-variable name, and validate `tag`, `zone`, `language`, and `max_results` (1-10). Load TOML with `tomllib` and YAML with `yaml.safe_load`; normalize both into the same model. Resolve configured secret values from named environment variables only. Implement `AppError` with code, message, retryability, and optional cause; configure a non-secret structured logger with contextual fields.

**Step 4: Add a documented TOML example**

Include a manual feed, `FREE_TRANSLATION_API_KEY` and `AI_TRANSLATION_API_KEY` references, an optional `ANYSEARCH_API_KEY` reference, `https://api.anysearch.com` as the documented base URL, and an export profile. Do not include real credentials or fixed translation-provider endpoint assumptions.

**Step 5: Re-run tests and lint**

Run: `uv run pytest tests/test_config.py -q`

Run: `uv run ruff check .`

Expected: both pass.

### Task 3: Implement SQLite Schema and Repository Operations

**Files:**
- Create: `src/rss_zen/db.py`
- Create: `tests/test_db.py`

**Step 1: Write failing repository tests**

Test schema initialization on a temporary database; feed upsert; article reconciliation by normalized link and GUID; content-hash update; translation and extraction state persistence; and export-run recording. Assert that SQL parameters safely preserve quote characters in titles.

**Step 2: Run database tests**

Run: `uv run pytest tests/test_db.py -q`

Expected: failure because the repository does not exist.

**Step 3: Implement versioned migrations and repository methods**

Create tables for `schema_migrations`, `feeds`, `articles`, `translations`, `extractions`, `export_runs`, and `sync_runs`, with indexes on article identity, publication time, and processing state. Use transaction context managers and parameterized SQL. Store original RSS data separately from translated values, alongside first/last seen times and content hashes. Back up the database before a schema migration changes it.

**Step 4: Re-run database tests**

Run: `uv run pytest tests/test_db.py -q`

Expected: pass.

### Task 4: Add Feed Configuration Reconciliation and OPML Import

**Files:**
- Create: `src/rss_zen/feeds.py`
- Create: `tests/fixtures/subscriptions.opml`
- Create: `tests/test_feeds.py`
- Modify: `src/rss_zen/cli.py`

**Step 1: Write failing feed tests**

Test merging manual config feeds with database feeds by canonical URL, idempotently importing nested OPML outlines, preserving OPML folder names as categories, skipping disabled HTML outlines, and reporting malformed OPML as a user-safe error.

**Step 2: Run feed tests**

Run: `uv run pytest tests/test_feeds.py -q`

Expected: failure because import and reconciliation are absent.

**Step 3: Implement feed management and `import-opml`**

Parse OPML with `xml.etree.ElementTree`, normalize feed URLs, and reconcile records. The command accepts an input path and config path, prints imported/updated/skipped totals, and remains idempotent. It must never rewrite the human-maintained YAML or TOML configuration file.

**Step 4: Re-run targeted tests and CLI smoke test**

Run: `uv run pytest tests/test_feeds.py tests/test_cli.py -q`

Expected: pass.

### Task 5: Build the Conditional RSS/Atom Synchronization Pipeline

**Files:**
- Create: `src/rss_zen/http_client.py`
- Create: `src/rss_zen/sync.py`
- Create: `tests/fixtures/sample.rss.xml`
- Create: `tests/fixtures/sample.atom.xml`
- Create: `tests/test_sync.py`
- Modify: `src/rss_zen/cli.py`

**Step 1: Write failing sync tests**

Using mocked HTTPX responses, cover ETag and Last-Modified request headers, `304 Not Modified`, RSS and Atom parsing, link/GUID identity, relative-link resolution, duplicate entries, updated content hash, individual-feed failure isolation, and bounded retryable timeouts.

**Step 2: Run sync tests**

Run: `uv run pytest tests/test_sync.py -q`

Expected: failure because the sync pipeline is missing.

**Step 3: Implement HTTP and parsing boundaries**

Create an injected HTTPX client with timeouts and exponential backoff. Parse only HTTP(S) feeds, persist conditional-request metadata, resolve links against the feed URL, and insert/update articles transactionally. Return a per-feed result object rather than throwing an exception that stops the whole run. Add `sync` command filters for configured source name and URL.

**Step 4: Re-run tests and lint**

Run: `uv run pytest tests/test_sync.py -q`

Run: `uv run ruff check .`

Expected: both pass.

### Task 6: Implement Language Detection and Translation Provider Fallback

**Files:**
- Create: `src/rss_zen/translation.py`
- Create: `tests/test_translation.py`
- Modify: `src/rss_zen/sync.py`

**Step 1: Write failing translation tests**

Test language override precedence, automatic detection, translation of title/summary/body to `zh-CN`, a failed primary free provider falling back to the AI provider, no fallback when both fail, and retranslation after an article content update. Assert that provider keys never appear in exceptions or logs.

**Step 2: Run translation tests**

Run: `uv run pytest tests/test_translation.py -q`

Expected: failure because translation adapters are absent.

**Step 3: Implement provider protocol and worker**

Define a `TranslationProvider` protocol and adapters driven by configuration. The primary provider is a configurable free API adapter; the AI provider is an OpenAI-compatible adapter. Make endpoint URLs, API key headers, models, and request/response mapping explicit config fields so no undocumented free-service contract is embedded. Detect language using a deterministic local library and honor feed overrides. Persist provider name, translated fields, status, and safe failure code.

**Step 4: Re-run translation tests**

Run: `uv run pytest tests/test_translation.py -q`

Expected: pass.

### Task 7: Implement Explicit AnySearch Full-Text Extraction

**Files:**
- Create: `src/rss_zen/extraction.py`
- Create: `tests/test_extraction.py`
- Modify: `src/rss_zen/cli.py`

**Step 1: Write failing extraction tests**

Test selecting specific article IDs and filters, rejecting articles without canonical links, posting a URL query to `/v1/search`, omitting the Authorization header for anonymous access, parsing `data.results`, accepting only the result whose normalized URL exactly matches the article link, storing its `content` without overwriting RSS content, handling `429` with `Retry-After` as retryable, handling `500`, `503`, and `504` as retryable, and returning safe errors for malformed responses, authentication errors, quota exhaustion, and no exact source match.

**Step 2: Run extraction tests**

Run: `uv run pytest tests/test_extraction.py -q`

Expected: failure because the extraction client does not exist.

**Step 3: Implement a configurable AnySearch adapter and command**

Define an `Extractor` protocol. Implement `AnySearchExtractor` against the documented `POST https://api.anysearch.com/v1/search` contract: send JSON with the canonical article URL as `query`, configured `tag` (default `general.general`), optional `zone` and `language`, and a bounded `max_results`; add `Authorization: Bearer <key>` only when the optional environment variable resolves. Parse the documented `code`, `message`, `request_id`, and `data.results` envelope. Use `content` only from a result whose normalized `url` exactly equals the canonical article URL; persist `request_id` for diagnosis, but never secrets or full response bodies. The `extract` command accepts article IDs or safe repository filters, shows per-article results, and persists result text, status, source URL, and errors independently.

**Step 4: Re-run extraction tests**

Run: `uv run pytest tests/test_extraction.py -q`

Expected: pass.

### Task 8: Implement Export Profile Evaluation and Markdown Rendering

**Files:**
- Create: `src/rss_zen/markdown.py`
- Create: `src/rss_zen/export.py`
- Create: `tests/test_export.py`
- Modify: `src/rss_zen/cli.py`

**Step 1: Write failing export tests**

Cover profile filtering by publication range, source, category, status, and full-text availability; stable sorting; content fallback `extraction -> RSS body -> summary`; HTML-to-Markdown conversion; date formatting; text replacement; conditional fields; duplicate headings; top-of-file directory anchors; atomic output replacement; and export history insertion.

**Step 2: Run export tests**

Run: `uv run pytest tests/test_export.py -q`

Expected: failure because no query/render pipeline exists.

**Step 3: Implement restricted declarative rendering**

Expose a typed article view and a finite set of preprocessor operations. Render a single Markdown document with configured title, a table of contents using `article-{id}` anchors, then each article body. Do not evaluate arbitrary scripts, templates with file access, or shell commands. Write to a temporary sibling path and replace the target only after rendering completes.

**Step 4: Re-run export tests**

Run: `uv run pytest tests/test_export.py -q`

Expected: pass.

### Task 9: Add the Long-Running Scheduler, Status, and Documentation

**Files:**
- Create: `src/rss_zen/scheduler.py`
- Modify: `src/rss_zen/cli.py`
- Create: `README.md`
- Modify: `tests/test_cli.py`
- Create: `tests/test_scheduler.py`

**Step 1: Write failing scheduler and status tests**

Test that `serve` schedules configured polling intervals, runs an initial sync deterministically with an injected clock, continues after one failed source, stops cleanly on an interrupt signal, and that `status` reports feed health, pending work, and latest failures without leaking secrets.

**Step 2: Run scheduler tests**

Run: `uv run pytest tests/test_scheduler.py tests/test_cli.py -q`

Expected: failure because the service loop and status query are missing.

**Step 3: Implement service orchestration and user documentation**

Use APScheduler with per-feed interval jobs and a clean shutdown path. Provide `serve`, `status`, and `init` commands. Document configuration, environment variables, OPML import, sync, extraction, export, testing, logs, and platform-specific ways to keep a foreground CLI process running. State the external-data implications of translation and extraction providers.

**Step 4: Run focused verification**

Run: `uv run pytest tests/test_scheduler.py tests/test_cli.py -q`

Expected: pass.

### Task 10: Run Full Verification and Perform a Manual Smoke Test

**Files:**
- Modify as needed only to correct verified defects from this task.

**Step 1: Run all automated checks**

Run: `uv run ruff check .`

Run: `uv run pytest`

Expected: all checks pass.

**Step 2: Run a local manual smoke test**

Use a temporary config, test database, and fixture HTTP server. Run `init`, `sync`, `status`, `extract` with a mocked AnySearch response, and `export profile-name`; inspect the generated Markdown for its directory, stable anchors, Chinese fields, and article content.

**Step 3: Record results**

Add only substantive verification findings to the README or an implementation note. Do not add generated databases, logs, credentials, or exported articles to version control.

## AnySearch API Contract

- Base URL: `https://api.anysearch.com`; endpoint: `POST /v1/search`.
- Authentication is optional. With `ANYSEARCH_API_KEY` configured, send `Authorization: Bearer <key>`; otherwise use the anonymous daily quota and do not send the header.
- Request content type is JSON. The implementation sends the canonical article URL as `query`, `tag` defaulting to `general.general`, and `max_results` constrained to 1-10. `zone` (`cn` or `intl`) and preferred response `language` are optional configuration fields.
- A successful envelope has `code: 0`, `message`, `request_id`, and `data.results`. Each result can contain `title`, `url`, `snippet`, and cleaned body `content`.
- The current reference does not document a separate URL-extraction endpoint, even though it lists `extract_*` error codes. Therefore RSS-Zen will use the documented URL-search flow and only save a result when its URL exactly matches the source article. A future direct extractor can be added as a separate adapter when its request schema is available.
- Retry `429` according to `Retry-After`, and retry transient `500`, `503`, and `504` failures with bounded backoff. Treat malformed requests, authentication failures, quota exhaustion, unsupported content, and no exact result match as non-retryable until user action or a future sync.

## Deferred Decisions

- Select a production default free translation service only after its current usage limits, terms, request schema, and privacy characteristics are reviewed. The implementation supports configuration-driven adapters now.
- Add a direct AnySearch URL-extraction adapter if its endpoint and request/response contract become available; the documented `/v1/search` implementation is fully specified above.
- Initialize Git and make focused commits after the user explicitly requests repository setup or version-control history; the current directory is not a Git repository.
