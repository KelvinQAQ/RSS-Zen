---
name: project-development-management
description: Manage long-running software development with local-only task, milestone, decision, risk, change-set, audit, checkpoint, and report data. Use when planning work, starting or finishing implementation, recording technical decisions/risks, preparing handoffs, reviewing release readiness, or reporting project progress. Actual project-management data is stored outside Git in .pi/project-management/ and must only be changed through the bundled CLI.
compatibility: Requires Python 3.11+ and a Git working tree. Uses only the Python standard library.
---

# Project Development Management

Use this skill to make project development traceable across Agent sessions without committing live
management data to Git.

## Non-negotiable storage boundary

- **Tracked implementation:** this skill directory, its scripts, and `.gitignore` rules.
- **Local-only runtime data:** `.pi/project-management/` (`state.json`, `events.jsonl`, `reports/`,
  `backups/`, `lock`). It is intentionally Git-ignored.
- Never use `write`, `edit`, or ad hoc shell redirection to modify files under
  `.pi/project-management/`.
- Use only `scripts/pdm.py`; it acquires an advisory lock, validates transitions, atomically writes
  state, appends an audit event, and snapshots before destructive reset/restore operations.
- Never put secrets, tokens, credentials, article content, full provider responses, or private URLs
  in task notes, events, decisions, or reports.

## Bootstrap and orientation

At the start of a new development session, run:

```bash
python3 .pi/skills/project-development-management/scripts/pdm.py init --name "RSS-Zen"
python3 .pi/skills/project-development-management/scripts/pdm.py status --format agent
python3 .pi/skills/project-development-management/scripts/pdm.py doctor
```

`init` is idempotent. It creates only ignored local runtime data.

Before changing code:

1. Read `status --format agent`.
2. Inspect active milestone, in-progress/blocked tasks, open risks, and active change sets.
3. Create or update a task before implementation.
4. Record an ADR-style decision before any consequential architecture/schema/compatibility choice.
5. Create a change set before edits that span multiple commits or branches.

## Required Agent workflow

### 1. Plan

```bash
# Create milestone once scope and exit criteria are agreed.
python3 .pi/skills/project-development-management/scripts/pdm.py milestone create \
  --title "v0.2 controlled operations" \
  --description "Bounded batches, resumable checkpoints, health contract, retention" \
  --exit-criteria "All quality gates pass; upgrade/restore drill passes"

# Create one small independently verifiable task.
python3 .pi/skills/project-development-management/scripts/pdm.py task create \
  --title "Add checkpoint status command" \
  --milestone m-001 --priority high \
  --description "Expose batch status without leaking article content" \
  --acceptance "CLI tests; JSON contract; docs updated"
```

### 2. Start execution

```bash
python3 .pi/skills/project-development-management/scripts/pdm.py task start t-001 \
  --branch "feature/v0.2-batch-status" \
  --note "Reviewed schema 4 and checkpoint lifecycle."

python3 .pi/skills/project-development-management/scripts/pdm.py change create \
  --title "Checkpoint status CLI" --task t-001 \
  --branch "feature/v0.2-batch-status" \
  --scope "cli, repository query, tests, docs"
```

### 3. Update while working

Use a concise, factual note at meaningful boundaries: test failure found, design changed, migration
added, validation passed, or blocked.

```bash
python3 .pi/skills/project-development-management/scripts/pdm.py task note t-001 \
  --text "TDD test added; implementation pending."

python3 .pi/skills/project-development-management/scripts/pdm.py risk add \
  --title "Schema migration rollback requires snapshot restore" \
  --severity high --task t-001 \
  --mitigation "Run WAL-aware upgrade and restore drill before release."
```

### 4. Record decisions before implementation when needed

```bash
python3 .pi/skills/project-development-management/scripts/pdm.py decision record \
  --title "Keep checkpoint runs separate from future job queue" \
  --context "Manual batch semantics differ from automatic worker scheduling." \
  --decision "Use batch_runs only for explicit operator commands." \
  --alternative "Reuse batch_run_items as generic jobs" \
  --rationale "Avoid conflating audit/recovery semantics with worker leasing." \
  --task t-001
```

