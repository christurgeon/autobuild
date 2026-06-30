---
name: autobuild-plan-backlog
description: Use when setting up an autobuild backlog and you need to turn GOAL.md into tasks — produce a dependency-ordered set of right-sized tasks/*.md files with correct frontmatter (id, priority, depends_on) and checkable acceptance criteria. Triggers on "plan the backlog", "break the goal into tasks", "create the tasks", "decompose the work for autobuild".
---

# autobuild: plan the backlog

> If you were handed exactly one task via a session `meta.json`, you are a spawned
> autobuild session — this skill does NOT apply. Follow your assigned task instead.

You are decomposing a project's `GOAL.md` into the `tasks/*.md` backlog autobuild drains.
The quality of this decomposition is the single biggest lever on whether a run succeeds:
get the task sizing and the dependency graph right and the loop fans out cleanly.

## Prerequisites

`GOAL.md` must exist and be filled in. If it's missing or still the template, stop and
point the user at the **autobuild-author-goal** skill first.

## Process

1. **Read `GOAL.md`** — mission, scale & operational assumptions, definition of done,
   constraints, non-goals. Everything you plan must serve the definition of done, fit the
   scale envelope, and stay inside the "MAY change" boundary.
2. **Explore the codebase** — structure, existing patterns, and the build/test commands
   (you'll reference them in acceptance criteria). Read `.autobuild/config.yml` to see the
   project's actual configured `checks:`.
3. **Handle what `autobuild init` seeded, then allocate ids.** Scan `tasks/*.md`:
   - **If the only file is the seeded `task-001-example.md` placeholder** (titled "Example
     task — replace me"), **delete it** and start your real backlog at `task-001` — do not
     leave it, or the scheduler will run a meaningless task.
   - **If real, human-authored tasks already exist,** leave them and continue the sequence
     from the highest existing `id`; never reuse an id.
4. **Draft the backlog.** Each task is one session of work: small enough to finish in a
   single fresh session, large enough to be worth a commit. For each task produce:
   - `id` (`task-NNN`, sequential), `title`, `priority` (lower = higher), `depends_on`
     (only real prerequisites — keep the graph a DAG).
   - **Goal** — the single self-contained piece of work.
   - **Acceptance criteria** — concrete and checkable, always including a line for the
     project's configured checks (read them from `.autobuild/config.yml`, e.g. "`ruff check
     .` and `pytest` pass" — the real commands, not the words "typecheck/lint/test"). If
     `config.yml` still has the seeded `echo 'replace me…'` placeholder, fall back to the
     commands named in `GOAL.md` / CI / README and suggest the user run **autobuild-configure**
     to make them real.
   - **Notes** — context, gotchas, links.
   - **Sizing** — roughly one module or one endpoint per task. If a task would touch 3+
     unrelated areas, or needs two distinct test suites, split it. Assign a cross-cutting
     criterion (e.g. an end-to-end test spanning several pieces) to a single owning task
     that `depends_on` all the pieces it exercises.
   - **Scale fit** — size the work to `GOAL.md`'s envelope. Small/simple scale → don't
     create infrastructure tasks (caching, queues, sharding, autoscaling) the goal doesn't
     call for; prefer the simplest thing that meets the DoD. Larger scale → give
     load/performance, observability, and capacity each an owning task. Don't invent scaling
     work the envelope doesn't justify, and don't omit it when the envelope demands it.
5. **Self-lint before writing.** Verify and fix:
   - no dependency cycles; every `depends_on` id exists;
   - no task obviously too big for one session (split it) or untestable (vague criteria →
     make them concrete);
   - priorities are sane and the ordering matches the dependency graph;
   - **coverage** — every Definition-of-Done bullet in `GOAL.md` is satisfied by at least
     one task, and no task falls outside the goal. This is what tells you the backlog
     actually achieves the goal.
   - **scale fit** — no task over- or under-builds relative to the scale envelope: no
     gold-plated infra the magnitude doesn't justify, no missing capacity/perf work it
     demands.
   Report what you found and fixed.
6. **Present, then write.** Show the proposed backlog as a compact list — `id · title ·
   depends_on · one-line goal` — and get approval (if non-interactive, write it and flag
   your sizing/dependency judgement calls for review). Then write each
   `tasks/task-NNN-*.md` following the frontmatter + Goal + Acceptance criteria + Notes
   structure. **Never overwrite a real, human-authored task file** without showing an
   inline diff first (deleting the seeded `task-001-example.md` placeholder in step 3 is
   the intended exception, not an overwrite).

## Next step

Suggest **autobuild-configure** (if `.autobuild/config.yml` still has placeholder checks)
and then `autobuild run`.
