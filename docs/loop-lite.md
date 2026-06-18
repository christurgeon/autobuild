# Lite mode: prototyping the inner loop with `/loop`

Before wiring up worktrees and parallelism, you can feel out the autobuild *inner
loop* in ~30 seconds using nothing but Claude Code's built-in `/loop` skill. This is
great for trying the plan→review→implement contract on a small backlog.

## What this does (and its limits)

`/loop` re-runs a prompt on an interval **inside one Claude session**. That means:

- ✅ Zero setup — no scripts, no worktrees.
- ✅ Good for a single-stream backlog you want to watch drain.
- ⚠️ **Context accumulates** across iterations (no fresh context per task) — fine for
  a handful of tasks, but it will rot on a long build. That's the whole reason the
  full `autobuild` harness spawns a fresh `claude -p` process per task instead.
- ⚠️ **No parallelism** — one session can't fan out to multiple worktrees.

Use `/loop` to prototype; graduate to `autobuild run` when you want fresh context
and parallel worktrees.

## Setup

1. In your project, create `GOAL.md`, `CLAUDE.md`, and a `tasks/` folder (or run
   `autobuild init` just to get the templates).
2. Start Claude Code in the project and run:

   ```
   /loop 3m action the next autobuild task
   ```

   Pair it with a prompt like the one below (save as `tasks/PROMPT.md` and reference it):

   ```
   Read GOAL.md and CLAUDE.md. Pick the highest-priority task in tasks/ whose
   status is `todo` and whose dependencies are all `done`. Follow
   plan -> review -> implement for that ONE task. Run the project's checks; if they
   pass, commit and set the task's status to `done`. Append a line to progress.log.
   If every task is already `done`, reply with exactly: COMPLETE (and stop looping).
   ```

3. Stop the loop when you see `COMPLETE`, or with the `/loop` controls.

## When to switch to the full harness

Move to `autobuild run` once any of these is true:

- You have more than ~5–10 tasks (context rot starts to bite).
- You want multiple tasks worked in parallel.
- You want each task isolated in its own branch/worktree with PRs.
