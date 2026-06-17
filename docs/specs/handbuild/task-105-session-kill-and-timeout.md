---
id: task-105
title: "#2 — Session kill + timeout harvest + worktree cleanup"
status: todo
priority: 3
depends_on: [task-104]
---

## Goal
Kill a hung/over-deadline session safely — **without ever destroying a valid result** — and
leave the repo clean afterward.

## Context
- `_harvest` encodes a load-bearing invariant: **a valid `result.json` beats liveness**
  (reapable → `continue` even if alive). Timeout logic must preserve it.
- `_wait_any` blocks on the *first* child for the full poll interval.
- `remove_worktree` can fail on a locked/dirty tree left by a SIGKILL'd agent.
- `TERMINAL = {done, blocked}`; there is no timeout status.

## Approach (resolved)
- `_kill_group(rs, grace)`: if the proc already exited → `wait(1)` to reap; else
  `killpg(SIGTERM)` → `proc.wait(grace)` → `killpg(SIGKILL)` → `proc.wait()` (reap the zombie).
  Wrap **every** `killpg` in `suppress(ProcessLookupError)` (ESRCH on an already-exited group).
- `_harvest` ordering (load-bearing): (1) reapable → reap; (2) exited-without-result → BLOCK
  via `write_sentinel_if_absent`; (3) alive AND `now >= deadline` → `_kill_group`, then
  **re-classify** — only if still absent/corrupt write a TIMEOUT sentinel (via
  `write_sentinel_if_absent`); (4) alive AND within deadline → survive.
- Replace `_wait_any` with `_wait_until_next_event`:
  `sleep(max(0, min(sleep_seconds, nearest_deadline - now)))`.
- New distinct status **`timeout`** (retryable; NEVER enters the integrate/`verify_checks`
  path). Not yet terminal — retry handled in task-106.
- Killed-worktree cleanup before/within `remove_worktree`: `git merge --abort` /
  `rebase --abort`, remove stale `.git/worktrees/<sid>/locked` + `index.lock`, then
  `git worktree prune` on the rmtree fallback.

## Acceptance criteria
- [ ] A result written ~at the deadline is reaped COMPLETE and never overwritten by a TIMEOUT.
- [ ] ESRCH on an already-exited group is swallowed; other sessions still harvested.
- [ ] No defunct child remains after a kill.
- [ ] A killed dirty/locked worktree is fully removable with no leaked admin dir.
- [ ] `_wait_until_next_event` wakes on the nearest deadline, not the first child.
- [ ] `timeout` never enters integrate/`verify_checks`.

## Test matrix
- [ ] result written ~at deadline → reaped COMPLETE, no TIMEOUT sentinel, task `done`
- [ ] stub ignores SIGTERM, writes COMPLETE during grace → COMPLETE wins
- [ ] killpg ESRCH (already-exited) swallowed; remaining sessions still harvested
- [ ] no zombie after a timeout-kill (`proc.poll()` reaped)
- [ ] killed dirty tree + `.git/worktrees/<sid>/locked` → `remove_worktree` + prune fully clean
- [ ] alive past deadline, no result → TIMEOUT sentinel; task status `timeout`
- [ ] `_wait_until_next_event` sleeps only until the nearest deadline
- [ ] a `timeout` task is not integrated/verified

## Out of scope
Retry, attempts, branch-fork-fresh (task-106); budgets/orphan-kill (task-107).
