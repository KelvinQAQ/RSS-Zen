#!/usr/bin/env python3
"""Local-only, agent-safe project development tracking CLI (stdlib only)."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DATA_RELATIVE = Path(".pi/project-management")
TASK_STATES = {"todo", "in_progress", "review", "done", "blocked", "cancelled"}
MILESTONE_STATES = {"planned", "active", "completed"}
RISK_STATES = {"open", "mitigated", "closed"}
CHANGE_STATES = {"planned", "active", "review", "closed", "abandoned"}
PRIORITIES = {"low", "medium", "high", "critical"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def root() -> Path:
    try:
        output = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return Path(output)
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def data_dir() -> Path:
    return root() / DATA_RELATIVE


def paths() -> dict[str, Path]:
    base = data_dir()
    return {"base": base, "state": base / "state.json", "events": base / "events.jsonl", "lock": base / "lock", "reports": base / "reports", "backups": base / "backups"}


def default_state(name: str = "Untitled Project") -> dict[str, Any]:
    stamp = now()
    return {"version": SCHEMA_VERSION, "name": name, "created_at": stamp, "updated_at": stamp,
            "tasks": {}, "milestones": {}, "decisions": {}, "risks": {}, "changes": {}, "handoffs": {},
            "counters": {"task": 0, "milestone": 0, "decision": 0, "risk": 0, "change": 0, "handoff": 0}}


def validate(state: dict[str, Any]) -> None:
    if state.get("version") != SCHEMA_VERSION or not isinstance(state.get("counters"), dict):
        raise ValueError("unsupported or invalid project-management state schema")
    for key in ("tasks", "milestones", "decisions", "risks", "changes", "handoffs"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"invalid state: {key} must be an object")


@contextmanager
def locked_state() -> Iterator[tuple[dict[str, Any], callable]]:
    p = paths()
    p["base"].mkdir(parents=True, exist_ok=True)
    p["reports"].mkdir(exist_ok=True)
    p["backups"].mkdir(exist_ok=True)
    with p["lock"].open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if p["state"].exists():
            state = json.loads(p["state"].read_text(encoding="utf-8"))
            validate(state)
        else:
            state = default_state()
        changed = False
        def mark() -> None:
            nonlocal changed
            changed = True
        try:
            yield state, mark
            if changed:
                state["updated_at"] = now()
                atomic_json(p["state"], state)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2, sort_keys=True)
            out.write("\n")
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def event(kind: str, data: dict[str, Any], actor: str = "agent") -> None:
    p = paths()["events"]
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"ts": now(), "type": kind, "actor": actor, "data": data}, ensure_ascii=False, sort_keys=True) + "\n")


def ident(state: dict[str, Any], kind: str, prefix: str) -> str:
    state["counters"][kind] += 1
    return f"{prefix}-{state['counters'][kind]:03d}"


def require(table: dict[str, Any], item_id: str, label: str) -> dict[str, Any]:
    if item_id not in table:
        raise ValueError(f"{label} not found: {item_id}")
    return table[item_id]


def git_info() -> dict[str, str | None]:
    def run(args: list[str]) -> str | None:
        try: return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip() or None
        except (OSError, subprocess.CalledProcessError): return None
    return {"branch": run(["git", "branch", "--show-current"]), "commit": run(["git", "rev-parse", "HEAD"])}


def task_progress(state: dict[str, Any], milestone: dict[str, Any]) -> tuple[int, int, int]:
    ids = milestone["task_ids"]
    done = sum(state["tasks"].get(i, {}).get("status") == "done" for i in ids)
    cancelled = sum(state["tasks"].get(i, {}).get("status") == "cancelled" for i in ids)
    return len(ids), done, cancelled


def cmd_init(args: argparse.Namespace) -> None:
    existed = paths()["state"].exists()
    with locked_state() as (state, mark):
        if args.name and state["name"] == "Untitled Project":
            state["name"] = args.name
            mark()
        if not existed:
            mark()
    if not existed:
        event("project.init", {"root": str(root())}, "system")
    print(f"initialized local tracking at {data_dir()}")


def cmd_task(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        if args.action == "create":
            if args.milestone: require(s["milestones"], args.milestone, "milestone")
            deps = [x for x in (args.depends_on or "").split(",") if x]
            item = {"id": ident(s, "task", "t"), "title": args.title, "description": args.description or "", "acceptance": args.acceptance or "", "milestone_id": args.milestone, "priority": args.priority, "depends_on": deps, "status": "todo", "branch": None, "commits": [], "notes": [], "created_at": now(), "updated_at": now(), "completed_at": None}
            s["tasks"][item["id"]] = item
            if args.milestone: s["milestones"][args.milestone]["task_ids"].append(item["id"])
            mark(); event("task.create", {"id": item["id"], "title": item["title"]}); print(item["id"]); return
        task = require(s["tasks"], args.id, "task")
        if args.action == "start":
            unfinished = [i for i in task["depends_on"] if s["tasks"].get(i, {}).get("status") not in {"done", "cancelled"}]
            if unfinished and not args.force: raise ValueError(f"unfinished dependencies: {', '.join(unfinished)}")
            task["status"] = "in_progress"; task["branch"] = args.branch or git_info()["branch"]
        elif args.action == "status":
            if args.status not in TASK_STATES: raise ValueError("invalid task status")
            task["status"] = args.status
            if args.status == "done": task["completed_at"] = now()
        elif args.action == "finish":
            task["status"] = "done"; task["completed_at"] = now()
            if args.commit: task["commits"].append(args.commit)
        elif args.action == "note":
            task["notes"].append({"ts": now(), "text": args.text})
        elif args.action == "link-commit":
            commit = args.commit or git_info()["commit"]
            if not commit: raise ValueError("no git commit available")
            if commit not in task["commits"]: task["commits"].append(commit)
        elif args.action == "show": print(json.dumps(task, ensure_ascii=False, indent=2)); return
        elif args.action == "list":
            rows = [v for v in s["tasks"].values() if (not args.status or v["status"] == args.status) and (not args.milestone or v["milestone_id"] == args.milestone)]
            print("\n".join(f"{x['id']} [{x['status']}/{x['priority']}] {x['title']}" for x in rows) or "(none)"); return
        if args.note: task["notes"].append({"ts": now(), "text": args.note})
        task["updated_at"] = now(); mark(); event(f"task.{args.action}", {"id": task["id"], "status": task["status"]}); print(task["id"])


def cmd_milestone(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        if args.action == "create":
            item = {"id": ident(s, "milestone", "m"), "title": args.title, "description": args.description or "", "exit_criteria": args.exit_criteria or "", "status": "planned", "task_ids": [], "created_at": now(), "updated_at": now(), "completed_at": None}
            s["milestones"][item["id"]] = item; mark(); event("milestone.create", {"id": item["id"]}); print(item["id"]); return
        if args.action == "list":
            print("\n".join(f"{m['id']} [{m['status']}] {m['title']}" for m in s["milestones"].values()) or "(none)"); return
        item = require(s["milestones"], args.id, "milestone")
        status = args.status_value or args.status
        if status not in MILESTONE_STATES: raise ValueError("invalid milestone status")
        if status == "completed":
            open_tasks = [i for i in item["task_ids"] if s["tasks"][i]["status"] not in {"done", "cancelled"}]
            if open_tasks: raise ValueError(f"unfinished milestone tasks: {', '.join(open_tasks)}")
            item["completed_at"] = now()
        item["status"] = status; item["updated_at"] = now(); mark(); event("milestone.status", {"id": item["id"], "status": status}); print(item["id"])


def cmd_decision(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        if args.action == "list": print("\n".join(f"{d['id']} {d['title']}: {d['decision']}" for d in s["decisions"].values()) or "(none)"); return
        tasks = args.task or []
        for task_id in tasks: require(s["tasks"], task_id, "task")
        item = {"id": ident(s, "decision", "d"), "title": args.title, "context": args.context, "decision": args.decision, "alternatives": args.alternative or [], "rationale": args.rationale or "", "task_ids": tasks, "status": "active", "created_at": now()}
        s["decisions"][item["id"]] = item; mark(); event("decision.record", {"id": item["id"], "title": item["title"]}); print(item["id"])


def cmd_risk(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        if args.action == "list": print("\n".join(f"{r['id']} [{r['severity']}/{r['status']}] {r['title']}" for r in s["risks"].values()) or "(none)"); return
        if args.action == "add":
            item = {"id": ident(s, "risk", "r"), "title": args.title, "severity": args.severity, "status": "open", "mitigation": args.mitigation or "", "task_ids": args.task or [], "notes": [], "created_at": now(), "updated_at": now()}; s["risks"][item["id"]] = item
        else:
            item = require(s["risks"], args.id, "risk")
            status = args.status_value or args.status
            if status not in RISK_STATES: raise ValueError("invalid risk status")
            item["status"] = status; item["updated_at"] = now()
            if args.note: item["notes"].append({"ts": now(), "text": args.note})
        mark(); event(f"risk.{args.action}", {"id": item["id"], "status": item["status"]}); print(item["id"])


def cmd_change(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        if args.action == "list": print("\n".join(f"{c['id']} [{c['status']}] {c['title']}" for c in s["changes"].values()) or "(none)"); return
        if args.action == "create":
            require(s["tasks"], args.task, "task")
            item = {"id": ident(s, "change", "c"), "title": args.title, "task_id": args.task, "branch": args.branch or git_info()["branch"], "scope": args.scope or "", "status": "active", "commits": [], "validation": "", "notes": [], "created_at": now(), "updated_at": now()}; s["changes"][item["id"]] = item
        else:
            item = require(s["changes"], args.id, "change")
            if args.action == "close": item["status"] = "closed"; item["validation"] = args.validation
            else:
                status = args.status_value or args.status
                if status not in CHANGE_STATES: raise ValueError("invalid change status")
                item["status"] = status
            commit = args.commit or (git_info()["commit"] if args.action == "close" else None)
            if commit and commit not in item["commits"]: item["commits"].append(commit)
            if args.note: item["notes"].append({"ts": now(), "text": args.note})
            item["updated_at"] = now()
        mark(); event(f"change.{args.action}", {"id": item["id"], "status": item["status"]}); print(item["id"])


def cmd_status(args: argparse.Namespace) -> None:
    with locked_state() as (s, _):
        tasks = list(s["tasks"].values()); active = [t for t in tasks if t["status"] in {"in_progress", "review", "blocked"}]
        payload = {"schema_version": 1, "name": s["name"], "updated_at": s["updated_at"], "git": git_info(), "counts": {state: sum(t["status"] == state for t in tasks) for state in TASK_STATES}, "active_tasks": active, "open_risks": [r for r in s["risks"].values() if r["status"] == "open"], "active_changes": [c for c in s["changes"].values() if c["status"] in {"active", "review"}], "latest_handoff": next(reversed(s["handoffs"].values()), None) if s["handoffs"] else None}
        if args.format == "json": print(json.dumps(payload, ensure_ascii=False, indent=2)); return
        if args.format == "agent":
            print("PROJECT TRACKING (local-only): " + s["name"])
            print("active=" + ", ".join(f"{t['id']}:{t['status']}:{t['title']}" for t in active[:8]) or "active=none")
            print("risks=" + ", ".join(f"{r['id']}:{r['severity']}:{r['title']}" for r in payload["open_risks"][:5]) or "risks=none")
            print("next=" + (payload["latest_handoff"] or {}).get("next", "review active tasks/change sets")); return
        print(f"{s['name']}: " + " ".join(f"{k}={v}" for k, v in payload["counts"].items()))


def cmd_handoff(args: argparse.Namespace) -> None:
    with locked_state() as (s, mark):
        item = {"id": ident(s, "handoff", "h"), "summary": args.summary, "next": args.next, "blocker": args.blocker or "", "git": git_info(), "created_at": now()}; s["handoffs"][item["id"]] = item; mark(); event("handoff.create", {"id": item["id"]}); print(item["id"])


def cmd_report(args: argparse.Namespace) -> None:
    p = paths()
    if args.action == "list": print("\n".join(x.name for x in sorted(p["reports"].glob("*.md"), reverse=True)) or "(none)"); return
    with locked_state() as (s, _):
        report = f"# {args.title}\n\n- Generated: {now()}\n- Project: {s['name']}\n\n## Task counts\n\n" + "\n".join(f"- {st}: {sum(t['status'] == st for t in s['tasks'].values())}" for st in sorted(TASK_STATES)) + "\n\n## Active tasks\n\n" + "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in s['tasks'].values() if t['status'] in {'in_progress','review','blocked'}) + "\n"
    path = p["reports"] / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.title[:40].replace(' ', '-')}.md"; path.write_text(report, encoding="utf-8"); event("report.create", {"path": path.name}); print(path)


def cmd_backup(args: argparse.Namespace) -> None:
    p = paths(); p["backups"].mkdir(parents=True, exist_ok=True)
    if args.action == "list": print("\n".join(x.name for x in sorted(p["backups"].glob("*.json"), reverse=True)) or "(none)"); return
    if args.action == "create":
        with locked_state() as (s, _):
            target = p["backups"] / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.reason[:24].replace(' ', '-')}.json"; atomic_json(target, s); event("backup.create", {"file": target.name}); print(target.name); return
    if not args.yes: raise ValueError("backup restore requires --yes")
    source = p["backups"] / args.file
    if not source.is_file(): raise ValueError("backup file not found")
    restored = json.loads(source.read_text(encoding="utf-8")); validate(restored)
    with locked_state() as (_s, mark):
        atomic_json(p["backups"] / f"pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json", _s)
        _s.clear(); _s.update(restored); mark()
    event("backup.restore", {"file": source.name}, "user"); print(source.name)


def cmd_doctor(_args: argparse.Namespace) -> None:
    p = paths(); issues: list[str] = []
    try:
        with locked_state() as (s, _): validate(s)
    except Exception as error: issues.append(f"state: {error}")
    ignored = subprocess.run(["git", "check-ignore", "-q", str(DATA_RELATIVE)], cwd=root()).returncode == 0
    if not ignored: issues.append(f"gitignore: {DATA_RELATIVE} is not ignored")
    if issues:
        print("ERROR\n" + "\n".join(f"- {item}" for item in issues)); raise SystemExit(1)
    print("OK\n- state schema valid\n- local runtime directory is Git-ignored\n- atomic state/audit CLI available")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdm.py"); sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init"); q.add_argument("--name"); q.set_defaults(func=cmd_init)
    q = sub.add_parser("status"); q.add_argument("--format", choices=["text", "agent", "json"], default="text"); q.set_defaults(func=cmd_status)
    q = sub.add_parser("doctor"); q.set_defaults(func=cmd_doctor)
    q = sub.add_parser("task"); ts = q.add_subparsers(dest="action", required=True)
    for a in ["create", "start", "status", "finish", "note", "link-commit", "show", "list"]:
        x = ts.add_parser(a); x.add_argument("id", nargs="?"); x.add_argument("--title"); x.add_argument("--description"); x.add_argument("--acceptance"); x.add_argument("--milestone"); x.add_argument("--priority", choices=PRIORITIES, default="medium"); x.add_argument("--depends-on"); x.add_argument("--branch"); x.add_argument("--note"); x.add_argument("--text"); x.add_argument("--commit"); x.add_argument("--status", choices=TASK_STATES); x.add_argument("--force", action="store_true"); x.set_defaults(func=cmd_task)
    q = sub.add_parser("milestone"); ms = q.add_subparsers(dest="action", required=True)
    for a in ["create", "status", "list"]:
        x = ms.add_parser(a); x.add_argument("id", nargs="?"); x.add_argument("status_value", nargs="?", choices=MILESTONE_STATES); x.add_argument("--title"); x.add_argument("--description"); x.add_argument("--exit-criteria"); x.add_argument("--status", choices=MILESTONE_STATES); x.set_defaults(func=cmd_milestone)
    q = sub.add_parser("decision"); ds = q.add_subparsers(dest="action", required=True)
    for a in ["record", "list"]:
        x = ds.add_parser(a); x.add_argument("--title"); x.add_argument("--context", default=""); x.add_argument("--decision", default=""); x.add_argument("--alternative", action="append"); x.add_argument("--rationale"); x.add_argument("--task", action="append"); x.set_defaults(func=cmd_decision)
    q = sub.add_parser("risk"); rs = q.add_subparsers(dest="action", required=True)
    for a in ["add", "status", "list"]:
        x = rs.add_parser(a); x.add_argument("id", nargs="?"); x.add_argument("status_value", nargs="?", choices=RISK_STATES); x.add_argument("--title"); x.add_argument("--severity", choices=["low","medium","high"], default="medium"); x.add_argument("--mitigation"); x.add_argument("--task", action="append"); x.add_argument("--status", choices=RISK_STATES); x.add_argument("--note"); x.set_defaults(func=cmd_risk)
    q = sub.add_parser("change"); cs = q.add_subparsers(dest="action", required=True)
    for a in ["create", "status", "close", "list"]:
        x = cs.add_parser(a); x.add_argument("id", nargs="?"); x.add_argument("status_value", nargs="?", choices=CHANGE_STATES); x.add_argument("--title"); x.add_argument("--task"); x.add_argument("--branch"); x.add_argument("--scope"); x.add_argument("--status", choices=CHANGE_STATES); x.add_argument("--note"); x.add_argument("--commit"); x.add_argument("--validation", default=""); x.set_defaults(func=cmd_change)
    q = sub.add_parser("handoff"); hs = q.add_subparsers(dest="action", required=True); x = hs.add_parser("create"); x.add_argument("--summary", required=True); x.add_argument("--next", required=True); x.add_argument("--blocker"); x.set_defaults(func=cmd_handoff)
    q = sub.add_parser("report"); rs = q.add_subparsers(dest="action", required=True); x = rs.add_parser("create"); x.add_argument("--title", required=True); x.set_defaults(func=cmd_report); rs.add_parser("list").set_defaults(func=cmd_report)
    q = sub.add_parser("backup"); bs = q.add_subparsers(dest="action", required=True); x = bs.add_parser("create"); x.add_argument("--reason", default="manual"); x.set_defaults(func=cmd_backup); bs.add_parser("list").set_defaults(func=cmd_backup); x = bs.add_parser("restore"); x.add_argument("file"); x.add_argument("--yes", action="store_true"); x.set_defaults(func=cmd_backup)
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

if __name__ == "__main__": main()
