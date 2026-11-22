"""Reporting: `status`, the end-of-run summary + digest + `done` notification
(`_finish_run`), and `clean`."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .common import _c, _git, _notify, _now, _read_json, log, ok, warn
from .config import Config
from .costs import _settle_session_costs
from .guards import RunLockHeld, run_lock
from .paths import Paths
from .progress import read_progress
from .retries import retry_count
from .scheduler import stuck_tasks
from .session import _atomic_write_json
from .tasks import index_tasks, iter_tasks
from .worktree import prune_worktrees

# --- status ------------------------------------------------------------------

def collect_status(paths: Paths) -> dict:
    tasks = iter_tasks(paths.tasks_dir)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    stuck = stuck_tasks(tasks, index_tasks(tasks))
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
            # Live per-session progress (issue #40), parsed from the stream-json session.out:
            # the assistant-message count and the final result event's cost. Idle time comes
            # free from the file's mtime (its last write = the agent's last activity); None
            # when the session hasn't emitted anything yet.
            out_file = sdir / "session.out"
            prog = read_progress(out_file)
            try:
                idle = max(0.0, time.time() - out_file.stat().st_mtime)
            except OSError:
                idle = None
            sessions.append({"session": sdir.name, "state": state,
                             "messages": prog.messages, "cost_usd": prog.cost_usd,
                             "idle_seconds": idle})

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
        bits = f"msgs={s['messages']}"
        if s.get("cost_usd") is not None:
            bits += f"  cost=${s['cost_usd']:.4f}"
        # idle is only meaningful for a still-live session; a reaped one isn't "idle".
        if s.get("idle_seconds") is not None and "(reaped)" not in s["state"]:
            bits += f"  idle={s['idle_seconds']:.0f}s"
        print(f"  {s['session']:<34} {s['state']:<18} {bits}")
    if not report["sessions"]:
        print("  (none)")

    print(f"\n{_c('1;37')}ACTIVE WORKTREES / BRANCHES{_c('0')}")
    print(f"  worktrees: {len(report['worktrees'])}   branches: {len(report['branches'])}")
    for b in report["branches"]:
        print(f"    {b}")
    print()
    return report


# --- run summary -------------------------------------------------------------

def _parse_iso(ts: object) -> datetime | None:
    """Parse an autobuild timestamp (`_now()`'s `%Y-%m-%dT%H:%M:%SZ`, UTC) back to an
    aware datetime, or None for anything unparseable — so a torn/missing timestamp
    just drops the duration rather than crashing the summary."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _duration_seconds(started: object, reaped_at: object) -> float | None:
    """Best-effort wall-time between a session's `started` (meta.json) and `reaped_at`
    (reaped.json). None when either timestamp is missing/unparseable or the clock ran
    backwards — we omit a duration we can't trust rather than guess one."""
    a, b = _parse_iso(started), _parse_iso(reaped_at)
    if a is None or b is None:
        return None
    secs = (b - a).total_seconds()
    return secs if secs >= 0 else None


def _session_outcomes(paths: Paths) -> dict[str, dict]:
    """Map task-id -> its latest session's integration outcome, harvested from the
    per-session forensics (`meta.json` + `reaped.json`, or `leak.json` when the session
    halted the run before it could be reaped). A task with several sessions (timeout
    retries / re-runs) reports its most recent one; sessions sort chronologically by id,
    so a later entry overwrites an earlier. Sessions with neither a reaped.json nor a
    leak.json contribute nothing — they have no terminal outcome yet."""
    outcomes: dict[str, dict] = {}
    if not paths.sessions_dir.is_dir():
        return outcomes
    for sdir in sorted(paths.sessions_dir.iterdir()):
        if not sdir.is_dir():
            continue
        meta = _read_json(sdir / "meta.json") or {}
        reaped = _read_json(sdir / "reaped.json")
        if reaped is not None:
            tid = str(meta.get("task", "")) or str(reaped.get("task", ""))
            outcome = {
                "session": sdir.name,
                "result": reaped.get("status"),
                "integrated": bool(reaped.get("integrated", False)),
                "checks": reaped.get("checks", "n/a"),
                "requeued": bool(reaped.get("requeued", False)),
                "followups": list(reaped.get("followups") or []),
            }
            leak = reaped.get("leak")
            if leak:
                outcome["leak"] = list(leak)
            duration = _duration_seconds(meta.get("started"), reaped.get("reaped_at"))
        else:
            # The auto-merge base-leak halt blocks the task and writes leak.json but —
            # deliberately — NO reaped.json (so a re-run keeps flagging the corrupt base).
            # Surface that session's outcome from leak.json so a halted run is still legible.
            leak = _read_json(sdir / "leak.json")
            if leak is None:
                continue  # no terminal outcome yet — skip
            tid = str(meta.get("task", "")) or str(leak.get("task", ""))
            outcome = {
                "session": sdir.name,
                "result": None,
                "integrated": False,
                "checks": "n/a",
                "requeued": False,
                "followups": [],
                "leak": list(leak.get("commits") or []),
            }
            duration = _duration_seconds(meta.get("started"), leak.get("detected_at"))
        if not tid:
            continue
        if duration is not None:
            outcome["duration_seconds"] = duration
        # The session's cost (issue #41): the total_cost_usd from its stream-json result
        # event, or None if it never emitted one (killed/crashed before finishing).
        outcome["cost_usd"] = read_progress(sdir / "session.out").cost_usd
        outcomes[tid] = outcome
    return outcomes


def write_run_summary(paths: Paths, config: Config, reason: str) -> dict:
    """Build and atomically write `.autobuild/run-summary.json` — the end-of-run digest of
    what the run did. Reuses `collect_status` for counts/tasks/stuck and layers each task's
    per-session integration outcome (from reaped.json), follow-ups filed, timeout-retry
    attempts (from the retries ledger), and best-effort wall-time on top. Returns the plain
    dict it wrote (also the message body the notifications hook formats)."""
    report = collect_status(paths)
    outcomes = _session_outcomes(paths)
    rows = []
    for t in report["tasks"]:
        out = outcomes.get(t.id)
        row = {
            "id": t.id,
            "status": t.status,
            "attempts": retry_count(paths.retries_ledger, t.id),
            "integration": out,
            "followups": list(out["followups"]) if out else [],
            # this task's latest session cost (issue #41); None if unknown/not finished
            "cost_usd": out.get("cost_usd") if out else None,
        }
        rows.append(row)
    # Project total = sum of the per-task row costs, so total == sum(rows) by construction
    # (no disagreement between the headline figure and the rows that compose it).
    total_cost = sum(r["cost_usd"] for r in rows if r["cost_usd"])
    summary = {
        "generated_at": _now(),
        "reason": reason,
        "counts": report["counts"],
        "total_cost_usd": total_cost,
        "tasks": rows,
        "stuck": report["stuck"],
    }
    _atomic_write_json(paths.run_summary, summary)
    return summary


def _print_run_digest(summary: dict) -> None:
    """Print the short at-a-glance digest (the JSON is the complete record). One line for
    the reason, one for the counts, and one per blocked/timed-out task with its reason."""
    log(f"run ended: {summary['reason']}")
    counts = summary["counts"]
    if counts:
        log("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    stuck = {s["task"]: s["reason"] for s in summary["stuck"]}
    for row in summary["tasks"]:
        if row["status"] not in ("blocked", "timeout"):
            continue
        warn(f"  {row['id']} {row['status']}: {_task_digest_reason(row, stuck)}")


def _task_digest_reason(row: dict, stuck: dict[str, str]) -> str:
    """A one-line 'why' for a blocked/timed-out task in the digest: prefer the
    integration forensics (leak / failed checks / reported result), then fall back to
    the stuck-dependency reason, then the bare status."""
    bits: list[str] = []
    integ = row.get("integration")
    if integ:
        if integ.get("leak"):
            bits.append("base-leak")
        elif str(integ.get("checks", "")).startswith("failed"):
            bits.append(str(integ["checks"]))
        elif integ.get("result"):
            bits.append(str(integ["result"]))
    if row["id"] in stuck:
        bits.append(stuck[row["id"]])
    return "; ".join(bits) or row["status"]


def _run_end_message(summary: dict) -> str:
    """The `done`-event notification body, built from the run summary: the terminal reason
    plus the per-status counts, so the operator learns what merged / blocked / timed out
    without opening run-summary.json."""
    counts = summary.get("counts") or {}
    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no tasks"
    return f"run ended ({summary.get('reason', '?')}): {counts_str}"


def _finish_run(paths: Paths, config: Config, reason: str, run_sids: set[str]) -> None:
    """Write the run summary, print its digest, and notify the operator of the run end —
    all best-effort. A failure here must never mask the run's real outcome (or a re-raised
    BaseBranchLeak halt), so any error is warned and swallowed rather than propagated.

    The `done` notification fires for every non-halted end (drained / settled /
    max_iterations); the `halt` end gets its own `halt` event from the BaseBranchLeak
    handler, so we suppress the `done` event here to avoid a double notification."""
    # Give this run's just-finished sessions a brief, bounded grace to flush their cost into
    # session.out before the summary reads it (#49). On the halt path the survivors were
    # killed (not agent-reaped) so they aren't candidates — it's a near-noop there.
    _settle_session_costs(paths, run_sids)
    summary = None
    try:
        summary = write_run_summary(paths, config, reason)
        _print_run_digest(summary)
    except Exception as exc:  # noqa: BLE001 — summary is advisory; never let it break a run
        warn(f"could not write run summary: {exc}")
    if reason != "halted" and summary is not None:
        _notify(config, "done", _run_end_message(summary))


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
