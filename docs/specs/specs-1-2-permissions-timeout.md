# Hardening specs: #1 Autonomy/permissions posture + #2 Per-session timeout/budget/retry

> Two staff-bar specs for making autobuild safe to run a real, unattended build. Each
> was drafted, then adversarially reviewed against the actual code; 🛡️ marks where the
> review changed the design. These are the two **blockers** before pointing the harness
> at a non-toy project.
>
> Sequencing: land the shared **sentinel write discipline** first (location + conditional
> write + atomic), then #1, then #2 (`depends_on` #1). Hand-verify #1 — it's a security
> posture decision and, until it lands, real confined sessions can't run at all.

---

## Spec #1 — Autonomy & permissions posture

**Goal:** make a headless `claude -p` session able to do real work unattended, with a
security model that is honest about where the boundary actually is.

### Key design decisions (resolved)

1. 🛡️ **Fix the sentinel-location bug first (it is a blocker in current code, not just the spec).**
   The session runs with `cwd=<root>/.autobuild/worktrees/<id>` but is told to write
   `result.json` to `<root>/.autobuild/sessions/<id>` — a *sibling subtree, outside cwd*.
   Under any real file confinement the agent cannot write its own sentinel, every task
   fails closed, and it looks like a generic crash. **Decision:** pass
   `--add-dir <session_dir>` scoped to *exactly* that session's dir (never the parent
   `.autobuild/sessions/` or `<root>`), so the agent can write its sentinel while edits
   stay confined to the worktree. Keep the session dir persistent (preserves
   `reaped.json` idempotency + audit trail). *(Considered: relocating the sentinel into
   the worktree — rejected because worktree removal would destroy the idempotency marker
   and audit logs.)*
2. 🛡️ **The allowlist is ergonomics, not security — say so.** `--allowedTools` with
   `Bash(git:*)` ≈ a full shell (`bash -c`, `git config core.pager`, chaining all escape
   it). **The only real isolation is the disposable sandbox VM.** Document this plainly.
3. **Fail-safe default, env-gated bypass.**
   - Default `permission_mode: acceptEdits` + an allowlist that *actually covers the
     workflow*: Edit/Write/Read, `Bash(git:*)`, one `Bash(<cmd>:*)` per `checks` entry,
     plus common file ops — 🛡️ because `acceptEdits` auto-accepts *edits only*; Bash
     still needs the allowlist or the checks dead-end.
   - `bypassPermissions` (`--dangerously-skip-permissions`) is the *intended* mode in the
     cloud sandbox, but **hard-refused** unless `dangerously_bypass_permissions: true`
     **and** `AUTOBUILD_SANDBOX=1` is set. No auto-detection; refuse loudly otherwise.
4. 🛡️ **Agent gets no push credentials and no network.** An autonomous (or prompt-injected
   via `GOAL.md`/task files) agent with ambient git creds can `git push origin HEAD:main`
   and bypass the verify-before-integrate gate. Pushing stays the *harness's* job,
   post-verification. `integration: pr` is only safe when the sandbox withholds
   credentials/egress — document as a hard requirement.
5. 🛡️ **Neutralize a hostile target repo's `.claude/`.** A cloned repo's
   `.claude/settings.json` hooks (`SessionStart`/`PreToolUse`) execute with the agent's
   privileges on spawn. Pass `--strict-mcp-config`; deny writes to `.claude/**`; document
   that the VM is the real containment.
6. 🛡️ **Capture usage separately from the completion signal.** Add `--output-format json`,
   parse stdout (`session.out`) into `usage.json`. The completion signal remains the
   agent-authored `result.json`. Update the test stub to emit a JSON result blob.
7. 🛡️ **Make denied-permission failures legible.** Inspect `proc.returncode` + tail of
   `session.err`; on nonzero-and-no-result, write a sentinel naming the exit code and
   pointing at `session.err` (detect permission-denial when possible), instead of the
   generic "exited without result → BLOCKED."

### Config (validated at load, added to `KNOWN_KEYS`)
```yaml
permission_mode: acceptEdits          # enum {plan,default,acceptEdits,bypassPermissions}
allowed_tools: [Edit, Write, Read, "Bash(git:*)"]   # + auto-derived per `checks` cmd
session_max_turns: 40                 # --max-turns; int >= 1
dangerously_bypass_permissions: false # true => --dangerously-skip-permissions ...
require_sandbox_for_bypass: true      # ... only if AUTOBUILD_SANDBOX=1, else refuse
capture_usage: true                   # --output-format json -> usage.json
```

### Must-have tests
- Sentinel path the agent is told to write is reachable: `--add-dir` target == its
  session dir, and that dir is *not* the parent/root.
- `acceptEdits` ⇒ every `checks` cmd + `git commit` appear in `--allowedTools`.
- bypass refused without `AUTOBUILD_SANDBOX`; permitted with it.
- nonzero exit + no result ⇒ sentinel cites exit code / `session.err`.
- `--output-format json` parsed to `usage.json`; result.json still the completion signal;
  garbage JSON doesn't crash the reaper.
- hostile-`.claude/`-hook fixture repo: hook does not execute (strict-mcp + deny).
- config validation: bad `permission_mode`, `session_max_turns: 0`, `allowed_tools: "Edit"`
  each aggregate into one `ConfigError`.

---

## Spec #2 — Per-session timeout, kill, budget & retry

**Goal:** no session can hang the loop or run unbounded, and a kill never destroys work
that actually finished. Depends on #1 (shared sentinel contract + `--output-format json`).

### Key design decisions (resolved)

