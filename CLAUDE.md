# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo **is the `autobuild` tool** — a file-native **Python** harness that drains a
backlog of tasks toward a `GOAL.md` by spawning fresh, isolated Claude Code sessions
(`claude -p`) in parallel git worktrees. Read `README.md` for the full mental model.

**Critical distinction:** the files under `autobuild/templates/` (`autobuild/templates/CLAUDE.md`,
`autobuild/templates/GOAL.md`, `autobuild/templates/tasks/`, `autobuild/templates/config.yml`)
are *scaffolding copied into a user's target project* by `autobuild init` (read out of the
installed package via `importlib.resources`). They are **not** instructions for developing
autobuild itself. In particular, `autobuild/templates/CLAUDE.md` is the **runtime contract a
spawned session obeys** (plan → review → implement → write `result.json`). Editing it changes
how *target-project sessions* behave, not how you work on this codebase. This root file is the
only one that governs work on the harness.

## Running and developing

A small, stdlib-first Python package managed with **uv**. The only runtime dependency is
**PyYAML**; the dev group adds **pytest**. Requires Python 3.11+ (uv provisions the interpreter).

```bash
uv sync                          # create the venv + install deps (incl. pytest)
uv run pytest                    # run the full suite (unit + a token-free e2e loop)

# Exercise the CLI against a throwaway target project (never run init in this repo):
uv run autobuild --help
mkdir -p /tmp/ab-scratch && cd /tmp/ab-scratch && git init
uv run autobuild init            # lay down GOAL.md, CLAUDE.md, tasks/, .autobuild/config.yml
uv run autobuild run             # outer loop: schedule -> spawn -> reap, until drained
uv run autobuild status          # task + session state
```

`uv tool install .` installs the `autobuild` command globally (entry point
`autobuild.cli:main`). The pytest suite is the safety net — there is no separate linter
configured. The e2e tests (`tests/test_e2e.py`) drive the whole loop against a **stub `claude`
on `PATH`** (`tests/fixtures/claude`, wired up in `tests/conftest.py`), so they spend no tokens.

## Architecture

`autobuild/cli.py` is the thin dispatcher: `argparse` maps a subcommand to a function (in
`cli.py` or `loop.py`). All commands assume they run from the **target project root**;
`Paths.from_cwd()` resolves `root = Path.cwd()` and derives `.autobuild/`, `tasks/`, `GOAL.md`,
etc. from it (the typed replacement for the bash `PROJECT_ROOT` and its path vars).

Config and task frontmatter are parsed with **PyYAML**, and the JSON sentinels
(`result.json`, `meta.json`, `reaped.json`) with the stdlib `json` module — no `grep`/`sed`
or `yq`. The three I/O layers everything else builds on:

- `load_config(config_file)` → a frozen `Config` (`config.py`); flat schema, all keys optional
  with the template defaults.
- `read_task` / `parse_frontmatter` / `set_status(path, status)` (`tasks.py`) — frontmatter
  read plus a **surgical, atomic** status rewrite (single-line regex; temp file + `os.replace`)
  that preserves comments and never reserializes a human task file.
- `Paths` (`paths.py`) — the one place every `.autobuild/` location is defined.

Module responsibilities:

- **`autobuild/cli.py`** — `argparse` dispatch + the `autobuild` entry point. `ab_init` copies
  the packaged templates into the project; `require_init` guards the other commands.
- **`autobuild/config.py`** — load `.autobuild/config.yml` into a typed, frozen `Config`
  (all keys optional; defaults match the template).
- **`autobuild/tasks.py`** — the `Task` model, frontmatter I/O, `set_status`, and the follow-up
  id allocator (`next_task_id` / `create_task_file`).
- **`autobuild/scheduler.py`** — `runnable_tasks` (status `todo` + every `depends_on` id is
  `done`, sorted by `(priority, id)`) and `claim_tasks(n)`, which atomically flips up to N tasks
  `todo→claimed` under an exclusive `fcntl.flock` on `.autobuild/backlog.lock` so parallel runs
  never double-claim. The same `backlog_lock` is reused when allocating follow-up ids.
- **`autobuild/worktree.py`** — `make_worktree` / `remove_worktree` / `prune_worktrees`: one
  isolated checkout + branch (`autobuild/<task-id>`) per session, forked from `base_branch`
  (the branch is reused on a retry).
