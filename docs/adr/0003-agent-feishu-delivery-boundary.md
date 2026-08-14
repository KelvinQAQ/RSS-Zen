# ADR-0003: Separate deterministic delivery from Pi editorial control

**Status:** Accepted  
**Date:** 2026-08-14

## Context

RSS-Zen is intended to deliver topic-oriented Chinese news editions to Feishu before a daily
`Asia/Shanghai` deadline. Pi Agent should let the user manage feeds and request editorial work in
natural language, but model availability, Agent sessions, provider latency, and tool execution are
not reliable scheduling or delivery primitives.

A one-way Feishu webhook would support an initial notification but would not provide the desired
path for authenticated inbound operations. Allowing Pi arbitrary shell, configuration-file, or
SQLite access in production would also make routine Agent operation unsafe and difficult to audit.

## Decision

- Use a **Feishu custom app bot** as the first delivery and future interaction channel.
- RSS-Zen owns deadline state, deterministic rendering, cost/rate limits, a durable delivery outbox,
  retry policy, idempotency, and delivery evidence.
- Pi owns bounded natural-language interpretation and editorial transformation. It receives
  minimized structured inputs and returns versioned structured outputs.
- Pi never sends production messages directly. A failed or late Pi editorial step selects a
  deterministic RSS-Zen fallback edition.
- Inbound Feishu events are authenticated, deduplicated, allowlisted, and converted into typed
  RSS-Zen operations before Pi can act.
- Pi has no arbitrary production shell, raw SQLite, root, credential-file, destructive maintenance,
  or unrestricted configuration-write capability.
- Daily editions prefer extracted full text already present, then RSS content, then RSS summary;
  they do not automatically request full-text extraction. Explicit selected-article extraction
  remains available.
- Article count is relevance-driven rather than fixed, subject to safety ceilings.
- Cost controls support observe/warn/enforce modes. Concrete financial limits are deferred until
  production usage is measured, but non-financial disaster ceilings are mandatory from the first
  release.

## Alternatives

### Let Pi schedule, generate, and deliver directly

Rejected because Agent sessions and model calls do not provide durable deadlines, exactly-once
semantics, bounded retries, or a deterministic fallback.

### Use a one-way Feishu group webhook

Rejected as the product boundary because the desired roadmap includes authenticated commands such
as adding feeds, requesting selected full-text extraction, and adjusting topics. The custom app bot
avoids replacing the identity and authorization model later.

### Require full-text extraction for every selected article

Rejected because RSS content is often sufficient and automatic extraction adds anti-bot, latency,
provider, and cost failure modes to the delivery deadline.

### Fix a daily article count and hard-code budgets immediately

Rejected because useful material varies by day and no production evidence exists yet for safe cost
thresholds. Dynamic selection still has enforceable technical ceilings.

## Consequences

- v0.3 needs edition and delivery state, a Feishu adapter, Agent-safe command contracts, usage
  accounting, and deadline-aware recovery in addition to the previously proposed job queue.
- The daily pipeline can deliver a degraded or empty-but-successful edition when external systems
  fail or no material qualifies.
- Feishu credentials stay in the existing systemd credential boundary and never enter Pi prompts,
  reports, or persisted message payloads.
- Two-way Feishu operation is implemented only after outbound delivery is stable and permission,
  event-verification, allowlist, and confirmation tests pass.
- SQLite remains the single-host durable store; Redis/Celery and multi-host operation remain out of
  scope.

See [the v0.3 scope and acceptance plan](../plans/2026-08-14-v0.3-agent-feishu-digest.md).