1. 🛡️ **Reapable always wins — harvest ordering is load-bearing.** A valid `result.json`
   beats a deadline. Order in `_harvest`: (1) reapable → reap; (2) exited-without-result
   → BLOCK; (3) alive & past deadline → kill; (4) alive & within deadline → survive.
   **After kill+grace, re-classify** and only then write a timeout sentinel.
2. 🛡️ **`write_sentinel_if_absent` — stop clobbering results.** `write_sentinel` currently
   overwrites `result.json` unconditionally. Add a guard that refuses to overwrite a
   parseable result or when `reaped.json` exists, and **route every harness sentinel
   write through it**. Require the agent to write `result.json` **atomically** (temp +
   `os.replace`) so `_classify_sentinel` never sees a half-written file.
3. 🛡️ **Process-group kill done correctly.** `Popen(start_new_session=True)`, persist
   `pgid` to `meta.json`. `_kill_group`: `poll()` → SIGTERM → `wait(grace)` → SIGKILL →
   `proc.wait()` (reap the zombie). Wrap **every** `killpg` in
   `suppress(ProcessLookupError)` — else the first already-exited session throws ESRCH
   and strands the whole harvest. (Unix-only; document it.)
4. 🛡️ **Clean up a killed worktree.** Before `remove_worktree`: `git merge --abort` /
   `rebase --abort`, drop stale `.git/worktrees/<sid>/locked` + `index.lock`; on rmtree
   fallback, `git worktree prune`. **Delete the partial branch** (`git branch -D
   autobuild/<tid>`) so a retry can't inherit a poisoned tree.
5. 🛡️ **Retry: fork fresh, persist attempts durably.** `make_worktree` *reuses* an
   existing `autobuild/<tid>` branch — fatal for retries. Retry must fork fresh from
   `base_branch`. Persist `attempts:` in the **task frontmatter** (crash-safe, per-task)
   via a surgical+atomic `bump_attempt`; `read_task` parses it; claim treats
   `attempts >= max_retries` as terminal. Per-session `meta.json` can't hold this (a
   retry is a new session id). Add `retry_backoff_seconds` before re-spawn.
6. 🛡️ **`timeout` is a distinct, retryable status** — not overloaded onto `blocked`.
   Timeouts never enter the integrate/`verify_checks` path. Terminal only once attempts
   are exhausted.
7. 🛡️ **Budget honesty.** `total_cost_usd` is only known *after* a session ends, so
   per-task `cost_budget_usd` is **accounting only** (gates retry eligibility, can't
   preempt). The only preemptive per-session lever is `--max-turns`. `run_budget_usd` /
   `run_timeout_seconds` are enforced at **claim time** and are **racy by `max_parallel`**
   — worst-case overshoot ≈ cost of `max_parallel` in-flight sessions. Document it; not a
   hard cap.
8. 🛡️ **Monotonic deadlines.** `deadline = time.monotonic() + task_timeout_seconds`;
   wall-clock `started` is display-only (NTP steps / suspend must not skew kills).
9. 🛡️ **Bounded wait, not block-on-one-child.** Replace `_wait_any` with
   `sleep(max(0, min(sleep_seconds, nearest_deadline - now)))`.
10. 🛡️ **Explicit run end-states:** `DRAINED | STUCK | RUN_BUDGET_EXCEEDED | RUN_TIMEOUT`.
    The settle check must treat retry-pending-within-backoff as *not settled*, and must
    **drain in-flight sessions** before exiting on a budget/time stop.
11. 🛡️ **Kill orphans on reconcile.** With `pgid` persisted in `meta.json`,
    reconcile-under-lock `killpg`s each orphaned in-progress session **before** writing
    its orphaned-BLOCKED sentinel — the only crash-safe reclamation.

### Config (validators need float/optional support; `0` = unset for turns)
```yaml
task_timeout_seconds: 1800   # int>=1, monotonic, per session
kill_grace_seconds: 10       # SIGTERM -> wait -> SIGKILL
session_max_turns: 0         # 0=unset; --max-turns when >0
max_retries: 0               # per task; attempts persisted in frontmatter
retry_backoff_seconds: 30
cost_budget_usd: null        # per task: ACCOUNTING ONLY (gates retry, not live)
run_timeout_seconds: null    # whole run, monotonic
run_budget_usd: null         # claim-time gate; racy by max_parallel
```

### Must-have tests
- **Result wins the race:** stub writes COMPLETE ~at deadline ⇒ reaped COMPLETE, never
  overwritten by timeout; task `done`.
- **Finish-during-grace:** stub ignores SIGTERM, writes COMPLETE before SIGKILL ⇒
  COMPLETE wins.
- `write_sentinel_if_absent` refuses to clobber a parseable result or when `reaped.json`
  exists.
- **ESRCH safety:** a session whose child already exited doesn't abort the harvest.
- **No zombies** after a timeout-kill (`proc.poll()` reaped).
- **Retry forks fresh:** time out attempt 1 (commits partial work) ⇒ attempt 2 forks from
  `base_branch`, not the poisoned branch.
- **Attempts survive a crash:** bump → crash + reconcile ⇒ count persists; exactly
  `max_retries+1` attempts, then terminal `timeout`.
- **Termination:** retry-pending-within-backoff ⇒ no premature COMPLETE; `run_budget` /
  `run_timeout` reached with sessions in flight ⇒ drain then exit
  `RUN_BUDGET_EXCEEDED`/`RUN_TIMEOUT`.
- **Overshoot bound:** ≤ `max_parallel` sessions finish past the budget threshold before
  claiming stops.
- **Orphan kill:** persisted pgid ⇒ fresh run's reconcile `killpg`s the orphan before
  BLOCKing.
- **Monotonic:** a mocked wall-clock jump neither prematurely kills nor extends a session.

---

_Wave 2 (Planner + acceptance-level verification) to follow as a separate issue._
