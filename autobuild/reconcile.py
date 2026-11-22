"""Startup crash recovery (`reconcile`) and the lock-aware one-shot `reap` command."""

from __future__ import annotations

from .common import _classify_sentinel, _read_json, warn
from .config import Config
from .guards import RunLockHeld, run_lock
from .paths import Paths
from .procs import _kill_orphan_group
from .reaper import reap_all
from .report import status
from .session import write_sentinel_if_absent
from .tasks import iter_tasks, set_status, task_index
from .worktree import prune_worktrees

# --- reconcile (startup crash recovery) -------------------------------------

def reconcile(paths: Paths, *, sweep_in_progress: bool = False) -> None:
    """Restore resume-ability after a `run` crash. Orphaned `claimed` tasks (claim
    succeeded, spawn never finished) go back to `todo`.

    The orphaned `in-progress` recovery sweep is gated on `sweep_in_progress`,
    which the caller sets only when it holds the run lock. Without the lock we
    cannot tell a genuinely orphaned session from one a *concurrent* live run is
    actively driving (its child process is invisible to us), so a reap that runs
    alongside a run must NOT sweep — doing so would kill and re-queue live sessions
    and remove their worktrees out from under the run. A fresh `run` (or a `reap`
    that can take the lock) holds it, proving no other process owns these sessions.
    Each orphan is recovered like a deadline kill: a synthetic TIMEOUT sentinel the
    reaper re-queues (or terminal-`timeout`s once the retry budget is spent)."""
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
                    # A crash/env-kill leaves the SAME state a deadline kill does: a session
                    # killed before it could write result.json, its worktree unverified and
                    # possibly partial. Recover it exactly like a timeout — a synthetic TIMEOUT
                    # sentinel the reaper re-queues (force-deleting the partial branch so the
                    # retry re-forks from base, bounded by timeout_max_retries) or, once the
                    # budget is spent, leaves terminal `timeout`. base_leak_commits still runs
                    # first at reap for every status, so a worktree escape onto base is blocked
                    # (and, under auto-merge, halts), not silently retried. We recover BOTH the
                    # absent- and corrupt-sentinel cases: a crashed run can't tell a self-exit
                    # from an env-kill, so retryable-and-budget-bounded is the safe default
                    # (mirrors _harvest, where a killed-without-result session is TIMEOUT).
                    warn(f"orphaned in-progress session {sdir.name}; recovering {tid} as TIMEOUT")
                    write_sentinel_if_absent(sdir, tid, "TIMEOUT",
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
