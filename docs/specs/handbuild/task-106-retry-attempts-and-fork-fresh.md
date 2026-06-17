---
id: task-106
title: "#2 — Retry: durable attempts, fork-fresh, backoff, termination"
status: todo
priority: 4
depends_on: [task-105]
---

## Goal
Allow bounded retry of timed-out / transient failures without inheriting a poisoned branch
or looping unboundedly — and persist the attempt count across crashes.

## Context
- `make_worktree` **reuses** an existing `autobuild/<tid>` branch (`worktree.py`) — fatal for
  a retry, which would inherit the previous attempt's half-done (possibly timeout-causing) commits.
- Attempt count has no durable store; per-session `meta.json` resets per new session id.
- `reconcile` flips `claimed → todo`; termination = "not running and not pending".

## Approach (resolved)
- Persist `attempts:` in the **task frontmatter** via `bump_attempt` (surgical + atomic, like
  `set_status`); `read_task` parses it (default 0).
- Config: `max_retries` (default 0), `retry_backoff_seconds` (default 30). Validate.
- Eligibility: a `timeout` task with `attempts < max_retries` is re-queued to `todo` after
  `retry_backoff_seconds`, `attempts += 1`. Once `attempts >= max_retries` → terminal `timeout`.
- Fork fresh: on a retry, delete the poisoned branch (`git branch -D autobuild/<tid>`) so
  `make_worktree` forks from `base_branch` (or make `make_worktree` fork-fresh when `attempts > 0`).
- Termination: a task within its retry-backoff window counts as **not settled** — the loop keeps
  looping with a short wait and must not declare COMPLETE while a retry is pending.

## Acceptance criteria
- [ ] `attempts` persists in frontmatter and survives a crash + `reconcile`.
- [ ] A deterministically-timing-out task with `max_retries = N` runs exactly `N+1` attempts, then terminal `timeout`.
- [ ] A retry forks fresh from `base_branch` (the poisoned branch is not reused).
- [ ] `retry_backoff_seconds` is honored before re-spawn.
- [ ] The loop does not declare COMPLETE while a retry is pending within backoff.

## Test matrix
- [ ] `bump_attempt` persists in frontmatter; survives `reconcile`/crash
- [ ] `max_retries = 2` ⇒ exactly 3 attempts ⇒ terminal `timeout`
- [ ] retry attempt-2 worktree does NOT contain attempt-1's partial commit (fork-fresh)
- [ ] task not re-spawned before `retry_backoff_seconds`
- [ ] retry-pending-within-backoff ⇒ loop not declared settled/COMPLETE
- [ ] `max_retries = 0` ⇒ no retry (today's behavior)

## Out of scope
Run-level budgets/timeouts, orphan kill (task-107).
