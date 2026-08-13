# RSS-Zen

RSS-Zen is a local, cross-platform command-line workflow for multilingual RSS and
Atom feeds. It stores source data in SQLite, translates articles to Simplified
Chinese, optionally retrieves full text through AnySearch, and renders reusable
Markdown article collections. Development scope, release exit criteria, and architecture decisions
are maintained in the [roadmap](docs/roadmap.md).

## Setup

Python 3.13+ and [uv](https://docs.astral.sh/uv/) are required for development and
single-host operation.

```bash
uv sync --locked
cp example.rss-zen.toml rss-zen.toml
```

For a Linux production deployment with systemd, see
[`docs/deployment-linux.md`](docs/deployment-linux.md). The shipped units run one
foreground service process with a local SQLite database, separate export/backup timers,
and a dedicated non-root account.
If the Linux host already has this checkout, run the guided installer:

```bash
sudo bash scripts/deploy-linux.sh bootstrap
```

It detects supported Linux distributions, installs missing `uv` and Python 3.13, creates
protected configuration templates, and only starts services after you replace all placeholders.
After bootstrap, daily release work runs as the restricted `rss-zen-deploy` account rather than root;
see the Linux deployment guide for the exact release command and systemd 247+ requirement.

Set only the provider credentials you configured in `rss-zen.toml`. Secrets are
read from environment variables and are never written to SQLite, logs, exports,
or configuration examples.

```bash
export FREE_TRANSLATION_API_KEY="..."
export AI_TRANSLATION_API_KEY="..."
export ANYSEARCH_API_KEY="..."  # Optional; anonymous AnySearch is supported.
```

Edit `rss-zen.toml` to configure real feed URLs and translation endpoints, then
initialize the local database:

```bash
uv run rss-zen init --config rss-zen.toml
```

YAML (`.yaml` or `.yml`) has the same schema as TOML. Feed definitions in the
config are authoritative for their metadata. OPML imports are stored in SQLite
and do not rewrite the configuration file.

## Commands

```bash
# Add feeds from OPML. Nested OPML groups become feed categories.
uv run rss-zen import-opml subscriptions.opml --config rss-zen.toml

# Synchronize every feed, or a single configured feed name/URL.
uv run rss-zen sync --config rss-zen.toml
uv run rss-zen sync --source "Example feed" --config rss-zen.toml

# Retry translation for selected already-synchronized articles.
# Manual batches are bounded by [limits]; use --limit to lower/override it,
# and --dry-run to inspect selection without provider calls.
uv run rss-zen translate --article-id 42 --config rss-zen.toml
uv run rss-zen translate --status failed --limit 20 --dry-run --config rss-zen.toml
uv run rss-zen translate --status failed --report-json reports/translate.json --config rss-zen.toml

# Explicit full-text retrieval; no extraction happens during normal sync.
# Extraction batches are also bounded by [limits].
uv run rss-zen extract --article-id 42 --config rss-zen.toml
uv run rss-zen extract --source "Example feed" --without-extraction --limit 20 --config rss-zen.toml
uv run rss-zen extract --source "Example feed" --report-json - --config rss-zen.toml

# Render a named Markdown profile.
# --since/--until override the profile's time filters without editing config.
# Accept a relative duration (2d, 12h, 1w) or an ISO datetime.
uv run rss-zen export daily --config rss-zen.toml
uv run rss-zen export daily --since 2d --config rss-zen.toml

# List articles with their translation/extraction status.
# Filters: --source, --since, --until, --status (succeeded/failed/pending), --limit.
uv run rss-zen list --since 2d --config rss-zen.toml
uv run rss-zen list --source "Example feed" --status succeeded --limit 20 --config rss-zen.toml

# Inspect local feed and processing state without network access.
# --json emits machine-readable structured output.
uv run rss-zen status --config rss-zen.toml
uv run rss-zen status --json --config rss-zen.toml

# Start the foreground polling service.
uv run rss-zen serve --config rss-zen.toml --verbose
```

`serve` performs one initial sync, then polls every enabled feed using its own
`poll_interval_minutes` or the configured default. It also retries due translations
with bounded exponential backoff. It never performs automatic AnySearch extraction or
Markdown export. Keep it alive through the platform's service manager; Linux production
should use the supplied `systemd` units.

## Processing Model

- Conditional requests use ETag and Last-Modified values persisted per feed.
- A feed failure is isolated; the remaining feeds continue to synchronize.
- Article identity prefers RSS GUID and then falls back to the canonical URL.
- Changed source content updates the article and triggers retranslation.
- Translation tries declared providers in order. A LibreTranslate-compatible
  free API or the small-text MyMemory free API can be first; an
  OpenAI-compatible endpoint can be the automatic fallback.
- OpenAI-compatible providers accept three optional settings:
  `reasoning_effort` (for example `"none"` to disable model thinking),
  `timeout_seconds` (per-request override of the 30s client default), and
  `max_chars` (long text is split into per-request chunks; defaults to 4000).
- AnySearch calls `POST https://api.anysearch.com/v1/search` using the article
  URL as the query. RSS-Zen only accepts a result whose normalized URL exactly
  matches the source article, preventing unrelated search content from being
  stored as full text.

`translate` and `extract` can write a versioned, atomic JSON run report with `--report-json`
(`-` emits it to stdout). Runtime reports include exact provider request/source-character
consumption; dry-run reports are explicitly marked as estimates.

Translation and AnySearch requests send article content to the configured third
party. Select providers according to your privacy and retention requirements. Feed URLs use
HTTPS and reject private resolved addresses; the optional curl fetcher validates every redirect
and writes sensitive headers only to a private temporary curl configuration. Place ordinary
headers in `headers`; configure `Authorization` and `Cookie` only via `header_env` and environment
credentials.

## Export Profiles

Each `[[exports]]` entry writes one Markdown file with a top-level directory and
stable `article-<id>` anchors. `fields` chooses output fields; `content_fallback`
controls the order of full-text, RSS-body, and summary content. `filters` and
`preprocess` are declarative only: no shell commands or arbitrary code run during
export. The `list` command reports each article's translation/extraction status and
supports `--since`/`--until`/`--status` filtering plus `--json` for machine-readable
output. The `export` command's `--since`/`--until` options override the profile's
`published_after`/`published_before` filters without editing the config.

```toml
[[exports]]
name = "daily"
output_path = "exports/daily.md"
title = "Daily reading"
fields = ["source_name", "published_at", "url", "content"]
content_fallback = ["full_text", "rss_content", "summary"]

[exports.filters]
categories = ["technology"]
translation_status = "succeeded"
require_full_text = false

[[exports.preprocess]]
field = "content"
operation = "strip_html"
```

Supported preprocessor operations are `strip_html`, `collapse_whitespace`,
`truncate`, `replace`, and `date_format`.

## Development

```bash
uv run ruff check .
uv run pytest -q
uv run pip-audit
uv run python scripts/check_text_encoding.py
```

Tests use local RSS/Atom fixtures and mocked HTTP transports. They never call
translation, AnySearch, or feed providers over the network. Structured logs
include run/feed/article identifiers where available and avoid API keys and full
request headers.
