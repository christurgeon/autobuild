"""Per-run session cost accounting: the cost-budget spend sum and the bounded run-end
grace that lets final sessions flush their cost before the summary reads it (#49)."""

from __future__ import annotations

import time
from pathlib import Path

from .common import _read_json, warn
from .paths import Paths
from .progress import read_progress

def _run_spend(paths: Paths, sids: set[str], cache: dict[str, float]) -> float:
    """Cumulative USD spend across the sessions THIS run spawned (`sids`) — the sum of each
    session's captured cost (`total_cost_usd` from its stream-json `result` event).

    Scoped to `sids`, NOT every session dir on disk: session dirs persist after reaping
    (only `clean` removes them), so summing all of them would pre-charge a fresh `run` with
    a prior run's spend and make a budget-capped backlog un-resumable. Counting only this
    run's own sessions keeps the budget per-run (consistent with run_budget_seconds) while
    still totalling a single run's real spend, retries included.

    `cache` memoizes FINISHED sessions, whose cost can never change again, so each pass only
    re-reads this run's still-running sessions (≤ max_parallel small files). A session with
    no `result` event yet — running, killed, or crashed — contributes 0; its cost is unknown,
    never guessed."""
    total = 0.0
    for sid in sids:
        if sid in cache:
            total += cache[sid]
            continue
        prog = read_progress(paths.sessions_dir / sid / "session.out")
        cost = prog.cost_usd or 0.0
        if prog.finished:
            cache[sid] = cost  # frozen: a finished session's cost is immutable
        total += cost
    return total


# Run-end grace for a finished session's stream-json `result` event (which carries the
# cost) to flush to session.out AFTER its result.json sentinel — see #49. Bounded; the real
# flush is sub-second, so this is ample headroom, not a typical wait.
_COST_SETTLE_SECONDS = 5.0

# Reaped statuses an AGENT writes itself, having run `claude` to completion — so its
# stream-json `result` event (the cost) flushes normally and is worth waiting for. TIMEOUT
# is excluded: it is ALWAYS synthetic (the harness killed the session), so it never flushes.
# A *synthetic* BLOCKED/NEEDS_HUMAN (process died/never started) is indistinguishable from
# the agent-reported one by status alone, so it can burn the grace once — bounded, and only
# at a run-end that already failed.
_FLUSHING_STATUSES = frozenset({"COMPLETE", "BLOCKED", "NEEDS_HUMAN"})


def _cost_pending(paths: Paths, run_sids: set[str]) -> list[Path]:
    """This run's reaped sessions whose cost-bearing stream-json `result` event hasn't landed
    in session.out yet — exactly what `write_run_summary` would under-report (#49). Limited to
    `run_sids` (this run only, so a lingering unflushed dir from a prior un-`clean`ed run can't
    add the grace to every future run-end) and to agent-written terminal statuses (a killed
    TIMEOUT never flushes). An already-finished session.out is excluded."""
    pending: list[Path] = []
    for sid in run_sids:
        sdir = paths.sessions_dir / sid
        reaped = _read_json(sdir / "reaped.json")
        if not reaped or reaped.get("status") not in _FLUSHING_STATUSES:
            continue
        if not read_progress(sdir / "session.out").finished:
            pending.append(sdir)
    return pending


def _settle_session_costs(paths: Paths, run_sids: set[str],
                          grace_seconds: float = _COST_SETTLE_SECONDS) -> None:
    """Best-effort: before the run summary is read, give this run's just-finished sessions a
    brief, bounded grace to flush their cost-bearing `result` event into session.out (#49).
    Real `claude` flushes it just AFTER result.json — on which the harness reaps — so the
    final draining session(s) can otherwise be summarized cost-less. Returns the moment
    nothing is pending, or when the grace elapses; NEVER raises and never hangs (a wedged
    child must not block run end)."""
    try:
        if not _cost_pending(paths, run_sids):
            return
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if not _cost_pending(paths, run_sids):
                return
    except Exception as exc:  # noqa: BLE001 — cost settling is advisory; never break run end
        warn(f"cost-settle skipped: {exc}")
