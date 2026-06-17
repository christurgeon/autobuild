---
id: task-107
title: "#2 — Run budgets/timeouts, end-states, and orphan kill on reconcile"
status: todo
priority: 5
depends_on: [task-103, task-106]
---

## Goal
Bound the whole run's cost and wall time, report a precise end-state, and reclaim orphaned
`claude` process trees after a supervisor crash.

## Context
- Per-session cost is available in `usage.json` (task-103).
- `reconcile` only edits files — it cannot kill the orphaned subprocesses of a crashed `run`.
- Termination only distinguishes "drained" vs "settled with unfinished."

## Approach (resolved)
- Record per-session `cost_usd` (from `usage.json`) into the reaped/meta record. Per-task
  `cost_budget_usd` is **accounting only** (it cannot preempt a running session); use it to gate
  **retry eligibility** (don't retry a task already over budget). Document that it is not preemptive.
- `run_budget_usd` / `run_timeout_seconds` (monotonic): enforced at **claim time** — stop
  claiming new tasks once cumulative recorded cost ≥ budget or the run is past its time. On stop,
  **drain in-flight** sessions, then exit. Racy by `max_parallel`: document overshoot ≤
  `max_parallel` sessions.
- Run end-states: `DRAINED | STUCK | RUN_BUDGET_EXCEEDED | RUN_TIMEOUT`. `run()`/`status()` report
  which; reserve "COMPLETE" for `DRAINED`.
- Orphan kill: reconcile-under-lock reads the persisted `pgid` (task-104) of each orphaned
  in-progress session and `os.killpg(SIGKILL)` (suppress ESRCH) **before** writing its
  orphaned-BLOCKED sentinel.
- Config: `cost_budget_usd` / `run_budget_usd` (optional float ≥ 0 or null), `run_timeout_seconds`
  (optional int ≥ 1 or null). Add an optional-float/optional-int validator; add to `KNOWN_KEYS`.

## Acceptance criteria
- [ ] At `run_budget_usd` / `run_timeout_seconds`, the loop stops claiming, drains in-flight, and exits with the right end-state (not "settled").
- [ ] Budget overshoot is bounded by `max_parallel` sessions.
- [ ] A task over `cost_budget_usd` is not retried even if attempts remain.
- [ ] `reconcile` kills orphaned `pgid`s before BLOCKing them.
- [ ] `DRAINED` is reported as COMPLETE; `STUCK` is distinct.

## Test matrix
- [ ] `run_budget_usd` reached with sessions in flight → drain → exit `RUN_BUDGET_EXCEEDED`
- [ ] `run_timeout_seconds` reached → drain → exit `RUN_TIMEOUT`
- [ ] overshoot ≤ `max_parallel` sessions past the budget threshold before claiming stops
- [ ] task over `cost_budget_usd` → not retried despite remaining attempts
- [ ] `reconcile` `killpg`s a (mocked) orphaned child group before writing its BLOCKED sentinel
- [ ] `DRAINED` reported as COMPLETE; `STUCK` reported distinctly
- [ ] config validation for the optional float/int budget keys

## Out of scope
Acceptance verification / planner (specs #3, #4).
