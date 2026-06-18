"""The outer Ralph-style loop, the reaper, reconcile, status, and clean.

Key invariants this module upholds:
- the reaper is idempotent (a reaped.json marker guards re-integration);
- integration runs BEFORE the task is marked done, so a failed merge never leaves
  a falsely-'done' task;
- follow-up ids are allocated under the backlog lock, so concurrent reaps can't collide;
- a startup reconcile restores crash-safe resume from files + git alone (no PID file);
- the loop terminates when nothing runs AND nothing is runnable, instead of
  spinning to max_iterations on tasks stuck behind unsatisfiable deps.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from .config import Config
from .paths import Paths
from .scheduler import backlog_lock, claim_tasks, stuck_tasks
from .session import (
    RunningSession,
    _atomic_write_json,
    parse_leading_json,
    spawn_session,
    write_sentinel_if_absent,
)
from .tasks import (
    create_task_file,
    is_terminal,
    iter_tasks,
    next_task_id,
    set_status,
    task_index,
)
from .worktree import branch_name, delete_branch, prune_worktrees, remove_worktree

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
    """Read `path` and parse it as a JSON object via parse_leading_json (tolerating
    stray trailing data after a valid leading object). Returns None if the file is
    unreadable, torn/partial, or parses to a non-object — returning a non-dict would
    crash callers that do `.get()` (reap_session, collect_status), wedging
    run/reap/status on a single poisoned file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_leading_json(text)


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
        if not which("gh"):
            return True, f"gh not found; left branch {branch} for manual PR"
        push = _git(root, "push", "-u", "origin", branch)
        if push.returncode != 0:
            # Push failed (no remote / auth): nothing reached the remote, so there is no PR.
            # The local branch is still the deliverable downstream tasks merge, so the task
            # stays done — but say so accurately instead of the misleading "PR creation
            # failed" (which implies the branch was pushed). Check remote/auth to get a PR.
            return True, f"push failed for {branch}; kept locally, no PR opened (check remote/auth)"
        r = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--base", base,
             "--title", f"autobuild: {tid}", "--body", f"Automated by autobuild for {tid}."],
            cwd=str(root), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True, f"opened PR for {branch}"
        # Branch IS pushed (visible on the remote); only the PR-open step failed -> a human
        # can open it from the pushed branch, so the work landed: done.
        return True, f"PR creation failed for {branch}; pushed branch left for manual PR"

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

class _ReapLockBusy(RuntimeError):
    """Another process holds this session's reap lock (it's reaping it now)."""


