"""The outer Ralph-style loop, the reaper, reconcile, status, and clean.
Ports loop.sh, with the audit's corruption/hang fixes:

- reaper is idempotent (a reaped.json marker; bash re-integrated every pass);
- integration runs BEFORE the task is marked done (bash set done first);
- follow-up ids are allocated under the backlog lock (bash raced on max(ls));
- a startup reconcile restores crash-safe resume without the .running PID file;
- the loop terminates when nothing runs AND nothing is runnable, instead of
  spinning to max_iterations on tasks stuck behind unsatisfiable deps.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from .config import Config
from .paths import Paths
from .scheduler import backlog_lock, claim_tasks, runnable_tasks, stuck_tasks
from .session import RunningSession, spawn_session, write_sentinel_if_absent
from .tasks import (
    create_task_file,
    is_terminal,
    iter_tasks,
    next_task_id,
    set_status,
    task_index,
)
from .worktree import branch_name, prune_worktrees, remove_worktree

# --- logging -----------------------------------------------------------------

def _c(code: str) -> str:
    return f"\033[{code}m"


def log(msg: str) -> None:
    print(f"{_c('1;34')}[autobuild]{_c('0')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('1;32')}[ ok ]{_c('0')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33')}[warn]{_c('0')} {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        # Tolerate trailing data after a valid JSON object — agents sometimes append
        # stray output (e.g. a closing tag) after the sentinel. Salvage the leading
        # object so a task that genuinely finished is not silently stranded. A truly
        # torn/partial write (no complete leading object) still returns None and is
        # retried next pass.
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            return None
    # Return only a JSON *object* (dict). A non-dict sentinel (`[]`, `"x"`, `5`) is not a
    # usable result; returning it would let callers that do `.get()` (reap_session,
    # collect_status) crash — a single poisoned file would wedge run/reap/status.
    return obj if isinstance(obj, dict) else None


def _classify_sentinel(sdir: Path) -> str:
    """Classify a session's result.json:
      'reapable' — present and parses to a usable JSON object (a dict);
      'corrupt'  — present but unparseable (leading garbage / empty) or not an object
                   (`[]`, `"x"`) — shapes raw_decode cannot salvage;
      'absent'   — no result.json yet.

    Centralizing this is what lets every gate (`_harvest`, `reconcile`) treat a
    present-but-unparseable sentinel as a finished-but-failed session and BLOCK it,
    instead of skipping it on mere file existence and stranding the task forever."""
    result_file = sdir / "result.json"
    if not result_file.exists():
        return "absent"
    return "reapable" if isinstance(_read_json(result_file), dict) else "corrupt"


# --- run lock ----------------------------------------------------------------

class RunLockHeld(RuntimeError):
    """Raised when the advisory run lock is already held — i.e. another
    `autobuild run` owns this project. str() is the lock file path."""


@contextlib.contextmanager
def run_lock(lock_file: Path):
    """Hold the project's exclusive run lock for the duration of the with-block.

    A single `run` owns the lock for its whole lifetime; this is what lets
    `reconcile()` safely BLOCK orphaned in-progress sessions (the lock proves no
    other process is driving them). Acquisition is non-blocking: if another holder
    has it, raise RunLockHeld immediately rather than waiting. The lock is an
    fcntl.flock, so the kernel releases it automatically if the holder dies — the
    exact crash semantic we want, with no stale-PID games."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise RunLockHeld(str(lock_file)) from e
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# --- integration -------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def integrate(tid: str, config: Config, paths: Paths) -> tuple[bool, str]:
    """Integrate the task's branch per config. Returns (success, detail). Only an
    auto-merge conflict counts as failure (work not landed); pr/branch always
    succeed because the branch itself is the deliverable."""
    branch = branch_name(tid)
    base = config.base_branch
    root = paths.root
    mode = config.integration

    if mode == "branch":
        return True, f"left branch {branch} for manual merge"

    if mode == "pr":
        if which("gh"):
            _git(root, "push", "-u", "origin", branch)
            r = subprocess.run(
                ["gh", "pr", "create", "--head", branch, "--base", base,
                 "--title", f"autobuild: {tid}", "--body", f"Automated by autobuild for {tid}."],
                cwd=str(root), capture_output=True, text=True,
            )
            if r.returncode == 0:
                return True, f"opened PR for {branch}"
            return True, f"PR creation failed for {branch}; branch left for manual PR"
        return True, f"gh not found; left branch {branch} for manual PR"

    if mode == "auto-merge":
        r = _git(root, "merge", "--no-ff", "-m", f"autobuild: merge {tid}", branch)
        if r.returncode == 0:
            return True, f"merged {branch} into {base}"
        _git(root, "merge", "--abort")  # don't leave the repo mid-merge
        return False, f"auto-merge conflict for {branch}"

    return False, f"unknown integration mode '{mode}'"


