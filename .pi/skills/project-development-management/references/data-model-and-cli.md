# Data Model and CLI Reference

## Local-only runtime layout

```text
.pi/project-management/
├── state.json          # Current materialized state, atomically replaced
├── events.jsonl        # Append-only audit log
├── lock                # Advisory lock file
├── reports/            # Local markdown reports
└── backups/            # Local management-state snapshots
```

The entire directory is Git-ignored. Runtime data can contain project names, local branch names,
work notes, and development timing; keep it local and free of secrets.

## Core commands

```bash
pdm.py init [--name NAME]
pdm.py status [--format text|agent|json]
pdm.py doctor
pdm.py report create --title TITLE
pdm.py report list
pdm.py handoff create --summary TEXT --next TEXT [--blocker TEXT]
pdm.py backup create [--reason TEXT]
pdm.py backup list
pdm.py backup restore FILE --yes
```

## Task commands

```bash
pdm.py task create --title TITLE [--description TEXT] [--acceptance TEXT] \
  [--milestone M-001] [--priority low|medium|high|critical] [--depends-on T-001,T-002]
pdm.py task start T-001 [--branch NAME] [--note TEXT] [--force]
pdm.py task status T-001 todo|in_progress|review|done|blocked|cancelled [--note TEXT]
pdm.py task finish T-001 [--commit SHA] --note TEXT
pdm.py task note T-001 --text TEXT
pdm.py task link-commit T-001 [--commit SHA]
pdm.py task list [--status STATUS] [--milestone M-001]
pdm.py task show T-001
```

## Milestones, decisions, risks, changes

```bash
pdm.py milestone create --title TITLE [--description TEXT] [--exit-criteria TEXT]
pdm.py milestone status M-001 planned|active|completed
pdm.py milestone list

pdm.py decision record --title TITLE --context TEXT --decision TEXT \
  [--alternative TEXT ...] [--rationale TEXT] [--task T-001]
pdm.py decision list

pdm.py risk add --title TITLE --severity low|medium|high --mitigation TEXT [--task T-001]
pdm.py risk status R-001 open|mitigated|closed [--note TEXT]
pdm.py risk list

pdm.py change create --title TITLE --task T-001 [--branch NAME] [--scope TEXT]
pdm.py change status C-001 planned|active|review|closed|abandoned [--note TEXT]
pdm.py change close C-001 [--commit SHA] --validation TEXT
pdm.py change list
```

## State schema principles

- IDs are monotonic and never reused: `t-001`, `m-001`, `d-001`, `r-001`, `c-001`, `h-001`.
- Timestamps are UTC ISO 8601 strings.
- Every mutation appends an event with actor, type, timestamp, and safe metadata.
- `state.json` is a cache/materialized view; `events.jsonl` is the audit trail.
- Schema version changes require a migrator in the CLI; invalid state is never silently overwritten.
