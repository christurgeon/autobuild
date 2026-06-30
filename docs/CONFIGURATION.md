# Configuration

Everything autobuild does is controlled by `.autobuild/config.yml`, written by
`autobuild init`. All keys are optional and fall back to the defaults below. The file is
**validated at load** — a bad value (e.g. `integration: prr`, `max_parallel: 0`) fails fast
with exit 2 *before* any session spawns.

```yaml
model: claude-opus-4-8        # passed to `claude --model`
max_parallel: 3               # WIP limit / number of concurrent worktrees
base_branch: main             # what feature branches fork from and merge into
max_iterations: 100           # global safety stop for the outer loop
run_budget_seconds: 0         # whole-run wall-clock ceiling (int >= 0; 0 = unlimited).
                              # Once spent, the loop stops claiming new tasks, drains
                              # what's in flight, and reports the cap. Monotonic +
                              # in-memory, so a killed/resumed run restarts the budget.

integration: pr               # pr | auto-merge | branch
                              #   pr        -> open a PR per finished task (default)
                              #   auto-merge-> if checks pass, merge the branch
                              #   branch    -> just leave the branch; you merge later

integration_max_retries: 2    # (pr mode) extra attempts with backoff for a transient
                              # push / `gh pr create` hiccup (int >= 0; 0 = single shot).
                              # Auth / no-remote / merge conflicts stay un-retried.

checks:                       # run after implement; must all pass to commit/finish
  - npm run typecheck
  - npm run lint
  - npm test

verify_checks: true           # reaper re-runs `checks` in the worktree before
                              # integrating a COMPLETE session; any failure blocks
                              # the task and keeps its branch (trust, but verify).
                              # false -> trust the agent, skip the re-run.

verify_after_merge: true      # auto-merge only: after a clean merge, re-run `checks`
                              # on the COMBINED base tree (catches semantic skew two
                              # green branches can't); on failure the merge is reverted
                              # and the task blocked. false -> land merges unverified.

claude_cmd: claude            # override if your CLI binary is named differently

dangerously_bypass_permissions: true  # DEFAULT: full --dangerously-skip-permissions ...
require_sandbox_for_bypass: false     # ... with no AUTOBUILD_SANDBOX gate (see SECURITY.md)
permission_mode: acceptEdits  # used only when bypass is OFF: plan|default|acceptEdits|bypassPermissions
allowed_tools: [Edit, Write, Read]   # (fenced mode) + Bash(git:*) and one Bash(<check>:*) per check
session_max_turns: 80         # --max-turns cap per session (int >= 1)
task_timeout_seconds: 3600    # per-session wall budget, monotonic (int >= 1)
kill_grace_seconds: 20        # SIGTERM -> wait -> SIGKILL grace (int >= 1)
timeout_max_retries: 2        # auto-retries for a timed-out task before it's left
                              # terminal `timeout` (int >= 0; 0 = block on the first
                              # timeout). Each retry re-spends task_timeout_seconds.

notify_command: ""            # shell command run on coarse run events ("" = disabled).
                              # See "Notifications" below.
```

The permission keys (`dangerously_bypass_permissions`, `require_sandbox_for_bypass`,
`permission_mode`, `allowed_tools`) decide how much a spawned session may do unattended —
read **[SECURITY.md](SECURITY.md)** before changing them or running unattended.

## Notifications

A long, walk-away `run` can ping you when something worth knowing happens — without you
watching the logs. Set `notify_command` to **any shell command**; autobuild runs it on a
small set of coarse, run-level events and passes the event + message as environment
variables. It is generic by design — wire it to Telegram, `ntfy`, email, a desktop
notifier, whatever — nothing is hardcoded into the harness.

| `AUTOBUILD_EVENT` | When it fires | `AUTOBUILD_MESSAGE` |
|---|---|---|
| `done` | the run ended (`drained` / `settled` / `max_iterations`) | the terminal reason + per-status task counts |
| `halt` | a session escaped onto `base_branch` and the run **halted** (auto-merge) | the leaking base branch / session / task |
| `needs_human` | a session reported `NEEDS_HUMAN` | the task id + its summary |

Events are **low-volume by design** — run-level plus `NEEDS_HUMAN`, never one-per-task —
so a notifier won't spam you. The hook is **best-effort**: it's bounded by a timeout and
**every failure is swallowed** (and warned), so a wedged or broken notifier can never hang
or break a run. The command is a shell *you* control, so — like the permission allowlist —
it is **not a security boundary**.

```yaml
# Example: Telegram via curl (TOKEN / CHAT from the environment)
notify_command: 'curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage"
  --data-urlencode "chat_id=$CHAT" --data-urlencode "text=[$AUTOBUILD_EVENT] $AUTOBUILD_MESSAGE"'
```
