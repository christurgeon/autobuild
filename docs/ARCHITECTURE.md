# Architecture

How autobuild is put together, and what a run actually does. For day-to-day use start with
the [README](../README.md); this is the deeper tour.

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

## Lineage

autobuild is the glue between three ideas that already work:

- **[Ralph loop](https://github.com/ghuntley/how-to-ralph-wiggum)** — run an agent
  with a *fresh context* every iteration; keep all state in files + git, never in
  the context window.
- **[Karpathy's autoresearch](https://github.com/karpathy/autoresearch)** — give the
  agent a compact area of control, explicit constraints, and a stopping criterion.
- **[Backlog.md](https://github.com/MrLesk/Backlog.md)** — one markdown file per task,
  with status and acceptance criteria, readable by humans and agents alike.

It ties them together and adds **worktree fan-out** so N agents run in parallel without
colliding, plus **per-session state** under `.autobuild/` that a single supervisor verifies
before anything lands on your branch.

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
