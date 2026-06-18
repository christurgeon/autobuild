"""Session spawning — launch one fresh `claude -p` per task in its own worktree.
Returns an in-memory RunningSession handle the loop supervises via Popen.poll();
all crash-recovery state lives in files (meta.json) + git, never in memory alone."""

from __future__ import annotations

import json
import os
import sys
import time
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
    pgid: int | None = None
    deadline: float | None = None  # monotonic; set in spawn_session for a launched child

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
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # On success os.replace already consumed tmp; on any failure (write or swap)
        # drop the scratch file so a failed write never leaks a stray .tmp.
        tmp.unlink(missing_ok=True)


def parse_leading_json(text: str) -> dict | None:
    """Parse `text` as a JSON object, tolerating stray trailing data after a valid
    leading object — agents sometimes append output (e.g. a closing tag) after the
    sentinel. Returns the object, or None if there is no complete leading object, or it
    parses to a non-object (`[]`, `"x"`, `5`) — which is not a usable result. This is the
    single definition of what makes a sentinel "reapable"; both the harness's guarded
    sentinel writes and the reaper's reads go through it."""
    try:
        obj = json.loads(text)
    except ValueError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


def _result_is_parseable(sdir: Path) -> bool:
    """True if result.json already holds a usable JSON object — a real result the
    harness must not clobber."""
    try:
        text = (sdir / "result.json").read_text(encoding="utf-8")
    except OSError:
        return False
    return parse_leading_json(text) is not None


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


# --- session permission posture ----------------------------------------------

class BypassNotPermitted(RuntimeError):
    """Raised when a permission-bypass posture is requested but the sandbox gate
    (AUTOBUILD_SANDBOX=1) is not satisfied. str() is the operator-facing reason."""


