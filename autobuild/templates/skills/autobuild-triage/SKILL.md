---
name: autobuild-triage
description: Use after an autobuild run when tasks are blocked, timed out, or the backlog settled and you need to understand why and what to do — diagnose from result.json sentinels, session logs, and preserved branches, then propose fixes, re-queues, or merges. Triggers on "why did this task block", "what's stuck", "triage the run", "help me unblock", "the backlog settled", "review the autobuild PRs".
---

# autobuild: triage a settled run

> If you were handed exactly one task via a session `meta.json`, you are a spawned
> autobuild session — this skill does NOT apply. Follow your assigned task instead.

You are helping a human make sense of a finished or settled autobuild run: which tasks
are `blocked` / `timeout` / waiting, *why*, and what to do next. All the evidence lives
in files and git — read it before proposing anything.

## Process

1. **Get the state.** Run `autobuild status` for every task's status, in-flight/settled
   sessions, and any stuck-dependency reasons. The CLI may not be a bare `autobuild` on
   PATH — try, in order: `autobuild status`, `uv run autobuild status` (from the project
   root), or `uv run --project <autobuild-checkout> autobuild status`. **If you must fall
   back to reading `tasks/*.md` directly, you lose the STUCK section** — recompute it
   yourself by checking each task's `depends_on` against the others' statuses.
2. **Investigate each non-`done` task that should have progressed.** For a `blocked` /
   `timeout` / unexpectedly-stuck task:
   - Read its session dir under `.autobuild/sessions/<id>/`. **`result.json` may be ABSENT
     — that's the normal shape of a `timeout` or a crashed/killed session, not corruption.**
     When it's missing (or `plan.md` is missing — also fine), reconstruct what happened from
     `meta.json` + `progress.log` + the preserved branch instead.
   - Check for `<id>/leak.json` — if present, this session committed onto `base_branch`
     instead of its worktree (a worktree escape, or a concurrent commit to base). See the
     base-leak cause below; this is the one failure that can stop the whole run.
   - Inspect the preserved branch `autobuild/<task-id>`: `git log` and `git diff` against
     `base_branch`. A blocked-before-implementing task often has 0 commits; a timed-out one
     may have WIP commits that wouldn't pass checks.
3. **Classify the cause** — one of:
   - **ambiguous / under-specified task** (BLOCKED with a "spec unclear" summary),
   - **dependency not done** — but **`status` STUCK names only ONE blocker per task.** Read
     the task's `depends_on` and check every listed dependency's status; a task can be
     blocked by several (e.g. one `blocked` + one `timeout`) while STUCK surfaces just one.
   - **checks failed** (COMPLETE but the reaper's verify re-run blocked it — see
     `<session>/checks.log` if present),
   - **ran out of time** (`timeout`: deadline hit. Within `timeout_max_retries` the
     harness deletes the partial branch itself and the automatic retry forks again from
     base. A terminal `timeout` keeps its `autobuild/<id>` branch for you to inspect;
     `progress.log` usually shows what ate the budget).
   - **needs a human decision** (NEEDS_HUMAN).
   - **broken task file** — STUCK says `invalid-task-file: <file> (<error>)` (the file starts
     with `---` but its frontmatter does not parse, does not close, is not a mapping, or has
     no `status:` line) or `invalid-dependency: <id>` (a dependency's file is invalid). Fix
     the named line in the file. `duplicate-id: <id> (<a.md>, <b.md>)` or
     `duplicate-dependency: <id>` means two files declare the same `id`; give one a new id
     (and fix any `depends_on` that meant it). A file with no frontmatter is ignored, not
     invalid; `autobuild doctor` lists both. If a session finished while its file was broken
     or duplicated, its reap was deferred; it finishes on the next `run` or `autobuild reap`
     after the fix.
   - **base leak** (`<id>/leak.json` present) — the session committed onto `base_branch`
     instead of its own branch; `leak.json` lists the offending `commits`. In `auto-merge`
     this halts the run (`BaseBranchLeak`, exit 2) and re-fires every run until base is
     clean; in `pr`/`branch` it only blocks that task. Recover: move the leaked commit(s)
     onto a branch and reset `base_branch` back (`git branch salvage <sha>; git reset --hard
     <sha-before-leak>`), then re-queue the task (`status: todo`). If it was a *concurrent*
     human/CI commit (not a real escape), just keep it and re-queue the task.
4. **Propose a concrete next action per task** — tie it to the cause: edit the task to
   clarify/scope it down and re-queue (set `status: todo`), split it into follow-ups, fix the config (raise `task_timeout_seconds`, correct a
   check command), or escalate to the human. **When a dependent is blocked by multiple
   upstream tasks, every one must reach `done` before it can run** — propose fixes for all
   of them, not just the one STUCK named. Note: a task left terminal `timeout` has already
   spent its `timeout_max_retries` budget, so it won't auto-retry — re-queueing it is a
   manual `status: todo` flip.

   **A manual re-queue reuses the branch.** Setting a `blocked` or terminal `timeout` task
   back to `status: todo` **reuses the existing `autobuild/<id>` branch** and its earlier
   commits. The branch is not rebased onto the current base (done dependencies are still
   merged in). That helps when the earlier work is worth keeping. For a fresh start, delete
   the branch before the next run: `git branch -D autobuild/<id>` (run `git worktree prune`
   first if git says the branch is checked out). The next `run` then forks again from
   `base_branch`.
5. **PR review for dependency chains (pr mode) — only if applicable.** *Skip this unless
   there are `done` tasks whose PRs form a dependency chain.* For a chain A → B → C,
   explain the stacked-history limitation — each PR contains its dependencies' commits —
   and recommend merging in **dependency order**, or onto one integration branch.

## Guardrail

**Never mutate the backlog silently.** Present the triage and the proposed changes first;
apply task edits or status flips only after the user approves. Re-queueing is just setting
`status:` back to `todo`; decide first whether to keep or delete `autobuild/<task-id>`.
