"""Session spawning — launch one fresh `claude -p` per task in its own worktree.
Ports session.sh. Replaces the bash `.running` PID file with an in-memory
RunningSession handle the loop supervises via Popen.poll()."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import Popen

from .config import Config
from .paths import Paths
from .tasks import Task, set_status, task_index
from .worktree import DependencyMergeConflict, branch_name, make_worktree


@dataclass
class RunningSession:
    sid: str
    tid: str
    sdir: Path
    worktree: Path | None
    task: Task
    proc: Popen | None

    @property
    def result_file(self) -> Path:
        return self.sdir / "result.json"

    def has_result(self) -> bool:
        return self.result_file.exists()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"sess-{stamp}-{uuid.uuid4().hex[:8]}"


def build_prompt(sdir: str, task_file: str, wt: str, tid: str) -> str:
    return (
        f"You are an autobuild session. Your session directory is: {sdir}\n"
        f"Your assigned task file is: {task_file}\n"
        f"You are working inside an isolated git worktree at: {wt}\n\n"
        "Read GOAL.md and CLAUDE.md in this worktree for your contract, then follow\n"
        f"plan -> review -> implement and finish by writing {sdir}/result.json.\n"
        f"Work ONLY on task {tid}. Do everything from within {wt}.\n"
    )


def write_sentinel(sdir: Path, tid: str, status: str, summary: str,
                   commit: str = "", followups: list | None = None) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "result.json").write_text(
        json.dumps({"task": tid, "status": status, "summary": summary,
                    "commit": commit, "followups": followups or []}, indent=2),
        encoding="utf-8",
    )


def _done_dependencies(task: Task, paths: Paths) -> list[str]:
    """Dependency ids whose task is `done`, in declaration order. The scheduler only
    makes a task runnable once every dep is done, but filter defensively so a direct
    spawn never tries to layer an unfinished (or missing) dependency."""
    index = task_index(paths.tasks_dir)
    return [d for d in task.depends_on
            if (dep := index.get(d)) is not None and dep.status == "done"]


def spawn_session(task: Task, config: Config, paths: Paths) -> RunningSession:
    sid = new_session_id()
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True, exist_ok=True)
    tid = task.id

    try:
        wt = make_worktree(paths, sid, tid, config.base_branch,
                           _done_dependencies(task, paths))
    except DependencyMergeConflict as exc:
        set_status(task.path, "blocked")
        write_sentinel(sdir, tid, "BLOCKED", str(exc))
        return RunningSession(sid, tid, sdir, None, task, None)
    except Exception:
        set_status(task.path, "blocked")
        write_sentinel(sdir, tid, "BLOCKED",
                       "worktree creation failed; check base_branch in config.yml")
        return RunningSession(sid, tid, sdir, None, task, None)

    (sdir / "meta.json").write_text(json.dumps({
        "session": sid,
        "task": tid,
        "task_file": str(task.path),
        "worktree": str(wt),
        "branch": branch_name(tid),
        "status": "in-progress",
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2), encoding="utf-8")
    set_status(task.path, "in-progress")

    prompt = build_prompt(str(sdir), str(task.path), str(wt), tid)
    out = open(sdir / "session.out", "w")
    err = open(sdir / "session.err", "w")
    try:
        proc = Popen(
            [config.claude_cmd, "-p", prompt, "--model", config.model],
            cwd=str(wt), stdout=out, stderr=err,
        )
    except FileNotFoundError:
        out.close()
        err.close()
        # No Claude CLI on PATH: emit NEEDS_HUMAN so the reaper has something
        # deterministic to act on instead of hanging.
        write_sentinel(sdir, tid, "NEEDS_HUMAN",
                       f"claude CLI '{config.claude_cmd}' not found on PATH; cannot run session")
        return RunningSession(sid, tid, sdir, wt, task, None)

    return RunningSession(sid, tid, sdir, wt, task, proc)
