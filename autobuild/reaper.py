"""The reaper: act on a finished session's result.json sentinel. Idempotent (the
reaped.json marker) and concurrency-safe (a per-session reap lock); integration runs
BEFORE the task is marked done; follow-up ids are allocated under the backlog lock."""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Callable
from pathlib import Path

from . import interrupt
from .common import _notify, _now, _read_json, ok, warn
from .config import Config
from .guards import BaseBranchLeak, base_leak_commits
from .integrate import integrate
from .paths import Paths
from .retries import clear_retries, record_timeout
from .scheduler import backlog_lock
from .session import RunningSession, _atomic_write_json, write_sentinel_if_absent
from .tasks import (
    INVALID,
    create_task_file,
    duplicate_ids,
    index_tasks,
    iter_tasks,
    load_task,
    next_task_id,
    set_status,
)
from .verify import verify_checks
from .worktree import branch_name, delete_branch, remove_worktree

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
    NOTE: the lock closes the *concurrent* double-integration. Across a crash, auto-merge
    re-reaps via <sdir>/merge.json (never merges twice); pr mode (push ok, then death before
    the marker) and follow-up filing are not idempotent across a crash."""
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


def _handle_timeout(tid: str, sid: str, task, config: Config, paths: Paths) -> bool:
    """Decide a timed-out task's fate under the backlog lock — atomic against claim_tasks
    and a concurrent reap. Returns True iff the task was re-queued for another attempt.

    Guarded on the task still being `in-progress` (re-read under the lock): if a concurrent
    run already moved it on, this reap was superseded — record nothing, change nothing.
    Otherwise record this timeout (a set of session-ids, so re-recording is idempotent) and
    branch on the distinct-attempt count:
      - within timeout_max_retries -> force-delete the partial branch (fresh base) and
        re-queue to `todo`; the same run re-claims it next pass;
      - exhausted -> leave the task terminal `timeout` and clear its ledger entry, so a
        later manual re-open starts with a fresh budget.
    A partial branch that won't delete (degenerate worktree state) makes us give up
    terminally rather than retry onto surviving partial work."""
    def give_up(reason: str) -> bool:
        set_status(task.path, "timeout")
        clear_retries(paths.retries_ledger, tid)
        warn(f"{sid}: {tid} TIMEOUT — {reason}; left terminal (timeout)")
        return False

    with backlog_lock(paths.lock_file):
        # load_task, not read_task: a file a human corrupted since the reaper's guard reads
        # as INVALID (not in-progress) and takes this superseded path instead of crashing.
        current = load_task(task.path)
        if current is None or current.status != "in-progress":
            return False  # superseded by a concurrent run — nothing to do
        count = record_timeout(paths.retries_ledger, tid, sid)  # distinct attempts so far
        if count > config.timeout_max_retries:
            return give_up(f"retries exhausted ({config.timeout_max_retries})")
        if not _discard_and_requeue(tid, sid, task, paths):
            return give_up(f"could not delete {branch_name(tid)} for a clean retry — "
                           f"delete it by hand to allow a retry")
        warn(f"{sid}: {tid} TIMEOUT — re-queued (attempt {count}/"
             f"{config.timeout_max_retries + 1}); discarded partial {branch_name(tid)}, "
             f"re-forking from base")
        return True


def _discard_and_requeue(tid: str, sid: str, task, paths: Paths) -> bool:
    """Remove the session's worktree, force-delete its partial branch (so the retry
    re-forks from base), and set the task `todo`. Caller holds the backlog lock. Returns
    False, changing no status, if the branch outlived its worktree removal (a genuine git
    failure, not a checkout pin) — never re-queue onto surviving partial work."""
    remove_worktree(paths, sid)  # free the branch from its worktree before -D
    if not delete_branch(paths, tid, force=True):
        return False
    set_status(task.path, "todo")
    return True


def _requeue_fresh(tid: str, sid: str, task, paths: Paths, *, label: str) -> bool:
    """Re-queue a killed session's task WITHOUT touching the timeout-retry ledger — for
    synthetic statuses that are not the task's fault (INTERRUPTED, RATE_LIMITED). Same posture as a
    timeout re-queue: the killed tree is incomplete and unverified, so it re-forks from
    base. Under the backlog lock, guarded on the task still being `in-progress`. Returns
    True iff re-queued."""
    with backlog_lock(paths.lock_file):
        current = load_task(task.path)  # tolerant: a corrupted file takes the superseded path
        if current is None or current.status != "in-progress":
            return False  # superseded by a concurrent run — nothing to do
        if not _discard_and_requeue(tid, sid, task, paths):
            set_status(task.path, "blocked")
            warn(f"{sid}: {tid} {label} — could not delete {branch_name(tid)} for a clean "
                 f"retry; blocked — delete the branch by hand, then reset the task to todo")
            return False
        warn(f"{sid}: {tid} {label} — re-queued (no retry budget used); discarded partial "
             f"{branch_name(tid)}")
        return True


def _reap_session_locked(sdir: Path, result: dict, config: Config, paths: Paths) -> bool:
    """The reap body, run while holding the session's reap lock and after confirming
    reaped.json is absent. `result` is the already-parsed result.json."""
    tid = str(result.get("task", ""))
    status = str(result.get("status", ""))
    sid = sdir.name
    tasks = iter_tasks(paths.tasks_dir)
    task = index_tasks(tasks).get(tid)
    dup_paths = duplicate_ids(tasks).get(tid)
    # A duplicated id or an INVALID file can't be acted on safely (which file? no status
    # line to write?). Defer: the next reap pass after the human fixes the file finishes it.
    unresolvable = bool(dup_paths) or (task is not None and task.status == INVALID)
    integrated = False
    requeued = False
    followups: list[str] = []
    checks_outcome = "n/a"

    # Worktree-escape check FIRST, before any integration: a NON-MERGE commit on
    # base_branch since spawn means the session (or something outside autobuild) committed
    # straight onto base instead of its own branch. Block the task and record forensics
    # either way; the *response* is scoped to the integration mode, because that is the
    # only mode where base movement actually corrupts autobuild's model:
    #   - auto-merge: dependents fork from base and deliverables MERGE onto it, so an
    #     unexpected base commit poisons every later integration. HALT the whole run
    #     (raise) and write NO reaped.json, so re-runs keep flagging until base is cleaned.
    #   - pr/branch: autobuild never advances base — each deliverable is its own branch —
    #     so one session's base movement can't corrupt the others. Block just this task
    #     (its work may have escaped) and keep going; do NOT halt unrelated in-flight work
    #     on what may simply be a concurrent commit to base.
    # Runs for every status (an agent can leak then report BLOCKED/TIMEOUT, not just COMPLETE).
    meta = _read_json(sdir / "meta.json") or {}
    base_branch = str(meta.get("base_branch") or config.base_branch)
    leaks = base_leak_commits(paths, base_branch, str(meta.get("base_sha", "")))
    if leaks:
        if task and not unresolvable:
            set_status(task.path, "blocked")
        _atomic_write_json(sdir / "leak.json",
                           {"detected_at": _now(), "task": tid, "base_branch": base_branch,
                            "base_sha": meta.get("base_sha", ""), "commits": leaks,
                            "integration": config.integration})
        warn(f"!! BASE LEAK !! {sid}: {tid} — {base_branch} advanced with "
             f"{len(leaks)} commit(s) autobuild did not create: {', '.join(leaks)}")
        if config.integration == "auto-merge":
            warn(f"   auto-merge integrates onto {base_branch}, so this corrupts every "
                 f"later merge — HALTING. {tid} blocked. Inspect {sdir / 'leak.json'}, move "
                 f"the commit(s) onto a branch and reset {base_branch}, then re-run.")
            # Raise BEFORE the remove_worktree/delete_branch cleanup below: the worktree
            # and the autobuild/<tid> branch must survive so an operator can recover.
            raise BaseBranchLeak(
                f"{base_branch} carries {len(leaks)} commit(s) autobuild did not create "
                f"(session {sid}, task {tid}); refusing to integrate onto a corrupted base")
        # pr/branch: localized — block this task, keep its branch for recovery, and let the
        # rest of the run proceed. Mark reaped so we don't re-flag it every pass.
        warn(f"   {config.integration} mode never integrates onto {base_branch}, so other "
             f"work is unaffected; {tid} blocked (its deliverable may have escaped). "
             f"Inspect {sdir / 'leak.json'}; the run continues.")
        remove_worktree(paths, sid)
        _atomic_write_json(sdir / "reaped.json",
                           {"reaped_at": _now(), "status": status, "integrated": False,
                            "requeued": False, "checks": "n/a", "followups": [],
                            "leak": leaks})
        return True

    if unresolvable:
        why = (f"id declared by {', '.join(p.name for p in dup_paths)}" if dup_paths
               else f"{task.path.name} is invalid ({task.error})")
        warn(f"{sid}: {tid} {status} — reap deferred: {why}; fix the task file and the "
             f"next pass (or `autobuild reap`) finishes it")
        return False

    if status == "COMPLETE":
        # Trust, but verify: re-run the checks against the committed tree ourselves
        # before integrating. A tree that fails verification is blocked, not merged.
        # A recorded merge (merge.json: an earlier reap merged, then died) already landed:
        # integrate's post-merge re-check is the gate, even if the worktree is gone.
        if (sdir / "merge.json").exists():
            verified, checks_outcome = True, "skipped (merge recorded)"
        else:
            verified, checks_outcome = verify_checks(config, paths, sdir, sid)
        if not verified and interrupt.requested():
            return _abandon_for_shutdown(sid, tid, f"verification {checks_outcome}")
        if not verified:
            if task:
                set_status(task.path, "blocked")
            warn(f"{sid}: {tid} verification {checks_outcome} — not integrated; "
                 f"branch {branch_name(tid)} kept (see {sdir / 'checks.log'})")
            # No follow-ups: a tree that fails checks did not meet the contract, so
            # its self-reported follow-ups are part of the same untrusted output.
        else:
            success, detail = integrate(tid, config, paths, sdir)
            if not success and interrupt.requested():
                return _abandon_for_shutdown(sid, tid, f"integration failed ({detail})")
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
        _notify(config, "needs_human",
                f"{tid}: {result.get('summary') or 'needs human attention'} (session {sid})")
    elif status == "TIMEOUT":
        # A killed-past-deadline session: NEVER integrated or verified (its tree is
        # incomplete), and no follow-ups filed. Re-queue for another attempt or, once the
        # budget is spent, leave the task terminal `timeout` (decided under the backlog lock).
        if task:
            requeued = _handle_timeout(tid, sid, task, config, paths)
        else:
            # No task file (deleted/renamed mid-flight) — still reaped by the shared tail,
            # but log it so a timed-out session never vanishes without a diagnostic.
            warn(f"{sid}: {tid} TIMEOUT — task file not found; reaped without retry")
    elif status in ("INTERRUPTED", "RATE_LIMITED"):
        # Killed by an operator signal / a halt, or stopped by a usage/rate limit: neither is
        # the task's fault. Like TIMEOUT never integrated, verified, or given follow-ups —
        # but re-queued WITHOUT a retries.json entry, so it never spends timeout budget.
        # (RATE_LIMITED needs no budget of its own: the run stops claiming on the first one.)
        if task:
            requeued = _requeue_fresh(tid, sid, task, paths, label=status)
        else:
            warn(f"{sid}: {tid} {status} — task file not found; reaped without retry")
    else:
        warn(f"{sid}: unrecognized status '{status}'")
        return False

    remove_worktree(paths, sid)
    # Under auto-merge the branch's commits now live on base_branch via the merge commit,
    # so the autobuild/<tid> branch is redundant — delete it (safely) instead of letting it
    # accumulate. Other modes keep the branch: it is the deliverable (pr/branch) and a
    # dependent may still need it to layer (the commits live nowhere else). A re-queued
    # timeout already force-deleted its branch in _handle_timeout (fresh base).
    if integrated and config.integration == "auto-merge":
        delete_branch(paths, tid)
    _atomic_write_json(sdir / "reaped.json",
                       {"reaped_at": _now(), "status": status, "integrated": integrated,
                        "requeued": requeued, "checks": checks_outcome, "followups": followups})
    return True


def _abandon_for_shutdown(sid: str, tid: str, why: str) -> bool:
    """A COMPLETE reap that failed while a shutdown is in progress: the failure is likely
    the terminal SIGINT hitting the check or git child, not the work. Change nothing and
    write no reaped.json, so the next run re-verifies and re-integrates from the intact
    result.json. integrate's failure paths have restored base, except a re-reap whose merge
    cannot be safely undone: that red merge stays on base until the next run re-checks it."""
    warn(f"{sid}: {tid} {why} during shutdown — left for the next run")
    return False


def reap_all(config: Config, paths: Paths,
             should_stop: Callable[[], bool] = lambda: False,
             skip: frozenset[str] = frozenset()) -> int:
    """Reap every finished session except the ids in `skip` (sessions whose process is
    still running). `should_stop()` is checked before each one; True stops early and
    leaves the rest reapable (a shutdown is in progress)."""
    n = 0
    if not paths.sessions_dir.is_dir():
        return 0
    for sdir in sorted(paths.sessions_dir.iterdir()):
        if should_stop():
            break
        if sdir.name in skip:
            continue
        if sdir.is_dir() and reap_session(sdir, config, paths):
            n += 1
    return n

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
