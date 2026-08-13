# ADR-0001: Release and schema compatibility policy

**Status:** Accepted  
**Date:** 2026-08-12

## Context

RSS-Zen uses forward-only SQLite migrations and is deployed as a single local service. A code
rollback may be unsafe after a schema migration. Feature work also ranges from low-risk CLI
improvements to scheduling architecture changes.

## Decision

- Keep `main` releasable; use short-lived feature and hotfix branches.
- Keep patch releases migration-free whenever possible.
- Give each minor version a documented scope, out-of-scope list, exit criteria, schema version,
  upgrade notes, and rollback consequences under `docs/releases/`.
- Require WAL-aware pre-migration snapshot and restoration tests for every schema change.
- Treat JSON CLI payloads consumed by automation as versioned public operational contracts.

## Consequences

- Large queue/worker changes cannot be merged as an unfinished feature.
- A release may be delayed until upgrade and restore drills pass.
- Operators can determine when code-only rollback is safe and when the matching pre-migration
  snapshot is required.
