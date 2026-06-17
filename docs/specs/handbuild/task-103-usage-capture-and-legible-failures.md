---
id: task-103
title: "#1 — Usage capture + legible exit failures"
status: todo
priority: 2
depends_on: [task-102]
---

## Goal
Capture cost/usage from each session and turn opaque nonzero exits (e.g. permission
denials, model errors) into actionable sentinels instead of a generic "crashed."

## Context
- With `--output-format json` (added to argv in task-102), the session's final result is one
  JSON object on stdout (currently redirected to `session.out`).
- `spawn_session` only special-cases `FileNotFoundError`; any other nonzero exit without a
  `result.json` becomes a generic BLOCKED "exited without result", indistinguishable from a crash.

## Approach (resolved)
- Parse `session.out`'s final JSON into `usage.json` (`total_cost_usd`, `usage`, `num_turns`,
  `is_error`). This is **independent** of the agent-authored `result.json`, which remains the
  completion signal. Tolerate truncated/garbage JSON (never crash the reaper).
- In the exited-without-result path (`_harvest`/`reap_stalled`): inspect `proc.returncode` and
  the tail of `session.err`; write the sentinel **via `write_sentinel_if_absent`** naming the
  exit code and pointing at `session.err`, detecting a permission-denial / max-turns exit when identifiable.
- Update the stub `claude` to emit a JSON result blob so existing tests pass.

## Acceptance criteria
- [ ] `usage.json` is produced from a JSON result; `result.json` is still the sole completion signal.
- [ ] Garbage/truncated JSON does not crash the reaper.
- [ ] A nonzero exit with no result yields a sentinel citing the exit code + `session.err`.

## Test matrix
- [ ] valid JSON result → `usage.json` with cost/turns
- [ ] garbage/truncated JSON → no crash, reaper proceeds, usage absent
- [ ] a `usage.json` alone (no `result.json`) is NOT treated as finished
- [ ] nonzero exit + no result → sentinel cites exit code and `session.err` path
- [ ] stub emits JSON; full e2e green

## Out of scope
Run-level cost budget (task-107).
