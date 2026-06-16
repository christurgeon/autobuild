# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo **is the `autobuild` tool** — a bash harness that drains a backlog of tasks
toward a `GOAL.md` by spawning fresh, isolated Claude Code sessions (`claude -p`) in
parallel git worktrees. Read `README.md` for the full mental model.

**Critical distinction:** the files under `templates/` (including `templates/CLAUDE.md`,
`templates/GOAL.md`, `templates/tasks/`, `templates/.autobuild/config.yml`) are
*scaffolding copied into a user's target project* by `autobuild init`. They are **not**
instructions for developing autobuild itself. In particular, `templates/CLAUDE.md` is the
**runtime contract a spawned session obeys** (plan → review → implement → write
`result.json`). Editing it changes how *target-project sessions* behave, not how you work
on this codebase. This root file is the only one that governs work on the harness.

## Running and developing

Pure bash; no build step, no package manager, no test framework. To use/develop the tool:

```bash
export PATH="$PWD/bin:$PATH"   # put the entrypoint on PATH

# Exercise it against a throwaway target project (never run init in this repo):
mkdir -p /tmp/ab-scratch && cd /tmp/ab-scratch && git init
autobuild init                 # lay down GOAL.md, CLAUDE.md, tasks/, .autobuild/config.yml
autobuild run                  # outer loop: schedule -> spawn -> reap, until drained
autobuild status               # task + session state
```

There is no automated test suite. Lint bash with **shellcheck** (the source uses
`# shellcheck source=/dev/null` directives): `shellcheck bin/autobuild lib/*.sh`.

## Architecture

`bin/autobuild` is a thin dispatcher: it `source`s every `lib/*.sh` module (order in the
file matters — `common.sh` first, it defines paths/logging/parsers used by the rest) and
maps a subcommand to an `ab_*` function. All commands assume they run from the **target
project root**; `common.sh` resolves `PROJECT_ROOT="$(pwd)"` and derives `.autobuild/`,
`tasks/`, etc. from it.

Module responsibilities:

- **`lib/common.sh`** — the foundation: logging (`log`/`ok`/`warn`/`err`), path vars, the
  minimal YAML/frontmatter layer, and `ab_init`. Any code that reads config or task
  frontmatter goes through these four parsers — there is no `yq` dependency by design:
  - `cfg KEY [default]` / `cfg_list KEY` — read scalar / block-list values from `config.yml`
  - `fm FILE KEY` — read a scalar from a task file's `--- frontmatter ---`
  - `set_status FILE STATUS` — rewrite a task's `status:` line in place
- **`lib/scheduler.sh`** — `runnable_tasks` (status `todo` + all `depends_on` are `done`,
  sorted by priority ascending) and `claim_tasks N`, which atomically flips up to N tasks
  `todo→claimed` under a `mkdir`-based lock (`.autobuild/backlog.lock`) so parallel runs
  never double-claim.
- **`lib/worktree.sh`** — `make_worktree`/`remove_worktree`: one isolated checkout + branch
  (`autobuild/<task-id>`) per session, forked from `base_branch`.
- **`lib/session.sh`** — `spawn_session`: builds the session dir + worktree + `meta.json`,
  then backgrounds `claude -p` with a generated prompt. Appends `sid:pid` to
  `.autobuild/.running`. `running_count` counts live PIDs from that file.
- **`lib/loop.sh`** — the outer Ralph-style loop (`ab_run`) plus the reaper, `ab_status`,
  and `ab_clean`. `ab_run` repeats: `reap_stalled` → `ab_reap_quiet` → claim up to
  `max_parallel - active` tasks → spawn → check termination → `sleep 2`.

### The session lifecycle / sentinel protocol (the core data flow)

This is the part that requires reading multiple files to understand:

1. Scheduler claims a `todo` task → `claimed`.
2. `spawn_session` creates `.autobuild/sessions/<sid>/` + a worktree, sets the task
   `in-progress`, and launches a **fresh `claude -p` process** (the Ralph property: no
   context carries between sessions — all state is files + git).
3. The session follows `templates/CLAUDE.md`'s contract and ends by writing
   `.autobuild/sessions/<sid>/result.json` with `status: COMPLETE | BLOCKED | NEEDS_HUMAN`.
4. The reaper (`_reap_session`) reads that sentinel and acts: `set_status` the task
   (`done`/`blocked`), `_integrate` the branch per `integration` config (`pr` via `gh` /
   `auto-merge` / `branch`), `_file_followups` (turn `followups[]` into new `tasks/*.md`),
   remove the worktree, and drop the session from `.running`.
5. `reap_stalled` is the safety net: a session whose PID is dead but left no `result.json`
   gets a synthetic `BLOCKED` sentinel, so a crashed agent can't stall the loop forever.

### Task status state machine

`todo` → `claimed` → `in-progress` → terminal (`done` | `blocked`). `done` and `blocked`
are the only terminal states; `pending_count` treats everything else as still in-flight,
which is what keeps the loop running. The full vocabulary lives in the template task file:
`todo | claimed | in-progress | review | done | blocked`.

## Conventions and gotchas

- **State is disposable and crash-safe.** Everything under `.autobuild/` (sessions,
  worktrees, `.running`, `backlog.lock`) can be deleted and rebuilds from `tasks/` + git.
  Re-running `autobuild run` after a kill resumes from the files; design changes to honor
  this idempotency.
- **All scripts use `set -euo pipefail`.** Remember `if`/`&&`/`||` suppress `-e`, so check
  git exit codes explicitly (see the note in `worktree.sh`).
- Naming: public subcommands are `ab_*`; internal helpers are `_*`.
- JSON (`result.json`, `meta.json`) is currently parsed with `grep`/`sed`, and the YAML
  parsers handle only flat scalars/block-lists. These are marked `# TODO` for hardening
  (`yq`/a real JSON parser). Match the existing minimal-dependency style unless you're
  deliberately taking on that TODO.
- Project status is v0/MVP. `# TODO` markers flag the parts wanting hardening (YAML edge
  cases, PR creation, richer check reporting).
