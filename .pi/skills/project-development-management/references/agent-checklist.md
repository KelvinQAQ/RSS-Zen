# Agent Checklist

## Every substantive turn

1. Run `pdm.py status --format agent` before planning or editing.
2. Verify `git status --short` and current branch separately; local tracking data is not Git truth.
3. Use the CLI to create/update a task; do not edit runtime data files.
4. Keep one task `in_progress`; mark unrelated work `todo` or create a separate change set.
5. Add a factual task note after a significant discovery, test failure, design decision, commit, or block.

## Before code changes

- Task exists, is assigned to the correct milestone, and has acceptance criteria.
- Dependencies are done/cancelled, or `--force` is justified and noted.
- Consequential design choice is recorded as a decision.
- Cross-session/multi-commit work has an active change set.

## Before a commit or PR

- Link commit to task/change set.
- Record exact validation command groups, not vague claims like “tests passed”.
- Move task to `review` if human/PR review remains; use `done` only when acceptance criteria are met.
- Generate or update a handoff if another agent/session will continue.

## Before a release

- Milestone tasks are done or explicitly cancelled.
- Open high risks are mitigated or explicitly accepted in a decision.
- Migration version, backup/restore test, upgrade note, and rollback consequence are documented.
- `doctor` reports valid local management state.

## Never do

- Never store secrets, personal data, full article bodies, provider response bodies, or authorization headers.
- Never commit `.pi/project-management/`.
- Never directly mutate state.json/events.jsonl/reports/backups.
- Never mark work done merely because code was written; require test and acceptance evidence.
