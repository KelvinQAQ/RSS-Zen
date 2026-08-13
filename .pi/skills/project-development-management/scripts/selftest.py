#!/usr/bin/env python3
"""Black-box smoke test for pdm.py in an isolated temporary Git repository."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("pdm.py").resolve()


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["python3", str(SCRIPT), *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pdm-selftest-") as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / ".gitignore").write_text(".pi/project-management/\n", encoding="utf-8")
        run(root, "init", "--name", "Self Test")
        milestone = run(root, "milestone", "create", "--title", "M", "--exit-criteria", "pass")
        task = run(
            root, "task", "create", "--title", "T", "--milestone", milestone, "--acceptance", "pass"
        )
        run(root, "task", "start", task)
        run(root, "task", "finish", task, "--note", "verified")
        run(root, "milestone", "status", milestone, "completed")
        run(root, "decision", "record", "--title", "D", "--context", "C", "--decision", "X")
        run(root, "risk", "add", "--title", "R", "--severity", "low", "--mitigation", "M")
        change = run(root, "change", "create", "--title", "C", "--task", task)
        run(root, "change", "close", change, "--validation", "selftest")
        run(root, "handoff", "create", "--summary", "done", "--next", "none")
        run(root, "report", "create", "--title", "Report")
        run(root, "backup", "create", "--reason", "test")
        run(root, "doctor")
        status = json.loads(run(root, "status", "--format", "json"))
        assert status["name"] == "Self Test"
        assert status["counts"]["done"] == 1
        assert status["active_changes"] == []
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".pi/project-management/state.json"], cwd=root
        )
        assert ignored.returncode == 0
    print("project-development-management self-test passed")


if __name__ == "__main__":
    main()
