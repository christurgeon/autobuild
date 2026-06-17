# Hand-build batch: specs #1 (permissions) + #2 (timeout/budget)

**These are developer task specs — build them by hand, in order. Do NOT dogfood them.**
You cannot safely use the autobuild loop to build the fixes to the loop's own permission
and liveness safety: the bug that breaks autonomy is exactly the one you'd be relying on
to fix it. Dogfood #3/#4 later, after these land.

## Dependency order (foundation first)

```
task-101  Sentinel write discipline (conditional + atomic)        [foundation]
   └─ task-102  Session permission posture + spawn argv (#1)
        ├─ task-103  Usage capture + legible exit failures (#1)
        └─ task-104  Process-group spawn + monotonic deadlines + pgid (#2)
             └─ task-105  Session kill + timeout harvest + worktree cleanup (#2)
                  └─ task-106  Retry: durable attempts, fork-fresh, backoff (#2)
                       └─ task-107  Run budgets/timeouts, end-states, orphan kill (#2)
                          (also depends on task-103 for per-session cost)
```

Execution waves if you parallelize: `101` → `102` → {`103`, `104`} → `105` → `106` → `107`.

## Why this order
- **101 first** because both specs write sentinels; the conditional+atomic guard is the base
  for "a kill never clobbers a valid result" (#2) and "a confined agent's write isn't torn" (#1).
- **102 before 104** because both touch `spawn_session`'s argv/`meta.json`; stabilize the
  permission argv before adding process-group/pgid plumbing.
- **107 last** because it needs per-session cost (103) and the retry/termination machinery (106).

## How to verify each task
Every task ships its own test matrix. After each: `uv run pytest` must be green, and no
prior task's tests may regress. Build TDD where practical (the kill/race and retry tasks
especially — write the red test first).

## Hand-verify, don't just test
**task-102** encodes a security posture (`bypassPermissions`, `--add-dir` scope, `.claude/`
neutralization, no-push-creds requirement). Review it as a security change, not just a green
suite — the test suite cannot prove the sandbox actually withholds credentials/egress.