# --- verification gate -------------------------------------------------------

def _write_checks_log(sdir: Path, cmd: str, code: int, stdout: str, stderr: str) -> None:
    """Persist why verification failed: the command, its exit code, and the tail of
    its combined output (last 50 lines) so a human can inspect without re-running."""
    tail = "\n".join(((stdout or "") + (stderr or "")).splitlines()[-50:])
    (sdir / "checks.log").write_text(
        f"command: {cmd}\nexit: {code}\n\n--- output (tail) ---\n{tail}\n",
        encoding="utf-8",
    )


def verify_checks(config: Config, paths: Paths, sdir: Path, sid: str) -> tuple[bool, str]:
    """Re-run config.checks against the session's worktree before integrating —
    the harness's own verification of a COMPLETE, not the agent's self-report.

    Returns (passed, outcome) where outcome is one of "skipped" (verification
    disabled or no checks), "passed", or "failed: <cmd>". Stops at the first failing
    command and writes <sdir>/checks.log. A missing worktree is treated as a failure:
    we cannot verify the tree, so we refuse to trust it."""
    if not config.verify_checks or not config.checks:
        return True, "skipped"

    worktree = paths.worktrees_dir / sid
    if not worktree.is_dir():
        _write_checks_log(sdir, "(pre-flight)", -1, "",
                          f"worktree {worktree} is missing; cannot verify checks")
        return False, "failed: worktree missing"

    for cmd in config.checks:
        r = subprocess.run(cmd, shell=True, cwd=str(worktree),
                           capture_output=True, text=True)
        if r.returncode != 0:
            _write_checks_log(sdir, cmd, r.returncode, r.stdout, r.stderr)
            return False, f"failed: {cmd}"
    return True, "passed"


# --- follow-ups --------------------------------------------------------------

def file_followups(result: dict, sdir: Path, paths: Paths) -> list[str]:
    """Create tasks/ files for each followups[] entry, allocating ids under the
    backlog lock so concurrent reaps can't collide."""
    followups = result.get("followups") or []
    created: list[str] = []
    if not followups:
        return created
    with backlog_lock(paths.lock_file):
        for fu in followups:
            if not isinstance(fu, dict):
                continue
            title = str(fu.get("title", "")).strip()
            if not title:
                continue
            try:
                priority = int(fu.get("priority", 3))
            except (TypeError, ValueError):
                priority = 3
            tid = next_task_id(paths.tasks_dir)
            create_task_file(paths.tasks_dir, tid, title, priority, str(fu.get("notes", "")))
            created.append(tid)
            ok(f"filed follow-up {tid}: {title}")
    return created


# --- reaper ------------------------------------------------------------------