- **`autobuild/session.py`** — `spawn_session`: builds the session dir + worktree + `meta.json`,
  sets the task `in-progress`, and launches a **fresh `claude -p`** via `subprocess.Popen`. It
  returns an in-memory `RunningSession` handle the loop supervises with `proc.poll()` — this
  replaces the bash `.running` PID file.
- **`autobuild/loop.py`** — the outer Ralph-style loop (`run`), the reaper
  (`reap_all` / `reap_session` / `reap_stalled`), `reconcile` (startup crash recovery),
  `integrate`, `file_followups`, `status`, and `clean`.
- **`autobuild/paths.py`** — the frozen `Paths` dataclass.

### The session lifecycle / sentinel protocol (the core data flow)

This is the part that requires reading multiple files to understand:

1. `claim_tasks` flips a runnable `todo` task → `claimed` under the backlog lock.
2. `spawn_session` creates `.autobuild/sessions/<sid>/` + a worktree, writes `meta.json`, sets
   the task `in-progress`, and launches a **fresh `claude -p` process** (the Ralph property: no
   context carries between sessions — all state is files + git). It returns a `RunningSession`
   the loop tracks in memory.
3. The session follows `autobuild/templates/CLAUDE.md`'s contract and ends by writing
   `.autobuild/sessions/<sid>/result.json` with `status: COMPLETE | BLOCKED | NEEDS_HUMAN`.
4. `reap_session` reads that sentinel and acts. For `COMPLETE` it **integrates first**
   (`integrate` → `pr` via `gh` / `auto-merge` / `branch`) and only then `set_status`es the task
   `done` — so a failed auto-merge leaves the task `blocked`, not falsely `done`. It files any
   `followups[]` as new `tasks/*.md` (ids allocated under the backlog lock), removes the
   worktree, and drops a `reaped.json` marker. That marker makes the reaper **idempotent**: a
   second pass over the same session is a no-op.
5. `reap_stalled` is one safety net — a `RunningSession` whose process exited without writing
   `result.json` gets a synthetic `BLOCKED` sentinel. `reconcile` is the other: at startup it
   returns orphaned `claimed` tasks to `todo` and gives orphaned `in-progress` sessions a
   `BLOCKED` sentinel, so a killed `run` resumes cleanly from files alone (no PID file needed).

The loop terminates when nothing is running **and** nothing is runnable (backlog drained, or
settled behind blocked/unsatisfiable deps), or when `max_iterations` trips. When a pass makes no
progress it blocks on the next live session finishing instead of busy-sleeping.

### Task status state machine

`todo` → `claimed` → `in-progress` → terminal (`done` | `blocked`). `done` and `blocked` are the
only terminal states (`is_terminal` / the `TERMINAL` set); everything else counts as in-flight,
which is what keeps the loop running. The full vocabulary lives in the template task file:
`todo | claimed | in-progress | review | done | blocked`.

## Conventions and gotchas

- **State is disposable and crash-safe.** Everything under `.autobuild/` (sessions, worktrees,
  `backlog.lock`) can be deleted and rebuilds from `tasks/` + git. There is no `.running` PID
  file anymore — running sessions live in memory, and `reconcile` recovers orphans on the next
  `run`. Re-running `autobuild run` after a kill resumes from the files; design changes to honor
  this idempotency.
- **Keep the reaper idempotent.** Integration happens *before* the task is marked `done`, and
  the `reaped.json` marker guards against double-integration / double follow-up filing. Preserve
  both properties when touching `loop.py`.
- **The backlog lock is `fcntl.flock` on `.autobuild/backlog.lock`** (auto-released if the holder
  dies — no stale lockdir to strand). Any mutation that must be serialized across parallel runs
  (claiming tasks, allocating follow-up ids) goes through `backlog_lock`.
- **Status writes are surgical and atomic.** `set_status` rewrites only the `status:` line via
  regex + `os.replace`; never round-trip a human task file through `yaml.dump` (it would strip
  comments and reorder keys). Follow-up task files are generated, so those *are* serialized with
  `yaml.safe_dump`.
- Naming: CLI subcommands map to `run` / `status` / `reap_all` / `clean` in `loop.py` (plus
  `ab_init` in `cli.py`); module-internal helpers are prefixed `_`.
- Minimal dependencies by design: **PyYAML** is the only runtime dep; everything else (process
  orchestration via `subprocess`, JSON sentinels, the claim lock) is the standard library. Match
  that style unless you're deliberately adding a dependency.
- Project status is v0/MVP. `# TODO` and audit-note markers in the source flag the parts still
  wanting hardening (PR creation, richer check reporting, integration edge cases).
