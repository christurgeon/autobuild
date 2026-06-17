---
id: task-104
title: "#2 — Process-group spawn + monotonic deadlines + pgid persistence"
status: todo
priority: 2
depends_on: [task-102]
---

## Goal
Lay the plumbing that makes a session killable and time-bounded. No kill logic yet — just
the durable handles the kill path (task-105) and crash recovery (task-107) need.

## Context
- `RunningSession` tracks the child only via `proc.poll()`; there is no deadline, no pgid,
  no process group, so `claude`'s children can't be reliably reaped.
- `meta.json` records a wall-clock `started` only.

## Approach (resolved)
- `Popen(start_new_session=True)` so the child is a process-group leader (POSIX `setsid`;
  document Unix-only).
- Persist `pgid = os.getpgid(proc.pid)` to `meta.json` at spawn (needed by reconcile to kill
  orphans after a supervisor crash).
- `RunningSession` gains `deadline = time.monotonic() + task_timeout_seconds` and `pgid`.
  Keep wall-clock `started` in `meta.json` for **display only**; all deadline math is monotonic.
- Config: `task_timeout_seconds` (int≥1, default 1800), `kill_grace_seconds` (default 10).
  Validate; add to `KNOWN_KEYS`.

## Acceptance criteria
- [ ] The spawned process is its own group leader (`os.getpgid(pid) == pid`).
- [ ] `meta.json` contains `pgid`.
- [ ] `RunningSession.deadline` is monotonic-based; a wall-clock change does not affect it.
- [ ] `task_timeout_seconds` / `kill_grace_seconds` validated.

## Test matrix
- [ ] spawned proc is a group leader (`os.getpgid(pid) == pid`)
- [ ] `meta.json` contains the `pgid`
- [ ] deadline computed from `time.monotonic`; a mocked wall-clock jump doesn't shift it
- [ ] config validation for `task_timeout_seconds` / `kill_grace_seconds`
- [ ] regression: normal spawn/reap still works

## Out of scope
The kill itself, harvest ordering, worktree cleanup (task-105).
