---
name: autobuild-configure
description: Use when setting up autobuild and you need to write .autobuild/config.yml — choose the checks, integration mode, parallelism and timeouts, and especially the security / permission posture. Triggers on "configure autobuild", "set up the checks", "which integration mode", "is it safe to run autobuild unattended", "what permission mode".
---

# autobuild: configure the run

> If you were handed exactly one task via a session `meta.json`, you are a spawned
> autobuild session — this skill does NOT apply. Follow your assigned task instead.

You are helping a human write `.autobuild/config.yml`. `autobuild init` already laid down
a template with safe defaults and explanatory comments — **tailor it, don't rewrite from
scratch.** Most keys are fine as-is; the two that need real thought are the **checks**
(the gate every task must pass) and the **security posture** (how much an unsupervised
session may do).

## Process

1. **Set the checks.** Find the project's typecheck / lint / test commands and make them
   `checks:` — every command must pass for a task to be done, and the reaper re-runs them
   before integrating. Sources, in order of authority:
   - **`.github/workflows/` (CI)** — mirror its invocation form exactly, including any
     runner prefix (`uv run …`, `npm run …`), so the harness gate equals what CI enforces
     on the PR. Checks run from the worktree root, so the runner must be on PATH there.
   - **`GOAL.md`** definition-of-done (it often names the checks directly).
   - The package manifest (`pyproject.toml`, `package.json`) and any Makefile/justfile.
   - **REPLACE the seeded placeholder check** `echo 'replace me…'` — it exits 0, so
     leaving it means sessions pass the gate with *zero* real verification. This is the
     most common footgun. Confirm the final list with the user.
2. **Choose the integration mode** — `pr` (default; opens a PR per finished task),
   `auto-merge` (merge the branch when checks pass), or `branch` (leave the branch, you
   merge later). Recommend `pr` unless the user explicitly wants hands-off merging.
3. **Settle the security posture — pick deliberately, never silently default.** A spawned
   session is a headless `claude -p` acting on the repo with little supervision,
   inheriting this machine's git credentials and network. Map the user's situation with
   this table:

   | User's environment | Posture | Keys |
   |---|---|---|
   | Disposable sandbox VM, or no-push creds | **Full bypass** | `dangerously_bypass_permissions: true`, `require_sandbox_for_bypass: false` |
   | Normal machine, real creds, *will* set `AUTOBUILD_SANDBOX=1` | **Sandbox-gated bypass** | `require_sandbox_for_bypass: true` |
   | Normal machine, real creds, **won't** run a VM | **Fenced** | `dangerously_bypass_permissions: false` + `permission_mode` + `allowed_tools` |

   - **Sandbox-gated bypass refuses to spawn at all unless `AUTOBUILD_SANDBOX=1` is set.**
     So if the user won't set up a sandbox, that option is effectively *off* — recommend
     **Fenced**, the only posture that actually runs on a credentialed dev machine.
   - When you pick **Fenced**, `require_sandbox_for_bypass` is moot (bypass is already
     off); leaving it `false` or flipping it `true` are both harmless — `true` is a tidy
     belt-and-suspenders so bypass can't silently re-engage.
   - Reassurance worth stating: the harness already **strips push/transport credentials**
     (`GH_TOKEN`/`GITHUB_TOKEN`/`GITLAB_TOKEN`, `SSH_AUTH_SOCK`, askpass, `GIT_SSH_COMMAND`,
     inline `GIT_CONFIG_*`) from the session env as defense-in-depth, so Fenced on a dev
     machine is reasonable. But it is **not** VM-grade: file-based creds and the network
     remain, and `Bash(git:*)` is ~a full shell, so the allowlist is ergonomics, not a
     hard boundary. Only a disposable VM truly contains a hostile/prompt-injected agent.
4. **Set the operational keys** sanely: `model`, `max_parallel` (WIP limit / concurrent
   worktrees), `base_branch`, `max_iterations`, `verify_checks`, and the timeout trio
   (`task_timeout_seconds`, `kill_grace_seconds`, `timeout_max_retries`). The template
   defaults are fine for most projects.
   - **Heads-up — worktree isolation has no config keys, only env vars.** `autobuild run`
     **refuses to start with a dirty base tree** (uncommitted source outside `tasks/` +
     `.autobuild/`), so tell the user to **commit GOAL.md/tasks/config before running**;
     `AUTOBUILD_ALLOW_DIRTY_BASE=1` overrides it (sibling of `AUTOBUILD_SANDBOX`). And the
     reaper blocks any session that committed onto `base_branch` (in `auto-merge` it halts
     the run) — there's nothing to tune, just know it exists when a run won't start or a
     task blocks with a `leak.json`.
5. **Show the diff, confirm, write.** Present the change **inline as old→new** (there's no
   command for this in an interactive session — show it in chat), plus a one-line
   plain-English statement of the chosen security posture. **Also update the posture
   comment block to describe the posture you chose** — the template's comments describe
   the default bypass posture, so leaving them stale next to fenced values is misleading.
   Write it. The config is
   validated when `autobuild run` starts (a bad value fails fast with exit 2); suggest
   `autobuild status` to confirm it loads.

## Next step

If the backlog isn't planned yet, suggest **autobuild-plan-backlog**; otherwise the user
is ready for `autobuild run`.
