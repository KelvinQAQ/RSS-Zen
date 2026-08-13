# ADR-0002: Keep v0.2 synchronous; defer durable jobs to v0.3

**Status:** Accepted  
**Date:** 2026-08-12

## Context

Feed synchronization currently stores changed articles and translates them synchronously. A
lease-based SQLite job queue could improve isolation and restart recovery, but it changes the
database schema, scheduler behavior, service lifecycle, observability, and recovery semantics.
Current operations are bounded manual batches rather than a real-time processing SLA.

## Decision

- v0.2 improves bounded batch accounting, checkpoints, retention, and health visibility without
  changing the synchronous service architecture.
- v0.3 may introduce a `jobs` table only after repository/CLI boundaries are split and v0.2
  metrics demonstrate that synchronous processing misses operational targets.
- The future queue uses SQLite lease-based claims, bounded workers, and recovery of expired
  leases; it does not introduce Redis, Celery, or multi-host workers.

## Consequences

- v0.2 remains lower risk and can ship independent operational improvements quickly.
- v0.3 requires dedicated schema migration, crash/recovery tests, provider rate-limit semantics,
  and a tested rollback/restore process.
