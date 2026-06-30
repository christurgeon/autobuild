# autobuild

> **Point it at a backlog. Walk away. Come back to merged PRs.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Runtime deps: 1](https://img.shields.io/badge/runtime%20deps-1%20(PyYAML)-brightgreen.svg)](pyproject.toml)
[![CI](https://github.com/christurgeon/autobuild/actions/workflows/ci.yml/badge.svg)](https://github.com/christurgeon/autobuild/actions/workflows/ci.yml)

autobuild drains a human-curated backlog toward a **`GOAL`** by spawning fresh, isolated
Claude Code sessions in parallel git worktrees — each one **plans, self-reviews, implements,
runs your checks**, then opens a PR or auto-merges. Every session starts from a clean
context; all state lives in **files and git**, so a run is disposable and crash-safe: kill it
mid-flight and re-run, and it recovers from the files alone.

<!-- Demo: a real autobuild run fanning out across worktrees and draining the backlog.
     Recorded token-free against the repo's stub agent (real orchestration, canned edits). -->
![autobuild draining a backlog in parallel worktrees](docs/demo.gif)

## How it works

```
GOAL.md       <- you write this. The north star. Rarely changes.
CLAUDE.md     <- the contract every session obeys (plan -> review -> implement).
tasks/        <- one .md per task. Humans own it; agents append follow-ups.
.autobuild/   <- machine state. Safe to delete; rebuilt from tasks/ + git.
```

1. The **scheduler** atomically claims up to `max_parallel` unblocked tasks.
2. Each gets a **fresh Claude session in its own git worktree** — it plans, self-reviews,
   implements, runs your checks, commits, and writes a `result.json`.
3. The **reaper** re-runs your `checks` against that worktree (*trust, but verify*) and, only
   if they pass, marks the task `done` — opening a PR or auto-merging per config.
4. Failures block the task and keep the branch; timeouts retry on a fresh branch. Repeat
   until the backlog drains.

A worked run and the internals are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Quick start

```bash
# 1. Install the CLI (uv-managed; provisions Python 3.11+ for you):
uv tool install .         # the `autobuild` command — or run ad hoc: uv run autobuild <command>

# 2. Scaffold a backlog in your project:
cd ~/my-big-project
autobuild init            # lays down GOAL.md, CLAUDE.md, tasks/, .autobuild/config.yml

# 3. Write GOAL.md + a few tasks/, then commit them (run refuses a dirty base tree):
git add -A && git commit -m "autobuild backlog"

# 4. Drain it:
autobuild run             # schedule -> spawn -> verify -> integrate, until done
autobuild status          # task + session state at any time
```

> **New here?** [`examples/quotes-api/`](examples/quotes-api/) is a complete worked backlog —
> a `GOAL.md` plus six tasks with a real dependency graph — to read or copy into a fresh repo.

`autobuild init` also installs four **Claude Code Skills** that help you *author and operate* a
backlog interactively — `autobuild-author-goal`, `autobuild-plan-backlog`, `autobuild-configure`,
and `autobuild-triage`. Run Claude Code in your project and they trigger on what you ask. Each
carries a guard so a spawned single-task session never invokes one, and re-running `init` won't
clobber a skill you've edited.

## Commands

| Command | What it does |
|---|---|
| `autobuild init` | Copy `GOAL.md`, `CLAUDE.md`, `tasks/`, and `.autobuild/config.yml` into the current project, and install the authoring skills under `.claude/skills/`. |
| `autobuild doctor` | Preflight the environment (`claude` on PATH, git identity, base branch, disk, `gh` auth): PASS/WARN/FAIL report, exits non-zero on any FAIL. `run` enforces the critical checks itself. |
| `autobuild run` | Run the outer loop: schedule → spawn sessions in worktrees → reap. Repeats until done. |
| `autobuild status` | Print every task's status and any in-flight sessions. |
| `autobuild reap` | One-shot: collect finished sessions, update tasks, open PRs / merge per config. |
| `autobuild clean` | Remove finished worktrees and stale session dirs. |

## Configuration

Everything lives in `.autobuild/config.yml` — the `model`, `max_parallel`, the `checks` to
run, the `integration` mode (`pr` / `auto-merge` / `branch`), timeouts and retries, a whole-run
budget, and a generic `notify_command` hook. It's validated at load, so a bad value fails fast.
See the annotated reference in **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

## ⚠️ Security — read before running unattended

By default a session runs with `--dangerously-skip-permissions` and **no sandbox**, inheriting
this machine's git credentials and network — safe *only* when those are disposable (a sandbox VM
or no-push credentials). autobuild fences what it can — it strips push tokens from the session
env, keeps each session in its worktree, and refuses to integrate a commit that escaped onto your
base branch — but **the only real isolation is a disposable VM**. Read
**[docs/SECURITY.md](docs/SECURITY.md)** before an unattended run.

## Why not just `/loop`?

The Claude Code `/loop` skill is the fastest way to try the *inner* loop with zero setup (see
[`docs/loop-lite.md`](docs/loop-lite.md)) — but it runs inside **one growing session** (context
rot on long builds) and can't fan out to parallel worktrees. autobuild keeps each iteration a
**fresh, disposable process** and runs several at once. Prototype with `/loop`; drain a backlog
with autobuild.

## Development

```bash
uv sync          # create the venv (Python 3.11+) and install deps
uv run pytest    # full suite — unit tests + a token-free e2e loop against a stub `claude`
```

## License

MIT
