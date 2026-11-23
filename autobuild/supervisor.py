"""The outer Ralph-style loop: `run` (under the run lock), the scheduling loop
(`_supervise`), session harvesting with per-session deadlines, and the deadline-bounded
waits. The loop terminates when nothing runs AND nothing is runnable, instead of spinning
to max_iterations on tasks stuck behind unsatisfiable deps."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import time

from . import interrupt
from .common import _classify_sentinel, _notify, log, ok, warn
from .config import Config
from .costs import _run_spend
from .guards import RunHalted, _assert_base_clean, _assert_not_nested, run_lock
from .paths import Paths
from .preflight import assert_run_preflight
from .procs import _kill_group, _kill_groups
from .reaper import reap_all, reap_session
from .reconcile import reconcile
from .report import _finish_run, status
from .scheduler import claim_tasks, runnable_tasks, stuck_tasks
from .session import RunningSession, spawn_session, write_sentinel_if_absent
from .tasks import index_tasks, is_terminal, iter_tasks, set_status

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
    reap_all(config, paths, should_stop=interrupt.requested)
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


def run(paths: Paths, config: Config, *, sleep_seconds: float = 2.0,
        monotonic=time.monotonic) -> None:
    """Drive the outer loop until the backlog drains. Holds the run lock for the
    whole lifetime: a second `run` is refused (RunLockHeld) and a `reap` running
    alongside this one cannot reconcile the sessions this run owns. Because we hold
    the lock, the startup reconcile is free to recover genuinely orphaned sessions
    (re-queue them as a synthetic TIMEOUT, or block them).

    `monotonic` is the wall-clock source for the optional run budget; injectable so
    tests can jump past the deadline without sleeping (mirrors `sleep_seconds`)."""
    _assert_not_nested()  # refuse a run nested inside a spawned session, before any lock
    with run_lock(paths.run_lock), interrupt.install():
        _run_locked(paths, config, sleep_seconds=sleep_seconds, monotonic=monotonic)

def _run_locked(paths: Paths, config: Config, *, sleep_seconds: float,
                monotonic=time.monotonic) -> None:
    paths.ensure_runtime_dirs()
    _assert_base_clean(paths)
    # Critical preflight (claude on PATH, git identity) BEFORE claiming or spawning, so a
    # misconfigured host aborts early with one clear message instead of N wasted sessions.
    assert_run_preflight(paths, config)
    budget = (f", run_budget={config.run_budget_seconds}s"
              if config.run_budget_seconds > 0 else "")
    if config.run_budget_usd > 0:
        budget += f", run_budget=${config.run_budget_usd:.2f}"
    log(f"starting loop (max_parallel={config.max_parallel}, "
        f"max_iterations={config.max_iterations}{budget})")
    reconcile(paths, sweep_in_progress=True)

    running: list[RunningSession] = []
    run_sids: set[str] = set()  # the sessions THIS run spawns (filled by _supervise); used
    #                             both for the cost budget and to settle their costs (#49)
    try:
        reason = _supervise(paths, config, running, run_sids,
                            sleep_seconds=sleep_seconds, monotonic=monotonic)
        if reason == "interrupted":
            # Inside the try: reaping an INTERRUPTED session runs the leak check first,
            # and a BaseBranchLeak raised there must reach the halt handler below.
            _shutdown(paths, config, running, run_sids)
    except RunHalted as exc:
        # base can't be integrated onto (a session escaped onto it, it isn't checked out,
        # or a failed merge couldn't be undone): stop now and KILL any sessions still
        # running — they'd only burn tokens on a run that can't integrate them. Mark each
        # killed session INTERRUPTED (not reaped) so the next run re-queues it without
        # spending timeout budget. Leave a summary (reason `halted`)
        # so even an aborted run is legible, notify the operator of the halt, then
        # re-raise for the CLI to report. `running` is mutated in place by _supervise,
        # so it holds the live set.
        _kill_groups(running, config.kill_grace_seconds, should_stop=interrupt.forced)
        _mark_interrupted(running, f"run halted; session killed: {exc}")
        _finish_run(paths, config, "halted", run_sids)
        _notify(config, "halt", str(exc))
        raise
    _finish_run(paths, config, reason, run_sids)
    status(paths, config)


def _mark_interrupted(running: list[RunningSession], summary: str) -> list[RunningSession]:
    """Write an INTERRUPTED sentinel for each killed session; return those written. A
    session that wrote a real result.json (e.g. during the kill grace) keeps it."""
    return [rs for rs in running
            if write_sentinel_if_absent(rs.sdir, rs.tid, "INTERRUPTED", summary)]


def _shutdown(paths: Paths, config: Config, running: list[RunningSession],
              run_sids: set[str]) -> None:
    """Graceful stop after an operator signal: kill every live session (one shared grace,
    cut short by a second signal), mark each INTERRUPTED and reap only those (fast: no
    verify, no integrate — they re-queue without spending timeout budget), write the run
    summary, then raise RunInterrupted. A real COMPLETE result stays reapable for the next
    run: the operator asked to stop, so no checks or merges start now."""
    sig = interrupt.signum() or signal.SIGINT
    name = signal.Signals(sig).name
    warn(f"{name} received: stopping {len(running)} session(s)")
    _kill_groups(running, config.kill_grace_seconds, should_stop=interrupt.forced)
    for rs in _mark_interrupted(running, f"run interrupted by {name}; session killed"):
        reap_session(rs.sdir, config, paths)
    _finish_run(paths, config, "interrupted", run_sids)
    raise interrupt.RunInterrupted(sig)

def _supervise(paths: Paths, config: Config, running: list[RunningSession],
               run_sids: set[str], *, sleep_seconds: float, monotonic=time.monotonic) -> str:
    """The scheduling loop. Appends spawned sessions to `running` and keeps it in sync
    with the live set IN PLACE (`running[:] = ...`) so a caller catching RunHalted
    can still see — and kill — what was running when the halt fired.

    Returns the terminal reason the run ended for — `drained` (backlog empty),
    `settled` (only blocked/unsatisfiable work left), or one of the budget caps with work
    left unstarted: `max_iterations`, `run_budget_seconds`, `run_budget_usd` — so the caller
    records it in the run summary instead of re-deriving it — or `interrupted` after an
    operator signal (the caller runs `_shutdown`). (The `halted` reason is supplied by the
    RunHalted handler.)"""
    iteration = 0
    # Optional whole-run WALL-CLOCK budget: a monotonic deadline computed ONCE here (not per
    # poll tick — same reasoning as max_iterations below: a long fleet of poll ticks must not
    # be what trips the cap). Past it, we fold into `over_budget` so the SAME drain-and-report
    # path handles it — stop claiming, drain in-flight, report — never forking the termination
    # logic. `monotonic` is injectable so tests jump past the deadline without sleeping. Like
    # the per-session deadlines, this clock is in-memory: a killed/resumed run does NOT resume
    # it (it restarts the budget), which is fine and consistent.
    budget_deadline = (monotonic() + config.run_budget_seconds
                       if config.run_budget_seconds > 0 else None)
    # Optional whole-run COST budget (issue #41): the financial complement to the wall-clock
    # one. Cost is only known once a session finishes (its stream-json `result` event), so it
    # can't preempt a running session mid-flight — instead, like the others, it folds into
    # `over_budget` to stop CLAIMING new work and drain. `run_sids` (caller-owned, filled
    # here as we spawn) is the set of sessions THIS run spawned — so the budget is per-run,
    # not pre-charged by persisted prior-run session dirs, AND the caller can later settle
    # their costs for the summary (#49). `cost_cache` freezes finished-session costs so each
    # pass only re-reads the in-flight ones.
    budget_usd = config.run_budget_usd if config.run_budget_usd > 0 else None
    cost_cache: dict[str, float] = {}
    while True:
        # Interrupt check points sit where the in-memory state is consistent: here, after
        # each spawn+append, and after the post-claim harvest (see interrupt.py).
        if interrupt.requested():
            return "interrupted"
        running[:] = _harvest(running, config, paths)

        free = config.max_parallel - len(running)
        # max_iterations bounds SCHEDULING ROUNDS (passes that start new work), not poll
        # ticks: a session that spans many _wait_until_next_event polls must not exhaust
        # the budget — a long session is bounded by its own task_timeout_seconds. So only a
        # pass that actually claims work counts against the cap, and once it's spent we stop
        # claiming but keep draining what's already running (never abandoning in-flight work).
        # (Counting every pass made the cap a ~max_iterations*sleep_seconds wall-clock limit
        # that tripped mid-run and stranded finished-but-unreaped work as in-progress.)
        over_iterations = iteration >= config.max_iterations
        # The wall-clock budget is, by contrast, deliberately elapsed-time based.
        over_time = budget_deadline is not None and monotonic() >= budget_deadline
        # The cost budget is real-spend based: sum this run's session costs (computed once
        # per pass and reused below, only when a budget is set so the disabled case does no
        # I/O). `>=` so the cap is a ceiling. In-flight sessions read 0 until they finish, so
        # actual spend can overshoot by up to ~max_parallel * a session's cost (soft budget,
        # same posture as run_budget_seconds).
        spent_usd = _run_spend(paths, run_sids, cost_cache) if budget_usd is not None else 0.0
        over_cost = budget_usd is not None and spent_usd >= budget_usd
        over_budget = over_iterations or over_time or over_cost
        claimed = claim_tasks(free, paths) if (free > 0 and not over_budget) else []
        if claimed:
            iteration += 1
        for i, t in enumerate(claimed):
            rs = spawn_session(t, config, paths)
            run_sids.add(rs.sid)  # count this session against the run's cost budget
            log(f"spawn {rs.sid} -> {rs.tid}")
            running.append(rs)
            if interrupt.requested():
                for unspawned in claimed[i + 1:]:  # reconcile would do it; keep status honest
                    set_status(unspawned.path, "todo")
                return "interrupted"

        # Final harvest before deciding the backlog is settled: a session that
        # finishes in this window must be reaped here, not silently stranded.
        running[:] = _harvest(running, config, paths)
        if interrupt.requested():
            return "interrupted"

        if not running:
            tasks = iter_tasks(paths.tasks_dir)
            index = index_tasks(tasks)
            # A reap in this pass can create newly-runnable work with nothing left running
            # to trigger the next claim — most notably a timeout RE-QUEUE flipping a task
            # back to `todo`. Loop back and claim it instead of settling prematurely (which
            # would strand the retry). Only while we're still allowed to start work: over
            # budget we stop claiming and report below.
            if not over_budget and runnable_tasks(tasks, index):
                continue
            pending = [t for t in tasks if not is_terminal(t.status)]
            if over_budget and pending:
                # Budget spent with work still left: report the cap accurately instead of
                # claiming a clean drain. In-flight sessions were drained above, so the only
                # thing unfinished is work we declined to start. Name WHICH cap tripped so a
                # human knows what to raise — the wall-clock budget takes precedence (the hard
                # real-time wall; raise run_budget_seconds), then the cost ceiling (raise
                # run_budget_usd), then the iteration cap — and return that reason so the run
                # summary records which cap ended the run.
                if over_time:
                    warn(f"hit run_budget_seconds ({config.run_budget_seconds}s); stopping "
                         f"with {len(pending)} unfinished task(s) — see 'autobuild status'")
                    return "run_budget_seconds"
                if over_cost:
                    warn(f"hit run_budget_usd (${config.run_budget_usd:.2f}, spent "
                         f"${spent_usd:.2f}); stopping with {len(pending)} unfinished "
                         f"task(s) — see 'autobuild status'")
                    return "run_budget_usd"
                warn(f"hit max_iterations ({config.max_iterations}); stopping with "
                     f"{len(pending)} unfinished task(s) — see 'autobuild status'")
                return "max_iterations"
            elif not pending:
                ok("backlog drained — COMPLETE")
                return "drained"
            else:
                stuck = stuck_tasks(tasks, index)
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
                return "settled"

        if not claimed:
            # No new work to start this pass (at capacity, or nothing runnable yet).
            # Block until a running session finishes rather than busy-spinning. If a
            # reap had unblocked a task, claim_tasks above would have claimed it.
            _wait_until_next_event(running, sleep_seconds)
