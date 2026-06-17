---
id: task-101
title: Foundation — sentinel write discipline (conditional + atomic)
status: todo
priority: 1
depends_on: []
---

## Goal
Make sentinel writes safe so neither a kill, a torn write, nor a confined agent can
corrupt or silently lose a result. This is the shared foundation for specs #1 and #2;
build it first.

## Context
- `session.py` `write_sentinel` overwrites `result.json` **unconditionally**.
- `loop.py` `_harvest` / `reap_stalled` / `reconcile` write BLOCKED sentinels directly.
- The agent writes `result.json` non-atomically (per `templates/CLAUDE.md`); a half-written
  file makes `_classify_sentinel` return `corrupt`.

## Approach (resolved)
- Add `write_sentinel_if_absent(sdir, tid, status, summary, ...)`: **refuse** to write if
  `reaped.json` exists OR `result.json` already parses to a dict; otherwise write
  **atomically** (temp in same dir + `os.replace`).
- Route **every** harness-authored sentinel write (worktree-creation BLOCK, exited-without-
  result BLOCK, orphan BLOCK, and the future TIMEOUT sentinel) through it. Keep `write_sentinel`
  itself atomic.
- Update `templates/CLAUDE.md`: the agent MUST write `result.json` atomically (write a temp
  file, then rename) so the reaper never observes a torn file. Update `tests/fixtures/claude`
  (the stub) to write atomically.

## Acceptance criteria
- [ ] `write_sentinel_if_absent` never overwrites a parseable `result.json` or when
      `reaped.json` exists; writes when absent or corrupt.
- [ ] All harness BLOCK/sentinel writes go through it (no direct `write_sentinel` for those paths).
- [ ] All sentinel writes are atomic (temp + `os.replace`).
- [ ] CLAUDE.md contract + stub updated for atomic agent writes.

## Test matrix
- [ ] refuses to overwrite a parseable `result.json`
- [ ] refuses when `reaped.json` exists
- [ ] writes when `result.json` is absent
- [ ] writes (overwrites) when `result.json` is present-but-corrupt
- [ ] atomicity: a concurrent reader never observes a partial file (simulate)
- [ ] regression: existing `reap_stalled` / `_harvest` / `reconcile` BLOCK paths still produce sentinels
- [ ] `uv run pytest` green

## Out of scope
Kill/timeout logic (task-105), permission flags (task-102).
