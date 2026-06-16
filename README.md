# autobuild

A lightweight, file-native harness that drains a human-curated backlog toward a
**GOAL** by spawning fresh, isolated Claude Code sessions in parallel git
worktrees, each following a **plan → review → implement** contract.

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
`.autobuild/` so every run is disposable and crash-safe.

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

The loop, in one breath: **scheduler** picks the highest-priority unblocked tasks and
atomically claims up to `max_parallel` of them → each claimed task gets a **fresh
Claude session in its own git worktree** → the session plans, self-reviews, implements,
runs checks, commits, and writes a `result.json` sentinel → the **reaper** marks the
task `done` (opening a PR or auto-merging per config) or files a follow-up task on
`BLOCKED` → repeat until the backlog is drained or a stop condition trips.

## Quick start

```bash
# 1. Install (just put bin/ on your PATH)
export PATH="$PWD/bin:$PATH"

# 2. In your target project, lay down the templates
cd ~/my-big-project
autobuild init

# 3. Write your GOAL.md and a few tasks/ files, then:
autobuild run            # drains the backlog
autobuild status         # see task + session state at any time
```

## Commands

| Command | What it does |
|---|---|
| `autobuild init` | Copy `GOAL.md`, `CLAUDE.md`, `tasks/`, and `.autobuild/config.yml` into the current project. |
| `autobuild run` | Run the outer loop: schedule → spawn sessions in worktrees → reap. Repeats until done. |
| `autobuild status` | Print every task's status and any in-flight sessions. |
| `autobuild reap` | One-shot: collect finished sessions, update tasks, open PRs / merge per config. |
| `autobuild clean` | Remove finished worktrees and stale session dirs. |

## Configuration (`.autobuild/config.yml`)

```yaml
model: claude-opus-4-8        # passed to `claude --model`
max_parallel: 3               # WIP limit / number of concurrent worktrees
base_branch: main             # what feature branches fork from and merge into
max_iterations: 50            # global safety stop for the outer loop

integration: pr               # pr | auto-merge | branch
                              #   pr        -> open a PR per finished task (default)
                              #   auto-merge-> if checks pass, merge the branch
                              #   branch    -> just leave the branch; you merge later

checks:                       # run after implement; must all pass to commit/finish
  - npm run typecheck
  - npm run lint
  - npm test

claude_cmd: claude            # override if your CLI binary is named differently
```

## Why not just `/loop`?

The Claude Code `/loop` skill is the fastest way to try the *inner* loop with zero
setup — see [`docs/loop-lite.md`](docs/loop-lite.md). But `/loop` runs inside **one
growing session** (context rot on long builds) and can't fan out to parallel
worktrees. autobuild keeps each iteration a **fresh, disposable process** and runs
several at once. Use `/loop` to prototype; use autobuild to actually drain a backlog.

## Status

v0 / MVP scaffold. The loop, scheduler, worktree, and session modules are wired up
with a real (if minimal) bash implementation. See `lib/` and the `TODO` markers for
the parts that want hardening (YAML edge cases, PR creation, richer check reporting).

## License

MIT