### 5. Validate and finish

Before setting `done`, run project quality gates and record the exact result:

```bash
uv lock --check
uv run ruff check .
uv run pytest -q
uv run pip-audit
uv run python scripts/check_text_encoding.py

git rev-parse --short HEAD
python3 .pi/skills/project-development-management/scripts/pdm.py task finish t-001 \
  --commit "<commit>" --note "All quality gates passed."
python3 .pi/skills/project-development-management/scripts/pdm.py change close c-001 \
  --commit "<commit>" --validation "ruff; pytest; audit; encoding; diff check"
```

Do not mark a task done if its acceptance criteria, tests, docs, migration/rollback notes, or
quality gates remain incomplete. Use `review` or `blocked` instead.

## Status and reporting

```bash
# Compact human status
python3 .pi/skills/project-development-management/scripts/pdm.py status

# Stable, concise Agent context; safe to read before every significant task.
python3 .pi/skills/project-development-management/scripts/pdm.py status --format agent

# JSON for tools/automation
python3 .pi/skills/project-development-management/scripts/pdm.py status --format json

# Local markdown report; never committed.
python3 .pi/skills/project-development-management/scripts/pdm.py report create --title "v0.2 checkpoint review"
python3 .pi/skills/project-development-management/scripts/pdm.py report list

# Integrity, ignored-path, stale-task, and Git-link checks.
python3 .pi/skills/project-development-management/scripts/pdm.py doctor
```

## State rules

### Tasks

Valid statuses:

```text
todo -> in_progress -> review -> done
                    -> blocked
any non-terminal -> cancelled
```

- `done` requires a completion note; link the implementation commit when one exists.
- `blocked` requires a reason note and should usually have an open risk.
- A task with unfinished dependencies cannot be started without `--force`.
- Tasks should be small enough to review and validate independently.

### Milestones

- A milestone is `planned`, `active`, or `completed`.
- Completion is rejected while linked tasks are `todo`, `in_progress`, `review`, or `blocked`.
- Include explicit exit criteria at creation; do not use a version number alone as a milestone goal.

### Change sets

- One active change set per branch/task is preferred.
- Record branch, intended scope, linked task, commits, and validation.
- Use change sets for handoffs and PR/release readiness; do not use them as a substitute for tasks.

### Decisions and risks

- Decisions are immutable in spirit: supersede rather than silently rewrite a consequential decision.
- Risks require severity and mitigation. Close only when the mitigation is verified.
- Record a risk for migration/rollback, supplier/provider, security, data-loss, or release-blocking
  concerns.

## Handoff protocol

Before ending a substantive session:

```bash
python3 .pi/skills/project-development-management/scripts/pdm.py handoff create \
  --summary "Checkpoint status CLI implemented; tests passing; awaiting review." \
  --next "Review active change set and merge after migration restore test." \
  --blocker "None"
```

The next Agent must start with `status --format agent`, read the latest handoff, verify Git status,
and then decide whether to resume, review, or create a new task.

## Recovery

```bash
# Reset only tracking data after creating a pre-reset local snapshot.
# Source code, branches, commits, and application databases are untouched.
python3 .pi/skills/project-development-management/scripts/pdm.py reset --yes

# List automatic local snapshots.
python3 .pi/skills/project-development-management/scripts/pdm.py backup list

# Make an explicit local tracking-state snapshot.
python3 .pi/skills/project-development-management/scripts/pdm.py backup create --reason "before roadmap reset"

# Restore requires an explicit snapshot filename and confirmation.
python3 .pi/skills/project-development-management/scripts/pdm.py backup restore <filename> --yes
```

Restoring project-management data never changes source code, Git commits, or Git branches.

## References

- See [data model and CLI reference](references/data-model-and-cli.md).
- See [agent workflow checklist](references/agent-checklist.md).
