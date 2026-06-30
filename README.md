# autobuild

> **Point it at a backlog. Walk away. Come back to merged PRs.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Runtime deps: 1](https://img.shields.io/badge/runtime%20deps-1%20(PyYAML)-brightgreen.svg)](pyproject.toml)
[![CI](https://github.com/christurgeon/autobuild/actions/workflows/ci.yml/badge.svg)](https://github.com/christurgeon/autobuild/actions/workflows/ci.yml)

autobuild drains a human-curated backlog toward a **`GOAL`** by spawning fresh,
isolated Claude Code sessions in parallel git worktrees — each one **plans,
self-reviews, implements, runs your checks**, and then opens a PR or auto-merges.
Every session starts from a clean context; all state lives in **files and git**, so a
run is disposable and crash-safe. Kill it mid-flight and re-run — it recovers from the
files alone.

<!-- Demo: a real autobuild run fanning out across worktrees and draining the backlog.
     Recorded token-free against the repo's stub agent (real orchestration, canned edits). -->
![autobuild draining a backlog in parallel worktrees](docs/demo.gif)

Think of it as the missing glue between three ideas that already work:

- **[Ralph loop](https://github.com/ghuntley/how-to-ralph-wiggum)** — run an agent
  with a *fresh context* every iteration; keep all state in files + git, never in
  the context window.
- **[Karpathy's autoresearch](https://github.com/karpathy/autoresearch)** — give the
  agent a compact area of control, explicit constraints, and a stopping criterion.
- **[Backlog.md](https://github.com/MrLesk/Backlog.md)** — one markdown file per task,
  with status and acceptance criteria, readable by humans and agents alike.

autobuild ties them together and adds the part nobody automates: **worktree fan-out**
so N agents run in parallel without colliding, and **per-session state** under
`.autobuild/` that a single supervisor verifies before anything lands on your branch.

## The mental model

```
GOAL.md            <- you write this. The north star. Rarely changes.
CLAUDE.md          <- the contract every session obeys (plan->review->implement).
tasks/             <- one .md per task. Humans own it; agents append follow-ups.
  task-001-*.md
.autobuild/        <- machine state. Safe to delete; rebuilt from tasks/ + git.
  config.yml
  sessions/<id>/   <- one dir per agent run: plan.md, progress.log, result.json
  worktrees/<id>/  <- the isolated checkout that session worked in
  backlog.lock     <- atomic task-claiming so parallel agents never grab the same task
```

The loop, step by step:

1. The **scheduler** picks the highest-priority unblocked tasks and atomically claims up
   to `max_parallel` of them.
2. Each claimed task gets a **fresh Claude session in its own git worktree**. The session
   plans, self-reviews, implements, runs your checks, commits, and writes a `result.json`
   sentinel.
3. The **reaper** re-runs the configured `checks` against that worktree itself (*trust, but
   verify*) and, only if they pass, marks the task `done` — opening a PR or auto-merging per
   config.
4. A failed check or a `BLOCKED` sentinel leaves the branch intact and blocks the task. A
   session that blows its `task_timeout_seconds` deadline has its whole process group killed
   and is **automatically retried on a fresh branch** up to `timeout_max_retries` times; once
   that budget is spent the task is left in a terminal `timeout` state for you to triage.
5. Repeat until the backlog is drained or a stop condition trips.

## What a run looks like

Say you've written four tasks — `db-schema`, then `api-layer` and `cli-flags` (both depend
on it), then `docs` (depends on the API). With `max_parallel: 3`:

```text
$ autobuild run
iter 1  claimed: db-schema                  → session a1 (worktree autobuild/db-schema)
iter 1  reaped:  db-schema   COMPLETE  ✓ checks passed  → merged
iter 2  claimed: api-layer, cli-flags        → sessions b2, c3  (running in parallel)
iter 2  reaped:  cli-flags   COMPLETE  ✓ checks passed  → PR #41
iter 2  reaped:  api-layer   BLOCKED   ✗ left branch autobuild/api-layer for you
iter 3  skipped: docs  (blocked: depends on api-layer)
backlog settled: 2 done, 1 blocked, 1 waiting — nothing left to run
```

Independent tasks ran at once; the dependent `docs` task never started because its
dependency blocked; the blocked branch is preserved for you to pick up. No context carried
between any of them — each was a clean process driven entirely by the files.

## Quick start

```bash
# 1. Install the CLI. autobuild is a uv-managed Python package; uv provisions a
#    Python 3.11+ interpreter for you. From a checkout of this repo:
uv tool install .              # installs the `autobuild` command globally
# …or run it without installing, straight from the checkout:
#   uv run autobuild <command>

# 2. In your target project, lay down the templates
cd ~/my-big-project
autobuild init

# 3. Write your GOAL.md and a few tasks/ files, then commit them — `run` refuses to
#    start with a dirty base tree, so an escaped session can't sweep uncommitted work
#    into a task commit (override: AUTOBUILD_ALLOW_DIRTY_BASE=1):
git add -A && git commit -m "autobuild backlog"

# 4. Drain the backlog:
autobuild run            # drains the backlog
autobuild status         # see task + session state at any time
```

> **Want a filled-out backlog to look at first?** [`examples/quotes-api/`](examples/quotes-api/)
> is a complete worked example — a `GOAL.md` plus six tasks with a real dependency graph — that
> you can read or copy into a fresh repo (see [`examples/README.md`](examples/README.md)).

## Authoring skills

The hard part of autobuild isn't the loop — it's writing a good `GOAL.md`, decomposing it into
right-sized, dependency-ordered tasks, and picking a safe config. So `autobuild init` also installs
four **Claude Code Skills** into your project's `.claude/skills/`. Run Claude Code interactively in
your project and these trigger on what you ask:

| Skill | Use it to |
|---|---|
| `autobuild-author-goal` | Interview you, then write a tight, testable `GOAL.md`. |
| `autobuild-plan-backlog` | Turn the GOAL into a DAG of right-sized `tasks/*.md` with checkable acceptance criteria. |
| `autobuild-configure` | Tailor `.autobuild/config.yml` — checks, integration mode, and the security posture. |
| `autobuild-triage` | After a run settles, explain *why* tasks blocked or timed out and propose re-queues/fixes. |

They're for the **human** setting up and operating a backlog — each carries a guard so a spawned
single-task session never invokes one. Re-running `autobuild init` won't clobber a skill you've edited.

## Commands

| Command | What it does |
|---|---|
| `autobuild init` | Copy `GOAL.md`, `CLAUDE.md`, `tasks/`, and `.autobuild/config.yml` into the current project, and install the [authoring/operating skills](#authoring-skills) under `.claude/skills/`. |
| `autobuild doctor` | Preflight the environment (`claude` on PATH, git identity, base branch, disk, `gh` auth): PASS/WARN/FAIL report, exits non-zero on any FAIL. `run` enforces the critical checks itself. |
| `autobuild run` | Run the outer loop: schedule → spawn sessions in worktrees → reap. Repeats until done. |
| `autobuild status` | Print every task's status and any in-flight sessions. |
| `autobuild reap` | One-shot: collect finished sessions, update tasks, open PRs / merge per config. |
| `autobuild clean` | Remove finished worktrees and stale session dirs. |

## Configuration (`.autobuild/config.yml`)

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
require_sandbox_for_bypass: false     # ... with no AUTOBUILD_SANDBOX gate (see warning below)
permission_mode: acceptEdits  # used only when bypass is OFF: plan|default|acceptEdits|bypassPermissions
allowed_tools: [Edit, Write, Read]   # (fenced mode) + Bash(git:*) and one Bash(<check>:*) per check
session_max_turns: 80         # --max-turns cap per session (int >= 1)
task_timeout_seconds: 3600    # per-session wall budget, monotonic (int >= 1)
kill_grace_seconds: 20        # SIGTERM -> wait -> SIGKILL grace (int >= 1)
timeout_max_retries: 2        # auto-retries for a timed-out task before it's left
                              # terminal `timeout` (int >= 0; 0 = block on the first
                              # timeout). Each retry re-spends task_timeout_seconds.
```

## Security posture (read before running unattended)

A spawned session is a headless `claude -p` acting on your repo with little supervision.
Be honest about where the boundary is:

- **The default is full bypass (`--dangerously-skip-permissions`), no sandbox gate.** Out
  of the box a session does whatever it needs without prompts — and inherits **this
  machine's git credentials and network**. A prompt-injected `GOAL.md`/task could push to
  your remote or exfiltrate. This is the right default *only* when those are disposable
  (a sandbox VM, or no-push credentials). autobuild prints a loud warning on every
  un-sandboxed bypass spawn. To run fenced, set `dangerously_bypass_permissions: false`.
- **To fence it, re-arm the gate.** Set `require_sandbox_for_bypass: true` and autobuild
  will **refuse to spawn** a bypass session unless `AUTOBUILD_SANDBOX=1` is set — the
  sandbox-only posture. With bypass off entirely, `permission_mode`/`allowed_tools` apply.
- **The allowlist is ergonomics, not a security boundary.** `--allowedTools` with
  `Bash(git:*)` is approximately a full shell (`git config core.pager=…`, command
  chaining, etc. all escape it). **The only real isolation is running autobuild inside a
  disposable sandbox VM.** `permission_mode`/`allowed_tools` keep an *honest* agent on the
  rails; they do not contain a hostile or prompt-injected one.
- **`integration: pr` is only safe when the agent has no push credentials and no network.**
  An autonomous (or prompt-injected via `GOAL.md`/task files) agent with ambient git creds
  could `git push origin HEAD:main` and bypass the verify-before-integrate gate. Pushing
  stays the *harness's* job, after verification — so withhold credentials/egress from the
  session environment.
- **The harness already strips env-based push credentials (defense-in-depth).** Before a
  session launches, its environment has git push tokens and transport helpers removed
  (`GH_TOKEN`/`GITHUB_TOKEN`/`GITLAB_TOKEN`, `SSH_AUTH_SOCK`, `GIT_ASKPASS`/`SSH_ASKPASS`,
  `GIT_SSH_COMMAND`, and inline `GIT_CONFIG_*` injection) — the agent keeps its commit
  identity and its own `ANTHROPIC_*` auth but loses the easy push primitive. This is **not**
  a secret scrubber: file-based credentials (`~/.git-credentials`, OS keychains, `~/.ssh`
  keys) and the network are still inherited, so a disposable VM remains the only real boundary.
- **A cloned target repo's `.claude/` is hostile input.** Its hooks would run with the
  agent's privileges; autobuild passes `--strict-mcp-config` and denies writes to
  `.claude/**`, but the real containment is still the VM.
- **A session is kept on its worktree, and an escape is caught.** The prompt anchors the
  agent only to its worktree and session dir — the contract, `GOAL.md`, and the task are
  *staged into the session dir*, so the agent is never handed a main-checkout path to
  resolve work against (the original escape vector). Belt-and-suspenders for the cases an
  honest anchor can't prevent: `run` snapshots `base_branch` at spawn and the reaper
  **refuses to integrate** if a session left a non-merge commit on base — the signature
  of an agent that committed onto the live base instead of its branch. In `auto-merge`
  (deliverables merge onto base) it **halts the whole run loudly** (`BaseBranchLeak`, exit
  2); in `pr`/`branch` (base is never integrated onto) it just blocks that one task and
  keeps going, so a concurrent commit to base can't stall unrelated work. `run` also
  **refuses to start with a dirty base tree** (override: `AUTOBUILD_ALLOW_DIRTY_BASE=1`),
  since a stray `git add -A` could otherwise sweep uncommitted work into a task commit.
  These keep an *honest* agent contained and make a dishonest one's mess detectable; they
  are not a substitute for the VM.

## Why not just `/loop`?

The Claude Code `/loop` skill is the fastest way to try the *inner* loop with zero
setup — see [`docs/loop-lite.md`](docs/loop-lite.md). But `/loop` runs inside **one
growing session** (context rot on long builds) and can't fan out to parallel
worktrees. autobuild keeps each iteration a **fresh, disposable process** and runs
several at once. Use `/loop` to prototype; use autobuild to actually drain a backlog.

## How it's built

A small, stdlib-first Python package (`autobuild/`). The only runtime dependency is
**PyYAML**; everything else — process orchestration, JSON sentinels, the atomic claim
lock — is the standard library.

| Module | Responsibility |
|---|---|
| `cli.py` | argparse dispatch + the `autobuild` entry point |
| `config.py` | load `.autobuild/config.yml` into a typed `Config` |
| `tasks.py` | frontmatter read + surgical, atomic status writes + the follow-up id allocator |
| `scheduler.py` | dependency gating, priority ordering, atomic claim under an `flock` |
| `worktree.py` | a git worktree + branch per session, with each task's `done` dependency branches layered onto its base |
| `session.py` | spawn one fresh `claude -p` via `subprocess.Popen` |
| `loop.py` | the outer loop, the reaper, crash-recovery reconcile, status, clean |
| `paths.py` | the one place every `.autobuild/` location is defined |

Parallelism is real OS processes (`Popen` + `poll()`), supervised by a single `run`
that holds a lock (`.autobuild/run.lock`), so a second `run` is refused rather than
colliding. There is no in-context state: kill `run` and re-run it — a startup
*reconcile* pass recovers orphaned work from files + git, so every iteration stays
disposable. `config.yml` is validated at load (a bad value fails fast with exit 2),
and `autobuild status` flags any tasks stuck behind unsatisfiable dependencies.

## Development

```bash
uv sync                  # create the venv (Python 3.11+) and install deps
uv run pytest            # run the full suite (unit + a token-free e2e loop)
```

The e2e tests drive the whole loop with a stub `claude` on `PATH` (see
`tests/fixtures/claude`) — no tokens spent — and assert tasks run in dependency
order.

## Limitations

**Dependency chains in `pr` mode produce stacked history.** So a dependent task can
see its dependencies' code in every integration mode, each session's worktree merges
its `done` dependencies' `autobuild/<dep>` branches onto its base. Under `auto-merge`
the dependency already landed on `base_branch`, so this is a no-op. Under `pr` (and
`branch`), the dependency's commits live only on its own branch, so a dependent's
branch — and therefore its PR — *contains its dependencies' commits*. For a chain
A → B → C you'll get stacked/overlapping diffs across the PRs. If you run a dependency
chain in `pr` mode, review/merge the PRs in dependency order (or merge them onto an
integration branch and open a single PR), rather than expecting independent diffs.

## License

MIT
