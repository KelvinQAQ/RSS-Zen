# RSS-Zen Roadmap

RSS-Zen is intentionally a single-host, local-SQLite application. Each release has a
narrow scope and explicit exit criteria; `main` must remain releasable throughout.

## Versioning and branch policy

- Patch releases (`0.1.x`) contain security fixes, bug fixes, documentation, and operational
  corrections that preserve user-facing behavior.
- Minor releases (`0.x.0`) add cohesive, documented capabilities. In the pre-1.0 phase they may
  make carefully documented configuration or CLI changes.
- Develop each feature on a short-lived `feature/v<version>-<topic>` branch. Merge only after
  CI passes, then delete the branch.
- Emergency fixes use `hotfix/<version>-<topic>` from `main` and are merged back immediately.
- Every release records its supported schema version, upgrade/rollback notes, and validation
  commands in `docs/releases/`.

## v0.1.1 — Security and operational hardening

**Status:** released from commit `6a36c56`.

- Verified SQLite online backups before schema migrations and bounded backup retention.
- Validated curl redirects and feed request headers, with sensitive headers sourced from
  environment credentials.
- Batch-size controls, backup configuration unification, stricter deployment permissions,
  BOM checks, dependency audit, and automated dependency update proposals.

See [v0.1.1 release notes](releases/v0.1.1.md).

## v0.2.0 — Controlled operations

**Goal:** make bounded manual processing and single-host operations measurable, resumable, and
safe without introducing a job queue.

1. Provider request/character budgets and deterministic batch reports.
2. Stable `status --json` and `doctor --json` operational contracts, including feed freshness,
   database/WAL size, disk capacity, backup health, and configuration permissions.
3. Explicit retention preview/apply commands with backup-before-delete behavior.
4. Batch checkpoints and `--resume` for interrupted operator commands.
5. Optional systemd health-check timer and documented alert integration.

**Out of scope:** automatic extraction, distributed workers, Redis/Celery, multi-host service
operation, or replacing SQLite.

See the [v0.2.0 scope and exit criteria](releases/v0.2.0.md).

## v0.3.0 — Durable processing jobs

**Goal:** decouple feed ingestion from translation and extraction only after v0.2 operational
metrics show the need.

- Split repository and CLI boundaries before changing scheduling semantics.
- Add a SQLite jobs table with lease-based claims and crash recovery.
- Move translation to bounded workers with provider limits, retry scheduling, and circuit
  breaker state.
- Evaluate extraction jobs only after translation jobs are stable.

See [ADR-0002](adr/0002-single-host-sqlite-job-queue.md). This version must include migration,
recovery, and rollback drills.

## v0.4.0 — Trusted delivery

**Goal:** deploy verified release artifacts rather than arbitrary source trees.

- CI-built artifacts with commit SHA, lockfile hash, and SBOM metadata.
- Artifact integrity/signature verification before atomic deployment.
- Internal mirror or artifact repository for deployments where direct GitHub access is unreliable.
- Encrypted off-host backup and a documented restore drill.
