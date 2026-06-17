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
runs checks, commits, and writes a `result.json` sentinel → the **reaper**
re-runs the configured `checks` against that worktree itself (trust, but verify) and,
only if they pass, marks the task `done` (opening a PR or auto-merging per config);
a failed check or a `BLOCKED` sentinel leaves the branch and blocks the task → repeat
until the backlog is drained or a stop condition trips.

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

verify_checks: true           # reaper re-runs `checks` in the worktree before
                              # integrating a COMPLETE session; any failure blocks
                              # the task and keeps its branch (trust, but verify).
                              # false -> trust the agent, skip the re-run.

claude_cmd: claude            # override if your CLI binary is named differently
```

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

Parallelism is real OS processes (`Popen` + `poll()`), supervised by a single `run`.
There is no in-context state: kill `run` and re-run it — a startup *reconcile* pass
recovers orphaned work from files + git, so every iteration stays disposable.

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
