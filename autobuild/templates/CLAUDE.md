# CLAUDE.md — the autobuild session contract

You are a single **autobuild** session. A scheduler has assigned you **exactly one
task** and dropped you into an **isolated git worktree** on your own branch. Your
entire memory of this project lives in files and git history — nothing carries over
between sessions, so write down what matters.

Read `GOAL.md` for the project's north star and constraints, and your assigned task
file. Both — along with this contract — are **staged into your session directory** and
their paths are given in your prompt; read them there.

**Stay inside your worktree.** Your worktree (its path is in your prompt) is your whole
repository: do every file read, edit, shell command, and `git` operation inside it, on
the branch already checked out. Never read, edit, or commit any path outside your
worktree and your session directory — in particular never touch the main project
checkout. Committing or editing outside your worktree corrupts shared state; the harness
detects it and **rejects your work**.

## The workflow: plan → review → implement

Follow these phases **in order**. Do not skip straight to code.

### 1. Plan
- Restate the task's goal and acceptance criteria in your own words.
- Write a concrete, step-by-step plan to `<session-dir>/plan.md`.
- List the files you expect to touch. Stay inside the "Agents MAY change" boundary
  from `GOAL.md`.
- Fit `GOAL.md`'s "Scale & operational assumptions" envelope — build for the stated
  magnitude. Don't introduce scaling infrastructure (caches, queues, sharding, extra
  services) it doesn't call for, and don't under-build below it.

### 2. Review
- Critique your own plan against the acceptance criteria and `GOAL.md` constraints.
- Ask: does this fully satisfy the task? Does it violate any non-goal or constraint?
  Is it the smallest change that works?
- Revise `plan.md` if the review surfaced problems. Only proceed when the plan holds up.

### 3. Implement
- Execute the plan. Make the smallest coherent change that satisfies the task.
- Run every command under `checks:` in `.autobuild/config.yml`. **All must pass.**
- If checks pass: `git add -A && git commit` **from inside your worktree** with a clear
  message referencing the task id (this commits onto your branch, never onto the base).
- Append a short narration of what you did to `<session-dir>/progress.log`.

## Finishing: write the sentinel

Always end by writing `<session-dir>/result.json`:

```json
{
  "task": "task-001",
  "status": "COMPLETE",        // COMPLETE | BLOCKED | NEEDS_HUMAN
  "summary": "one-line description of what changed",
  "commit": "<sha or empty>",
  "followups": []               // optional: new tasks to file, see below
}
```

**Write it atomically.** The reaper may read `result.json` at any moment, so never
write it in place — a half-written file looks corrupt and blocks your task. Write the
full JSON to a temp file in the session dir, then rename it over `result.json` (an
atomic replace on the same filesystem):

```bash
printf '%s' "$JSON" > <session-dir>/result.json.tmp && mv -f <session-dir>/result.json.tmp <session-dir>/result.json
```

- **COMPLETE** — task done, checks pass, work committed. The reaper re-runs the
  `checks` against your worktree before integrating, so do **not** write COMPLETE on a
  tree where they fail — it will be overridden to `blocked` and left unmerged.
- **BLOCKED** — you cannot proceed (missing dependency, ambiguous spec). Put the reason
  in `summary`. The reaper marks the task `blocked` and will not retry it blindly.
- **NEEDS_HUMAN** — a decision only the human should make. Explain in `summary`.

### Filing follow-up tasks ("fallups")
If you discover work that belongs in its own task, add it to `followups` as objects
`{ "title": "...", "priority": 2, "notes": "..." }`. The reaper will create
`tasks/task-NNN-*.md` files for them. Do **not** silently expand your own scope —
file a follow-up instead.

## Rules
- One task per session. Do not start work the scheduler didn't assign you.
- Never edit files outside the "Agents MAY change" boundary.
- Prefer leaving the repo in a working state over leaving it half-done. If you can't
  finish cleanly, write `BLOCKED` rather than committing broken code.
