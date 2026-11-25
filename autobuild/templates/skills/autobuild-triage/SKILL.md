---
name: autobuild-triage
description: Use after an autobuild run when tasks are blocked, timed out, or the backlog settled and you need to understand why and what to do — diagnose from result.json sentinels, session logs, and preserved branches, then propose fixes, re-queues, or merges. Also use after a force-killed or crashed run to find and fix the states a kill during a reap can leave. Triggers on "why did this task block", "what's stuck", "triage the run", "help me unblock", "the backlog settled", "review the autobuild PRs", "the run was killed", "autobuild crashed", "main is broken after autobuild".
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

## Recovering after a killed run

Use this when a `run` was force-killed (SIGKILL, crash, power loss). A kill during a reap
can leave the states below, and the harness does not detect them. No file records that a
kill happened, so check each state. `<base>` is `base_branch` in `.autobuild/config.yml`.

1. **Stop, then settle.** Confirm that no run is active: `pgrep -fl "autobuild run"`
   prints nothing. Then run `autobuild reap` (same fallbacks as `status`). It recovers
   orphans and reaps finished sessions, as the next `run` would. List sessions still
   unreaped: `for d in .autobuild/sessions/*/; do [ -f "$d/reaped.json" ] || echo "$d"; done`.
   An unreaped session usually has a broken task file (see above); fix it and reap again.
   **Do this before any commit to `<base>`.** A reap compares `<base>` to the session's
   `base_sha`, so a revert or fix commit looks like a base leak to an unreaped session.
2. **Red base (`auto-merge` only).** Run each `checks:` command from the config in the
   main checkout on `<base>`. If one fails, base is red: every merge after it failed the
   post-merge checks, was undone, and its task was blocked. Find the first bad commit.
   `git log --first-parent --format='%h %s' <base>` lists base's history; harness merges
   have the subject `autobuild: merge <task-id>`. Test a candidate in a scratch worktree:
   `git worktree add --detach <dir> <sha>`, run the checks there, then
   `git worktree remove <dir>`. The cause can also be a human commit or the environment
   (a missing tool); fix that instead. Undo a bad harness merge with
   `git revert -m 1 <merge-sha>`. It adds a commit and keeps the uncommitted `tasks/`
   status lines. **Never `git reset --hard` here** — it discards those lines. Then set
   that task to `status: todo`. A successful auto-merge deleted its branch, so the retry
   forks from the fixed base.
3. **Tasks blocked by a broken merge.** The post-merge gate writes
   `post-merge-checks.log` in the session dir. Find them with
   `ls .autobuild/sessions/*/post-merge-checks.log`. The `task` field in that session's
   `meta.json` names the task. Keep only tasks now `blocked` (`reaped.json` has
   `"integrated": false`). Re-queue them (`status: todo`) only after base passes the
   checks. Their branch is kept, so the retry reuses that work. If base passes but the log
   shows the task's own change failing, it is merge skew: treat it as "checks failed".
4. **Follow-ups filed twice.** A kill after a COMPLETE session filed its follow-ups, but
   before `reaped.json`, makes the next reap file them again under new ids. Follow-up
   files have no parent field. Heuristic: find repeated titles with
   `grep -h '^title:' tasks/*.md | sort | uniq -d`, and match them against the session's
   `result.json` `followups[]`. Generated files contain "filed automatically by autobuild
   as a follow-up". That session's `reaped.json` `followups` lists only the last filing's
   ids. Propose to keep one copy per title. Before deleting a copy, run
   `grep -l '<id>' tasks/*.md` and point any `depends_on` at the kept id. Two real
   follow-ups can share a title, so confirm each pair with the human.
5. **A `done` task with nothing merged.** A COMPLETE session with an empty branch passes
   the checks and is marked `done`. Heuristic, per mode: in `auto-merge`,
   `git log --first-parent --format=%h --grep='^autobuild: merge <id>$' <base>` prints
   nothing; in `pr`/`branch`, `git log --first-parent --no-merges --oneline
   <base>..autobuild/<id>` prints nothing (dependency merges are excluded). A human may
   also have set `done` by hand. Ask before re-queueing.
6. **A finished task flipped to `blocked`.** A kill after the reap removed the worktree,
   but before `reaped.json`, fails the next reap's verification. Its `checks.log` shows
   `command: (pre-flight)` and "is missing; cannot verify checks". The work may already be
   integrated; check with the commands in item 5. If it is, propose `status: done`.

## Guardrail

**Never mutate the backlog silently.** Present the triage and the proposed changes first;
apply task edits or status flips only after the user approves. After a killed run,
`autobuild reap`, `git revert`, and deleting a task file are changes too: propose each
one first. Re-queueing is just setting `status:` back to `todo`; decide first whether to
keep or delete `autobuild/<task-id>`.
