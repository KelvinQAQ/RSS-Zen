# Linux Production Hardening Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

**Goal:** Make RSS-Zen safe and operable as a single-instance Linux `systemd` service with a local SQLite database, HTTPS-only external access, automatic feed/translation recovery, scheduled exports, and verified local backups.

**Architecture:** Keep the existing Typer CLI and SQLite storage. Extend the schema with retry state and operational metadata, run one foreground `serve` process protected by an advisory lock, and use independent systemd timers for export and backup. RSS input is treated as untrusted: external URLs are HTTPS-only, credentials/private-network targets are rejected, redirects are revalidated, and payload sizes are bounded.

**Tech Stack:** Python 3.13+, Typer, APScheduler, httpx, SQLite, pytest, Ruff, Linux systemd.

**Confirmed decisions:**

- Deployment is one Linux host, one active `rss-zen serve` instance, and one local persistent SQLite database.
- `serve` synchronizes feeds and retries translations; it never triggers AnySearch extraction automatically.
- Markdown exports run from a separate systemd timer.
- Feeds and configured third-party endpoints must use HTTPS.
- Daily local backups retain 30 days; remote backup is out of scope.
- No PostgreSQL, Docker/Compose, web API, external queue, or high-availability design is included.

**Repository note:** Git metadata was not available in the supplied workspace, so the normal per-task commit steps are intentionally omitted.

---

### Task 1: Add validated production settings and migration-safe runtime limits

**Files:**
- Modify: `src/rss_zen/models.py`
- Modify: `src/rss_zen/config.py`
- Modify: `example.rss-zen.toml`
- Test: `tests/test_config.py`

**Step 1: Write failing configuration tests**

Add tests proving that:

- a `[limits]` table validates positive response/article/translation limits;
- feed and translation endpoints reject HTTP URLs and URL userinfo;
- service retry interval/count values reject invalid values;
- the default configuration remains backward-compatible when `[limits]` is absent.

**Step 2: Run the focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q --basetemp .test-tmp
```

Expected: failures for the new schema expectations.

**Step 3: Implement typed settings**

Add `LimitsSettings` and extend `ServiceSettings` with explicit defaults such as:

```python
class LimitsSettings(BaseModel):
    max_feed_response_bytes: int = Field(default=10_000_000, ge=1)
    max_entries_per_feed: int = Field(default=500, ge=1)
    max_article_chars: int = Field(default=500_000, ge=1)
    max_translation_chars: int = Field(default=100_000, ge=1)