def _note(msg: str) -> None:
    # Local stderr logger (config.py uses the same pattern) so session.py keeps no
    # upward import to loop.py, which imports this module.
    print(f"\033[1;34m[autobuild]\033[0m {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"\033[1;33m[warn]\033[0m {msg}", file=sys.stderr)


def _process_group_id(proc: Popen) -> int | None:
    """The child's process-group id. With start_new_session=True the child is its own
    group leader, so this equals proc.pid. Returns None if the child has already exited
    (ProcessLookupError/ESRCH — even an unreaped zombie raises it): there is no live
    group to record or kill."""
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None


def _allowed_tools(config: Config) -> list[str]:
    """The --allowedTools list: the configured base tools, plus Bash(git:*) for commits,
    plus one Bash(<check>:*) per checks entry so the implement phase's checks can run.
    Deduped, order preserved. This is ergonomics, NOT a security boundary (see README)."""
    tools = list(config.allowed_tools)
    tools.append("Bash(git:*)")
    for c in config.checks:
        c = c.strip()
        if c:
            tools.append(f"Bash({c}:*)")
    seen: set[str] = set()
    out: list[str] = []
    for t in tools:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _session_flags(config: Config, sdir: Path, *, sandbox: bool) -> list[str]:
    """Build the permission/tool flags appended to `claude -p`.

    A bypass posture (`dangerously_bypass_permissions`, or `permission_mode:
    bypassPermissions`) emits `--dangerously-skip-permissions`, but ONLY when the sandbox
    gate is satisfied (`require_sandbox_for_bypass` is false, or `AUTOBUILD_SANDBOX=1`);
    otherwise it raises BypassNotPermitted so the caller refuses to spawn. Non-bypass
    postures emit the permission mode, an allowlist that covers the workflow, a
    `.claude/**` write-deny, and `--strict-mcp-config`. `--add-dir` is scoped to EXACTLY
    this session's dir so a confined agent can still write its own result.json (the
    sentinel-location fix)."""
    wants_bypass = (config.dangerously_bypass_permissions
                    or config.permission_mode == "bypassPermissions")
    flags: list[str] = []
    if wants_bypass:
        if config.require_sandbox_for_bypass and not sandbox:
            raise BypassNotPermitted(
                "permission bypass requested (dangerously_bypass_permissions or "
                "permission_mode: bypassPermissions) but AUTOBUILD_SANDBOX is not set — "
                "refusing to spawn. Run only inside a disposable sandbox with "
                "AUTOBUILD_SANDBOX=1, or set require_sandbox_for_bypass: false to override "
                "(strongly discouraged)."
            )
        flags.append("--dangerously-skip-permissions")
    else:
        flags += ["--permission-mode", config.permission_mode]
        flags += ["--allowedTools", *_allowed_tools(config)]
        flags += ["--disallowedTools", "Edit(.claude/**)", "Write(.claude/**)"]
    flags += ["--add-dir", str(sdir)]
    flags.append("--strict-mcp-config")
    if config.session_max_turns:
        flags += ["--max-turns", str(config.session_max_turns)]
    return flags


# Git push/transport credentials a session never needs (the harness pushes from the
# PARENT, post-verification), stripped from the child env so a prompt-injected agent loses
# the easiest push primitive. Defense-in-depth only (NOT a general secret scrubber — the
# real boundary is the sandbox VM). The agent's commit identity rides on GIT_AUTHOR_* /
# GIT_COMMITTER_* (and GIT_CONFIG_GLOBAL/SYSTEM, file pointers) — all kept so it can still
# commit; its own auth (ANTHROPIC_*) is kept too.
_CREDENTIAL_ENV_DENYLIST = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN",
    "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND",
    # Inline git config injection — would otherwise re-enable credential.helper /
    # core.sshCommand and defeat the denials above. (KEY_*/VALUE_* matched by prefix.)
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
})
_CREDENTIAL_ENV_DENY_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _session_env() -> dict[str, str]:
    """The child's environment: the parent's, minus git push/transport credentials."""
    return {k: v for k, v in os.environ.items()
            if k not in _CREDENTIAL_ENV_DENYLIST
            and not k.startswith(_CREDENTIAL_ENV_DENY_PREFIXES)}


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

    # Resolve the permission posture first: a bypass requested without the sandbox gate
    # is refused before we build a worktree we'd only throw away.
    sandbox = os.environ.get("AUTOBUILD_SANDBOX") == "1"
    try:
        flags = _session_flags(config, sdir, sandbox=sandbox)
    except BypassNotPermitted as exc:
        set_status(task.path, "blocked")
        write_sentinel_if_absent(sdir, tid, "NEEDS_HUMAN", str(exc))
        return RunningSession(sid, tid, sdir, None, task, None)
    if "--dangerously-skip-permissions" in flags and not sandbox:
        _warn(f"session {sid} for {tid}: running with --dangerously-skip-permissions and NO "
              f"sandbox (AUTOBUILD_SANDBOX unset) — the agent inherits this machine's git "
              f"credentials and network. Only acceptable if those are disposable.")
    else:
        posture = ("bypassPermissions (AUTOBUILD_SANDBOX)"
                   if "--dangerously-skip-permissions" in flags else config.permission_mode)
        _note(f"session {sid} for {tid}: permission posture = {posture}")

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

    # The base meta.json is written (atomically) BEFORE the spawn so an in-progress
    # session always has a meta for crash recovery; pgid is added once the child exists.
    meta = {
        "session": sid,
        "task": tid,
        "task_file": str(task.path),
        "worktree": str(wt),
        "branch": branch_name(tid),
        "status": "in-progress",
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _atomic_write_json(sdir / "meta.json", meta)
    set_status(task.path, "in-progress")

    prompt = build_prompt(str(sdir), str(task.path), str(wt), tid)
    out = open(sdir / "session.out", "w")
    err = open(sdir / "session.err", "w")
    try:
        proc = Popen(
            [config.claude_cmd, "-p", prompt, "--model", config.model, *flags],
            cwd=str(wt), stdout=out, stderr=err, start_new_session=True,
            env=_session_env(),
        )
    except FileNotFoundError:
        out.close()
        err.close()
        # No Claude CLI on PATH: emit NEEDS_HUMAN so the reaper has something
        # deterministic to act on instead of hanging.
        write_sentinel_if_absent(sdir, tid, "NEEDS_HUMAN",
                                 f"claude CLI '{config.claude_cmd}' not found on PATH; cannot run session")
        return RunningSession(sid, tid, sdir, wt, task, None)

    # The child is now live: from here on, never return without its handle — a lost
    # RunningSession would orphan a running process the loop can't reap or kill.
    # deadline is computed first (it cannot fail) so the loop can always time the child
    # out; it is monotonic and in-memory only (a crashed supervisor's orphans are
    # reclaimed by reconcile's kill, not by deadline expiry, and monotonic() isn't
    # cross-process).
    deadline = time.monotonic() + config.task_timeout_seconds
    pgid: int | None = None
    try:
        pgid = _process_group_id(proc)
        meta["pgid"] = pgid
        _atomic_write_json(sdir / "meta.json", meta)
    except OSError as exc:
        _warn(f"session {sid}: could not finalize pgid/meta ({exc}); session still tracked")
    return RunningSession(sid, tid, sdir, wt, task, proc, pgid=pgid, deadline=deadline)