def reap_session(sdir: Path, config: Config, paths: Paths) -> bool:
    """Reap one finished session from its result.json sentinel. Idempotent: a
    reaped.json marker guards against double-integration / double follow-up filing.
    Returns True if it acted, False if skipped or not finished."""
    if (sdir / "reaped.json").exists():
        return False
    result = _read_json(sdir / "result.json")
    if result is None:
        return False  # not finished, or corrupt -> leave for clean

    tid = str(result.get("task", ""))
    status = str(result.get("status", ""))
    sid = sdir.name
    task = task_index(paths.tasks_dir).get(tid)
    integrated = False
    followups: list[str] = []
    checks_outcome = "n/a"

    if status == "COMPLETE":
        # Trust, but verify: re-run the checks against the committed tree ourselves
        # before integrating. A tree that fails verification is blocked, not merged.
        verified, checks_outcome = verify_checks(config, paths, sdir, sid)
        if not verified:
            if task:
                set_status(task.path, "blocked")
            warn(f"{sid}: {tid} verification {checks_outcome} — not integrated; "
                 f"branch {branch_name(tid)} kept (see {sdir / 'checks.log'})")
            # No follow-ups: a tree that fails checks did not meet the contract, so
            # its self-reported follow-ups are part of the same untrusted output.
        else:
            success, detail = integrate(tid, config, paths)
            if success:
                if task:
                    set_status(task.path, "done")
                integrated = True
                ok(f"{sid}: {tid} COMPLETE — {detail}")
            else:
                if task:
                    set_status(task.path, "blocked")
                warn(f"{sid}: {tid} integration failed — {detail}")
            followups = file_followups(result, sdir, paths)
    elif status == "BLOCKED":
        if task:
            set_status(task.path, "blocked")
        warn(f"{sid}: {tid} BLOCKED — {result.get('summary', '')}")
    elif status == "NEEDS_HUMAN":
        if task:
            set_status(task.path, "blocked")
        warn(f"{sid}: {tid} NEEDS_HUMAN — see {sdir / 'result.json'}")
    elif status == "TIMEOUT":
        # A killed-past-deadline session: a distinct, retryable status. NEVER integrated
        # or verified (its tree is incomplete), and no follow-ups filed. Retry lands in 106.
        if task:
            set_status(task.path, "timeout")
        warn(f"{sid}: {tid} TIMEOUT — killed past deadline (retry pending in a later task)")
    else:
        warn(f"{sid}: unrecognized status '{status}'")
        return False

    remove_worktree(paths, sid)
    (sdir / "reaped.json").write_text(json.dumps(
        {"reaped_at": _now(), "status": status, "integrated": integrated,
         "checks": checks_outcome, "followups": followups},
        indent=2,
    ), encoding="utf-8")
    return True


def reap_all(config: Config, paths: Paths) -> int:
    n = 0
    if not paths.sessions_dir.is_dir():
        return 0
    for sdir in sorted(paths.sessions_dir.iterdir()):
        if sdir.is_dir() and reap_session(sdir, config, paths):
            n += 1
    return n


def _signal_session(rs: RunningSession, sig: int) -> None:
    """Signal the session's whole process group (preferred) or the child directly.
    ESRCH — the group/child already exited — is suppressed so one dead session never
    aborts the harvest of the others."""
    with contextlib.suppress(ProcessLookupError):
        if rs.pgid is not None:
            os.killpg(rs.pgid, sig)
        elif rs.proc is not None:
            rs.proc.send_signal(sig)


