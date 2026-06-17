"""Session spawning — launch one fresh `claude -p` per task in its own worktree.
Ports session.sh. Replaces the bash `.running` PID file with an in-memory
RunningSession handle the loop supervises via Popen.poll()."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import Popen

from .config import Config
from .paths import Paths
from .tasks import Task, set_status, task_index
from .worktree import WorktreeError, branch_name, make_worktree


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


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to `path` atomically: serialize to a temp file in the SAME directory
    (so the swap stays on one filesystem), then `os.replace` it over the target. A
    concurrent reader therefore only ever sees the old complete file or the new one —
    never a half-written sentinel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _result_is_parseable(sdir: Path) -> bool:
    """True if result.json already holds a usable JSON object — a real result the
    harness must not clobber. Mirrors loop._classify_sentinel's "reapable" rule (a valid
    leading object plus stray trailing junk still counts); kept local so this lower-level
    module keeps no upward import to loop.py."""
    try:
        text = (sdir / "result.json").read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        obj = json.loads(text)
    except ValueError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            return False
    return isinstance(obj, dict)


def write_sentinel(sdir: Path, tid: str, status: str, summary: str,
                   commit: str = "", followups: list | None = None) -> None:
    """Write the result.json sentinel atomically (temp + os.replace), overwriting any
    existing file. The atomic primitive; harness-authored paths use the guarded
    `write_sentinel_if_absent` instead."""
    sdir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(sdir / "result.json",
                       {"task": tid, "status": status, "summary": summary,
                        "commit": commit, "followups": followups or []})


def write_sentinel_if_absent(sdir: Path, tid: str, status: str, summary: str,
                             commit: str = "", followups: list | None = None) -> bool:
    """Write a sentinel ONLY when it is safe to: refuse if the session was already
    reaped (reaped.json) or a parseable result.json already exists; otherwise write it
    atomically. Returns True if written, False if refused.

    Every HARNESS-authored sentinel (worktree-creation BLOCK, exited-without-result
    BLOCK, orphan BLOCK, a missing-CLI NEEDS_HUMAN) goes through this, so a late-arriving
    real agent result — or a re-run over an already-reaped session — is never clobbered."""
    if (sdir / "reaped.json").exists():
        return False
    if _result_is_parseable(sdir):
        return False
    write_sentinel(sdir, tid, status, summary, commit, followups)
    return True


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
    except WorktreeError as exc:
        # a dependency-merge conflict OR a non-conflict merge failure — surface the
        # specific, actionable reason in the sentinel rather than a generic message.
        set_status(task.path, "blocked")
        write_sentinel_if_absent(sdir, tid, "BLOCKED", str(exc))
        return RunningSession(sid, tid, sdir, None, task, None)
    except Exception:
        set_status(task.path, "blocked")
        write_sentinel_if_absent(sdir, tid, "BLOCKED",
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
        write_sentinel_if_absent(sdir, tid, "NEEDS_HUMAN",
                                 f"claude CLI '{config.claude_cmd}' not found on PATH; cannot run session")
        return RunningSession(sid, tid, sdir, wt, task, None)

    return RunningSession(sid, tid, sdir, wt, task, proc)
