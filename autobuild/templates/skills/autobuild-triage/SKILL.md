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
   - Read its session dir under `.autobuild/sessions/<id>/`. **`result.json` may be
     ABSENT — that is the normal shape of a `timeout` or a crashed/killed session, not
     corruption.** When it's missing (or `plan.md` is missing — also fine), reconstruct
     what happened from `meta.json` + `progress.log` + the preserved branch instead.
   - Inspect the **preserved branch** `autobuild/<task-id>`: `git log` and `git diff`
     against `base_branch`. A blocked-before-implementing task often has **0 commits**; a
     timed-out one may have WIP commits that wouldn't pass checks.
3. **Classify the cause** — one of:
   - **ambiguous / under-specified task** (BLOCKED with a "spec unclear" summary),
   - **dependency not done** — but **`status` STUCK names only ONE blocker per task.**
     Always read the task's `depends_on` and check **every** listed dependency's status;
     a task can be blocked by several (e.g. one `blocked` + one `timeout`) while STUCK
     surfaces just one.
   - **checks failed** (COMPLETE but the reaper's verify re-run blocked it — see
     `<session>/checks.log` if present),
   - **ran out of time** (`timeout`: deadline hit. The partial `autobuild/<id>` branch is
     **preserved** until a retry re-forks from base — on a settled-without-retry run it's
     still there to inspect; `progress.log` usually shows what ate the budget).
   - **needs a human decision** (NEEDS_HUMAN).
4. **Propose a concrete next action per task** — tie it to the cause: edit the task to
   clarify/scope it down and **re-queue** (set `status: todo`; the next `run` re-forks from
   base), split it into follow-ups, fix the config (raise `task_timeout_seconds`, correct a
   check command), or escalate to the human. **When a dependent is blocked by multiple
   upstream tasks, every one must reach `done` before it can run — propose fixes for all of
   them, not just the one STUCK named.** Note: a task left terminal `timeout` has already
   spent its `timeout_max_retries` budget, so it will **not** auto-retry — re-queueing it is
   a manual `status: todo` flip.
5. **PR review for dependency chains (pr mode) — only if applicable.** *Skip this unless
   there are `done` tasks whose PRs form a dependency chain.* For a chain A → B → C,
   explain the stacked-history limitation — each PR contains its dependencies' commits —
   and recommend merging in **dependency order**, or onto one integration branch.

## Guardrail

**Never mutate the backlog silently.** Present the triage and the proposed changes first;
apply task edits or status flips only after the user approves. Re-queueing is just setting
a task's `status:` back to `todo` — the harness handles the re-fork.