def _kill_group(rs: RunningSession, grace: float) -> None:
    """Terminate a session and reap its child. If already exited, just reap. Otherwise
    SIGTERM -> wait(grace) -> SIGKILL -> wait(), leaving no zombie."""
    proc = rs.proc
    if proc is None:
        return
    if proc.poll() is not None:
        return  # already exited: poll() reaped it
    _signal_session(rs, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return  # exited within grace -> reaped
    except subprocess.TimeoutExpired:
        pass
    _signal_session(rs, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)  # reap the zombie


def reap_stalled(running: list[RunningSession], paths: Paths) -> None:
    """A live handle whose process exited without writing result.json gets a
    BLOCKED sentinel, so a crashed agent can't stall the loop."""
    for rs in running:
        if rs.has_result():
            continue
        if rs.proc is not None and rs.proc.poll() is not None:
            warn(f"{rs.sid} exited without a result; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "session process exited without writing result.json")


# --- reconcile (startup crash recovery) -------------------------------------

def reconcile(paths: Paths, *, sweep_in_progress: bool = False) -> None:
    """Restore resume-ability after a `run` crash. Orphaned `claimed` tasks (claim
    succeeded, spawn never finished) go back to `todo`.

    The orphaned `in-progress` -> BLOCKED sweep is gated on `sweep_in_progress`,
    which the caller sets only when it holds the run lock. Without the lock we
    cannot tell a genuinely orphaned session from one a *concurrent* live run is
    actively driving (its child process is invisible to us), so a reap that runs
    alongside a run must NOT sweep — doing so would BLOCK live sessions and remove
    their worktrees out from under the run. A fresh `run` (or a `reap` that can
    take the lock) holds it, proving no other process owns these sessions, and
    sweeps as before."""
    for t in iter_tasks(paths.tasks_dir):
        if t.status == "claimed":
            set_status(t.path, "todo")

    if sweep_in_progress:
        index = task_index(paths.tasks_dir)
        if paths.sessions_dir.is_dir():
            for sdir in sorted(paths.sessions_dir.iterdir()):
                if not sdir.is_dir():
                    continue
                if (sdir / "reaped.json").exists():
                    continue
                kind = _classify_sentinel(sdir)
                if kind == "reapable":
                    continue  # a finished session — the reaper handles it, not us
                meta = _read_json(sdir / "meta.json")
                if not meta:
                    continue
                tid = str(meta.get("task", ""))
                task = index.get(tid)
                if task and task.status == "in-progress":
                    reason = ("result.json present but could not be parsed as a JSON object"
                              if kind == "corrupt"
                              else "no result.json was written")
                    warn(f"orphaned in-progress session {sdir.name}; marking {tid} BLOCKED")
                    write_sentinel_if_absent(sdir, tid, "BLOCKED",
                                             f"orphaned: run restarted while session was in-progress; {reason}")
    prune_worktrees(paths)


def reap(paths: Paths, config: Config) -> None:
    """One-shot reap. If the run lock is free (no active run) we take it and do the
    full crash-recovery reconcile, exactly as a fresh `run` would. If a run holds
    it, we skip the in-progress sweep entirely and only collect finished sentinels
    — idempotent result.json handling that never touches the live run's sessions."""
    paths.ensure_runtime_dirs()
    try:
        with run_lock(paths.run_lock):
            reconcile(paths, sweep_in_progress=True)
            reap_all(config, paths)
    except RunLockHeld:
        warn("an 'autobuild run' is active (holds run.lock); collecting finished "
             "sessions only, leaving its in-progress sessions alone")
        reap_all(config, paths)
    status(paths, config)


# --- outer loop --------------------------------------------------------------

def _active(running: list[RunningSession]) -> list[RunningSession]:
    return [rs for rs in running if not rs.has_result() and rs.alive()]


def _harvest(running: list[RunningSession], config: Config, paths: Paths) -> list[RunningSession]:
    """Reap finished sessions, BLOCK exited-without-result ones, and return the
    still-running survivors.

    A session is never dropped from tracking without first being reaped or stalled.
    This closes the race where a session's process exits in the window between a
    bare `reap_stalled` and a liveness filter: such a session used to be silently
    dropped, leaving a valid result.json unreaped (task stuck in-progress forever).
    """
    survivors: list[RunningSession] = []
    # Snapshot `now` once: a kill can block up to `grace`, so a session that crosses its
    # deadline DURING an earlier kill is caught on the next pass — fine, deadlines (>=1800s)
    # are coarse vs grace (<=10s). Deliberate; do not recompute per session.
    now = time.monotonic()
    for rs in running:
        kind = _classify_sentinel(rs.sdir)
        if kind == "reapable":
            continue  # (1) a valid sentinel beats everything (even the deadline) — reap_all
            #             handles it (even if still alive, so a session that writes its
            #             result then lingers can't hang)
        if rs.alive():
            if rs.deadline is not None and now >= rs.deadline:
                # (3) past deadline, still running, no usable result — kill, then
                # RE-CLASSIFY: a result written during the grace window must still win.
                warn(f"{rs.sid} exceeded its deadline; killing {rs.tid}")
                _kill_group(rs, config.kill_grace_seconds)
                if _classify_sentinel(rs.sdir) == "reapable":
                    continue  # finished during grace — reap_all takes the COMPLETE
                write_sentinel_if_absent(
                    rs.sdir, rs.tid, "TIMEOUT",
                    f"session exceeded task_timeout_seconds "
                    f"({config.task_timeout_seconds}s) and was killed")
                continue
            survivors.append(rs)  # (4) within deadline, still running — let it finish
            #                       (an absent or torn/mid-write sentinel under a live
            #                       process is never BLOCKED)
            continue
        # (2) Process exited without a usable result — BLOCK it (never silently drop).
        if kind == "corrupt":
            warn(f"{rs.sid} exited with an unparseable result.json; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "result.json present but could not be parsed as a JSON object")
        else:  # absent
            warn(f"{rs.sid} exited without a result; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "session process exited without writing result.json")
    reap_all(config, paths)
    return survivors


def _next_wait(running: list[RunningSession], sleep_seconds: float, now: float) -> float:
    """How long to sleep before the next harvest: the poll interval, but never past the
    nearest session deadline (so an over-deadline session is killed promptly), and never
    negative."""
    deadlines = [rs.deadline for rs in running if rs.deadline is not None]
    if not deadlines:
        return sleep_seconds
    return max(0.0, min(sleep_seconds, min(deadlines) - now))


def _wait_until_next_event(running: list[RunningSession], sleep_seconds: float) -> None:
    """Block until the next deadline-bounded poll tick, waking early if a child exits.
    Invariant: every entry in `running` has a live `proc` (proc-less handles are reaped/
    blocked by `_harvest` before we get here), so this never tight-spins on `wait == 0`."""
    wait = _next_wait(running, sleep_seconds, time.monotonic())
    for rs in running:
        if rs.proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                rs.proc.wait(timeout=wait)
            return
    time.sleep(wait)


def run(paths: Paths, config: Config, *, sleep_seconds: float = 2.0) -> None:
    """Drive the outer loop until the backlog drains. Holds the run lock for the
    whole lifetime: a second `run` is refused (RunLockHeld) and a `reap` running
    alongside this one cannot reconcile the sessions this run owns. Because we hold
    the lock, the startup reconcile is free to BLOCK genuinely orphaned sessions."""
    with run_lock(paths.run_lock):
        _run_locked(paths, config, sleep_seconds=sleep_seconds)


def _run_locked(paths: Paths, config: Config, *, sleep_seconds: float) -> None:
    paths.ensure_runtime_dirs()
    log(f"starting loop (max_parallel={config.max_parallel}, max_iterations={config.max_iterations})")
    reconcile(paths, sweep_in_progress=True)

    running: list[RunningSession] = []
    iteration = 0
    while True:
        iteration += 1
        if iteration > config.max_iterations:
            warn(f"hit max_iterations ({config.max_iterations}); stopping")
            break

        running = _harvest(running, config, paths)

        free = config.max_parallel - len(running)
        claimed = claim_tasks(free, paths) if free > 0 else []
        for t in claimed:
            rs = spawn_session(t, config, paths)
            log(f"spawn {rs.sid} -> {rs.tid}")
            running.append(rs)

        # Final harvest before deciding the backlog is settled: a session that
        # finishes in this window must be reaped here, not silently stranded.
        running = _harvest(running, config, paths)

        if not running:
            tasks = iter_tasks(paths.tasks_dir)
            pending = [t for t in tasks if not is_terminal(t.status)]
            if not pending:
                ok("backlog drained — COMPLETE")
            else:
                stuck = stuck_tasks(tasks, {t.id: t for t in tasks})
                if stuck:
                    warn(f"backlog settled: {len(stuck)} task(s) cannot proceed:")
                    for tid in sorted(stuck):
                        warn(f"  {tid}: {stuck[tid]}")
                    other = len(pending) - len(stuck)
                    if other:
                        warn(f"  ...plus {other} other unfinished task(s) — see 'autobuild status'")
                else:
                    warn(f"backlog settled with {len(pending)} unfinished task(s) "
                         f"— see 'autobuild status'")
            break

        if not claimed:
            # No new work to start this pass (at capacity, or nothing runnable yet).
            # Block until a running session finishes rather than busy-spinning. If a
            # reap had unblocked a task, claim_tasks above would have claimed it.
            _wait_until_next_event(running, sleep_seconds)

    status(paths, config)


# --- status ------------------------------------------------------------------

def collect_status(paths: Paths) -> dict:
    tasks = iter_tasks(paths.tasks_dir)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    stuck = stuck_tasks(tasks, {t.id: t for t in tasks})
    stuck_list = [{"task": tid, "reason": stuck[tid]} for tid in sorted(stuck)]

    sessions = []
    if paths.sessions_dir.is_dir():
        for sdir in sorted(paths.sessions_dir.iterdir()):
            if not sdir.is_dir():
                continue
            result = _read_json(sdir / "result.json")
            state = result.get("status") if result else "pending"
            if (sdir / "reaped.json").exists():
                state = f"{state} (reaped)"
            sessions.append({"session": sdir.name, "state": state})

    wt = _git(paths.root, "worktree", "list")
    worktrees = [ln for ln in wt.stdout.splitlines() if "/.autobuild/worktrees/" in ln]
    br = _git(paths.root, "branch", "--list", "autobuild/*")
    branches = [ln.strip("* ").strip() for ln in br.stdout.splitlines() if ln.strip()]

    return {"counts": counts, "tasks": tasks, "sessions": sessions,
            "worktrees": worktrees, "branches": branches, "stuck": stuck_list}


def status(paths: Paths, config: Config) -> dict:
    report = collect_status(paths)
    print(f"\n{_c('1;37')}TASKS BY STATE{_c('0')}")
    if report["counts"]:
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    else:
        print("  (none)")

    print(f"\n{_c('1;37')}TASKS{_c('0')}")
    for t in report["tasks"]:
        print(f"  {t.id:<12} p{t.priority:<3} {t.status:<12} {t.title}")

    if report["stuck"]:
        print(f"\n{_c('1;31')}STUCK{_c('0')}")
        for s in report["stuck"]:
            print(f"  {s['task']:<12} {s['reason']}")

    print(f"\n{_c('1;37')}SESSIONS{_c('0')}")
    for s in report["sessions"]:
        print(f"  {s['session']:<34} {s['state']}")
    if not report["sessions"]:
        print("  (none)")

    print(f"\n{_c('1;37')}ACTIVE WORKTREES / BRANCHES{_c('0')}")
    print(f"  worktrees: {len(report['worktrees'])}   branches: {len(report['branches'])}")
    for b in report["branches"]:
        print(f"    {b}")
    print()
    return report


# --- clean -------------------------------------------------------------------

def clean(paths: Paths) -> None:
    log("cleaning finished worktrees and reaped sessions")
    prune_worktrees(paths)
    if paths.sessions_dir.is_dir():
        import shutil
        for sdir in sorted(paths.sessions_dir.iterdir()):
            if sdir.is_dir() and ((sdir / "reaped.json").exists() or (sdir / "result.json").exists()):
                shutil.rmtree(sdir, ignore_errors=True)
    ok("clean")