@contextlib.contextmanager
def _session_reap_lock(sdir: Path):
    """Exclusive, NON-BLOCKING per-session lock around a reap. Raises _ReapLockBusy if
    another process is already reaping this session (so we skip it — it's being handled).
    fcntl.flock auto-releases on close and on holder death, so a crashed reaper strands
    nothing (the next pass reaps) — unlike an O_EXCL marker, which would lose the work."""
    f = open(sdir / ".reap.lock", "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise _ReapLockBusy(str(sdir)) from e
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def reap_session(sdir: Path, config: Config, paths: Paths) -> bool:
    """Reap one finished session from its result.json sentinel. Idempotent AND
    concurrency-safe: the reaped.json marker guards re-runs, and a per-session
    NON-BLOCKING flock serializes a `run` and a concurrent `reap` so only one integrates
    (no duplicate PRs / follow-ups). Returns True if it acted, False if skipped/not finished.
    NOTE: the lock closes the *concurrent* double-integration; a crash *mid-integrate*
    (push ok, then death before the marker) is not covered — `integrate` isn't idempotent
    across a crash. That's a separate concern (out of scope here)."""
    if (sdir / "reaped.json").exists():
        return False  # fast path (best-effort): already reaped
    result = _read_json(sdir / "result.json")
    if result is None:
        return False  # not finished, or corrupt -> leave for clean (no lock needed)
    try:
        with _session_reap_lock(sdir):
            # Re-check UNDER the lock: a concurrent reaper may have finished between the
            # fast-path check above and our acquiring the lock. THIS is the real
            # idempotency guard — keep it here in reap_session, not in _session_reap_lock.
            if (sdir / "reaped.json").exists():
                return False
            return _reap_session_locked(sdir, result, config, paths)
    except _ReapLockBusy:
        return False  # another process is reaping this session right now — it's handled


def _reap_session_locked(sdir: Path, result: dict, config: Config, paths: Paths) -> bool:
    """The reap body, run while holding the session's reap lock and after confirming
    reaped.json is absent. `result` is the already-parsed result.json."""
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
        # or verified (its tree is incomplete), and no follow-ups filed.
        if task:
            set_status(task.path, "timeout")
        warn(f"{sid}: {tid} TIMEOUT — killed past deadline (awaiting retry)")
    else:
        warn(f"{sid}: unrecognized status '{status}'")
        return False

    remove_worktree(paths, sid)
    # Under auto-merge the branch's commits now live on base_branch via the merge commit,
    # so the autobuild/<tid> branch is redundant — delete it (safely) instead of letting it
    # accumulate. Other modes keep the branch: it is the deliverable (pr/branch) and a
    # dependent may still need it to layer (the commits live nowhere else).
    if integrated and config.integration == "auto-merge":
        delete_branch(paths, tid)
    _atomic_write_json(sdir / "reaped.json",
                       {"reaped_at": _now(), "status": status, "integrated": integrated,
                        "checks": checks_outcome, "followups": followups})
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
    proc.wait()  # SIGKILL is uncatchable; wait unbounded so the child is always reaped
    #              (a bounded wait could time out and leak a zombie the loop won't reclaim)


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


def _kill_orphan_group(pgid: object) -> bool:
    """SIGKILL an orphaned session's process group — a crashed run's detached `claude`
    subtree (reparented to init). Returns True if we signalled it.

    Heavily guarded, because os.killpg(pgid) maps to kill(-pgid): pgid 0 == OUR OWN group
    (would SIGKILL the running harness), pgid 1 == kill(-1) == a BROADCAST SIGKILL to every
    process we may signal, and the harness's own group must never be hit by a corrupted
    meta. We also suppress ESRCH (group already gone) AND EPERM (a recycled pgid we may not
    signal) so one un-killable orphan never aborts the whole reconcile sweep.

    CAVEAT — pgid reuse: a recorded pgid shares the pid number space, so after the original
    leader exited and was reaped the OS can recycle the number into an unrelated group (>1);
    worst case this signals that group. The window is bounded — reconcile runs only under
    the run lock, at startup shortly after a crash, on a host where autobuild is the only
    thing spawning detached groups, so a collision needs that exact number recycled since
    the crash. A portable leader-identity check isn't feasible without a new dependency (no
    /proc on macOS); a stronger guard is deferred as future hardening."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    if pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False  # group already gone — nothing to reclaim
    except PermissionError:
        warn(f"cannot signal orphaned process group {pgid} (permission denied); skipping")
        return False


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
                    # Reclaim the crashed run's detached process tree BEFORE blocking — so the
                    # orphan is dead before any later reap touches its worktree.
                    pgid = meta.get("pgid")
                    if _kill_orphan_group(pgid):
                        warn(f"killed orphaned process group {pgid} for {tid}")
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
                # Note the deliberate asymmetry: an absent/corrupt result under a *killed*
                # session is TIMEOUT (retryable), whereas the same shape from a process
                # that exited ON ITS OWN (the branch below) is terminal BLOCKED — a killed
                # agent's torn/missing write shouldn't be held against it as a hard failure.
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
        running = _harvest(running, config, paths)

        free = config.max_parallel - len(running)
        # max_iterations bounds SCHEDULING ROUNDS (passes that start new work), not poll
        # ticks: a session that spans many _wait_until_next_event polls must not exhaust
        # the budget — a long session is bounded by its own task_timeout_seconds. So only a
        # pass that actually claims work counts against the cap, and once it's spent we stop
        # claiming but keep draining what's already running (never abandoning in-flight work).
        # (Counting every pass made the cap a ~max_iterations*sleep_seconds wall-clock limit
        # that tripped mid-run and stranded finished-but-unreaped work as in-progress.)
        over_budget = iteration >= config.max_iterations
        claimed = claim_tasks(free, paths) if (free > 0 and not over_budget) else []
        if claimed:
            iteration += 1
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
            if over_budget and pending:
                # Budget spent with work still left: report the cap accurately instead of
                # claiming a clean drain. In-flight sessions were drained above, so the only
                # thing unfinished is work we declined to start — raise max_iterations to do more.
                warn(f"hit max_iterations ({config.max_iterations}); stopping with "
                     f"{len(pending)} unfinished task(s) — see 'autobuild status'")
            elif not pending:
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
    """Remove reaped sessions and prune detached worktrees — but only when no run is
    active. Taking the run lock is what makes deletion safe: without it, clean could
    rmtree a live session's worktree or a COMPLETE-but-unreaped session (losing the
    result). Refuses (and warns) rather than racing a live run."""
    paths.ensure_runtime_dirs()
    try:
        with run_lock(paths.run_lock):
            _clean_locked(paths)
    except RunLockHeld:
        warn("an 'autobuild run' is active (holds run.lock); skipping clean so it can't "
             "delete live sessions or prune their worktrees")


def _clean_locked(paths: Paths) -> None:
    log("cleaning reaped sessions and pruning detached worktrees")
    prune_worktrees(paths)
    if paths.sessions_dir.is_dir():
        import shutil
        for sdir in sorted(paths.sessions_dir.iterdir()):
            # Only `reaped.json` means truly finished. A bare `result.json` can be a
            # COMPLETE session the reaper hasn't integrated yet — deleting it loses work.
            if sdir.is_dir() and (sdir / "reaped.json").exists():
                shutil.rmtree(sdir, ignore_errors=True)
    ok("clean")