```

Add validated retry settings to `ServiceSettings`, require secure URL endpoints, and reject any parsed URL containing `username` or `password`.

**Step 4: Document the settings**

Add the `[limits]` block and retry defaults to `example.rss-zen.toml` with concise comments.

**Step 5: Re-run focused tests**

Expected: all configuration tests pass.

---

### Task 2: Add outbound URL policy and bounded feed retrieval

**Files:**
- Create: `src/rss_zen/network.py`
- Modify: `src/rss_zen/feeds.py`
- Modify: `src/rss_zen/http_client.py`
- Modify: `src/rss_zen/sync.py`
- Test: `tests/test_feeds.py`
- Test: `tests/test_sync.py`

**Step 1: Write failing security and resource tests**

Cover:

- rejection of HTTP URLs, URL userinfo, loopback/private/link-local/reserved IP literals;
- rejection of hostnames resolving to blocked addresses using an injected resolver;
- revalidation of every redirect target;
- max redirect count;
- rejection of feed responses exceeding configured `Content-Length` or streamed bytes;
- truncation/rejection when entry count or source fields exceed configured limits.

**Step 2: Implement `network.py`**

Provide a testable URL policy that:

- permits only absolute HTTPS URLs without credentials;
- resolves a hostname through an injectable resolver;
- rejects non-global addresses using `ipaddress`;
- preserves a canonical URL without fragments;
- returns a safe `AppError` code rather than exposing resolver internals.

**Step 3: Implement bounded fetches**

Refactor `FeedHttpClient.get_feed` to accept the policy and byte limit. Follow redirects manually, validate each `Location`, cap redirects, use a response context manager, and reject over-limit responses before parsing.

Cap `Retry-After`, add jitter to transient retry delays, and keep retry count finite.

**Step 4: Bound parsing and persistence input**

Pass `LimitsSettings` to the sync pipeline. Limit parsed entries deterministically and reject/trim oversized text according to the chosen policy before `ArticleInput` persistence and translation work.

**Step 5: Run focused tests and Ruff**

Expected: all affected tests pass and no request can reach HTTP/private targets.

---

### Task 3: Add SQLite production pragmas, single-instance lock, and retry schema

**Files:**
- Modify: `src/rss_zen/db.py`
- Create: `src/rss_zen/runtime.py`
- Test: `tests/test_db.py`
- Test: `tests/test_scheduler.py`

**Step 1: Write failing database tests**

Add tests for:

- WAL and a configured busy timeout being applied to each connection;
- a migration adding translation retry fields without damaging existing records;
- querying due translation retries;
- marking retry success/failure and computing the next attempt;
- advisory process lock preventing a second local service instance.

**Step 2: Add migration**

Create one forward-only migration that adds retry metadata to `translations`, for example:

```sql
ALTER TABLE translations ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translations ADD COLUMN next_retry_at TEXT;
ALTER TABLE translations ADD COLUMN last_attempt_at TEXT;
ALTER TABLE translations ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0;
```

Adapt repository dataclasses and every read/write query. Initial or changed articles must have a pending, non-terminal translation state before external translation is attempted.

**Step 3: Add SQLite pragmas and database error mapping**

At connection creation, set foreign keys, WAL, a bounded busy timeout, and a safe synchronous policy. Convert expected operational failures (locked, disk full, readonly) into application errors that can be logged/retried without producing uncontrolled tracebacks.

**Step 4: Implement a cross-platform advisory lock**

Create `runtime.py` with a context manager based on an exclusive lock file. `serve` holds it for the entire lifetime; one-shot commands use a short exclusive lock only when they mutate shared state. The error must tell operators that another instance owns the database.

**Step 5: Run focused tests**

Expected: migrations, locking, and retry-state behavior pass.

---

### Task 4: Make translation recovery automatic and resilient

**Files:**
- Modify: `src/rss_zen/translation.py`
- Modify: `src/rss_zen/sync.py`
- Modify: `src/rss_zen/scheduler.py`
- Modify: `src/rss_zen/cli.py`
- Test: `tests/test_translation.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_sync.py`

**Step 1: Write failing retry tests**

Test that:

- a newly stored article is first marked pending;
- transient provider failures result in a due retry, not permanent abandonment;
- successful retry clears failure data and retry metadata;
- maximum attempts marks an item terminal;
- scheduler startup and periodic processing retry due translations;
- unchanged articles with a previous failed/pending translation are eventually retried;
- extraction is never selected by scheduler code.

**Step 2: Implement retry scheduling**

Move retry delay calculation into one deterministic helper with injected clock/random source for tests. Use bounded exponential backoff with jitter. A retryable failure advances `attempt_count` and `next_retry_at`; an authentication/configuration failure becomes terminal immediately or after a small fixed policy.

**Step 3: Update scheduler lifecycle**

Schedule a recurring due-translation job in addition to per-feed jobs. Ensure jobs do not overlap, coalesce missed intervals, and isolate all expected application/database failures. The initial sync should not prevent recovery work from starting indefinitely.

**Step 4: Preserve existing CLI semantics**

Keep `translate` as an explicit operator command, but make it able to force reprocessing selected records and report pending/terminal state accurately.

**Step 5: Run focused tests**

Expected: feed/translation recovery works without automatic AnySearch calls.

---

### Task 5: Add graceful shutdown, structured operational logging, and rich status

**Files:**
- Modify: `src/rss_zen/cli.py`
- Modify: `src/rss_zen/scheduler.py`
- Modify: `src/rss_zen/logging.py`
- Modify: `src/rss_zen/db.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_scheduler.py`

**Step 1: Write failing lifecycle/status tests**

Test handling of `SIGTERM` through a testable shutdown controller, scheduler stop/wait behavior, and `status` output containing overdue feeds, consecutive failures, pending/failed/terminal translations, and next retry time.

**Step 2: Implement graceful lifecycle management**

Add signal registration only in the CLI entry point. On SIGTERM/SIGINT, request scheduler shutdown, prevent new work, allow in-flight bounded work to finish, release the lock, and exit with an appropriate code.

**Step 3: Add structured fields**

Add event names, durations, result counts, retry metadata, and safe error codes to JSON logs. Never log secrets, authorization headers, or full untrusted response content.

**Step 4: Extend persisted and CLI status**

Persist enough feed failure state to distinguish first/transient/consecutive failures. Extend status output in a stable human-readable form and, if low-cost, provide `--json` for automation.

**Step 5: Run focused tests**

Expected: graceful shutdown and status reporting are deterministic and testable.

---

### Task 6: Harden Markdown export and provide independent export scheduling

**Files:**
- Modify: `src/rss_zen/export.py`
- Modify: `src/rss_zen/markdown.py`
- Modify: `src/rss_zen/cli.py`
- Test: `tests/test_export.py`
- Create: `deploy/systemd/rss-zen-export.service`
- Create: `deploy/systemd/rss-zen-export.timer`

**Step 1: Write failing export-security tests**

Test that untrusted titles cannot break Markdown links/headings, dangerous raw HTML is removed or escaped, and unsafe link schemes are not emitted in exported Markdown.

**Step 2: Implement safe Markdown rendering**

Escape titles and metadata rendered inline. Normalize/strip raw HTML and unsafe URL schemes while preserving regular article text, HTTPS links, headings, and code blocks as far as feasible.

**Step 3: Add an export unit and timer**

The oneshot unit invokes one named export profile using the absolute production config path. The timer has an explicit schedule, persistent catch-up behavior, and journald logging. It must not run concurrently with another export invocation.

**Step 4: Run export tests**

Expected: export functionality remains backward compatible for safe feeds and safely degrades for unsafe content.

---

### Task 7: Implement verified local SQLite backup and retention

**Files:**
- Create: `src/rss_zen/backup.py`
- Modify: `src/rss_zen/cli.py`
- Test: `tests/test_backup.py`
- Create: `deploy/systemd/rss-zen-backup.service`
- Create: `deploy/systemd/rss-zen-backup.timer`

**Step 1: Write failing backup tests**

Test that backup:

- uses SQLite's backup API;
- produces a separately readable database;
- runs `PRAGMA integrity_check` successfully;
- rejects a failed integrity check;
- retains the newest 30 daily backups and removes only older matching backup files;
- never deletes files outside the configured backup directory.

**Step 2: Implement backup service**

Create a `rss-zen backup` command. It opens the source database safely, writes a temporary dated backup through the SQLite backup API, verifies integrity, atomically publishes it, then applies retention.

**Step 3: Add systemd timer**

Run daily with `Persistent=true`; use `flock` or the shared runtime lock to avoid an unsafe collision with migrations or maintenance commands.

**Step 4: Run backup tests**

Expected: every created backup can be opened independently and exactly 30 retained artifacts remain.

---

### Task 8: Ship Linux production assets and operations documentation

**Files:**
- Create: `deploy/systemd/rss-zen.service`
- Create: `deploy/systemd/rss-zen.tmpfiles`
- Create: `deploy/systemd/rss-zen.env.example`
- Create: `docs/deployment-linux.md`
- Modify: `README.md`
- Test: `tests/test_deployment_assets.py`

**Step 1: Write static asset tests**

Test that unit files use absolute paths, a non-root user, appropriate restart behavior, secure systemd hardening directives, correct unit dependencies, and that all commands referenced by the units exist in the Typer CLI.

**Step 2: Add the `serve` systemd unit**

Define service user/group, `StateDirectory`, `RuntimeDirectory`, a fixed config path, `Restart=on-failure`, bounded restart delay, stop timeout, restrictive filesystem directives, `NoNewPrivileges`, and an explicit command using the locked production dependency set.

**Step 3: Write operator documentation**

Document prerequisites, service user setup, package installation, secret file permissions, configuration placement, install/start/enable commands, journal inspection, status commands, backup restore, upgrade, rollback, and the one-instance/local-disk restriction.

**Step 4: Update README**

Replace Windows-only setup snippets with cross-platform/production references while retaining developer instructions.

**Step 5: Run static tests**

Expected: shipped deployment assets and docs agree with the code.

---

### Task 9: Final verification and compatibility review

**Files:**
- Modify as needed from test findings only
- Test: all `tests/`

**Step 1: Run quality checks**

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .test-tmp
```

**Step 2: Test CLI smoke paths**

Use a temporary local config/database to exercise:

```powershell
rss-zen init
rss-zen status
rss-zen backup
rss-zen export daily
```

No live feed, translation provider, or AnySearch request may be made in automated tests.

**Step 3: Validate systemd assets without a live service**

On a Linux validation host, run `systemd-analyze verify` against all supplied unit files and execute the documented installation flow as the non-root `rss-zen` user.

**Step 4: Verify documentation against generated artifacts**

Confirm all documented config paths, commands, file permissions, retention count, and service/timer names match the shipped files.

**Step 5: Deliver implementation summary**

Report changed files, migration version, default behavior changes, upgrade/rollback notes, test output, and the remaining intentional scope limitations: single host, single active instance, local backups only, and manual extraction.
