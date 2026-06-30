---
name: autobuild-author-goal
description: Use when setting up an autobuild project and you need to write or improve GOAL.md — the stable north star (mission, scale & operational assumptions, project-level definition of done, constraints / area-of-control, non-goals) that every autobuild session reads each iteration. Triggers on "write the GOAL", "create/generate GOAL.md", "set up autobuild's goal", "set the scale/assumptions", "what should my GOAL.md say".
---

# autobuild: author GOAL.md

> If you were handed exactly one task via a session `meta.json`, you are a spawned
> autobuild session — this skill does NOT apply. Follow your assigned task instead.

You are helping a human author `GOAL.md` for an autobuild project. `GOAL.md` is the
**north star**: the *why* and the *definition of done for the whole project*, read by
every session on every iteration. It is **not** a task list (tasks live in `tasks/`)
and it should rarely change. Keep it tight, stable, and testable.

## Process

1. **Locate and read the existing file.** `GOAL.md` lives at the project root. If it
   already has real content (beyond the template's `<!-- comments -->`), treat this as a
   **revision**: read it, and propose edits as an inline old→new diff rather than
   overwriting.
2. **Understand the project.** Skim the README, the package manifest, and the top-level
   directory layout so your questions are grounded in what's actually here. Anything the
   README/manifest already answers, don't ask about — confirm it.
3. **Interview the user — efficiently.** Fill the sections below; prefer multiple-choice.
   Batch trivially-related questions (e.g. the whole API/behavior contract in one block:
   "here's the obvious contract — tweak anything?"), skip what the docs already answer, and
   aim for **≤4 exchanges on a small single-service project**. Don't run a long
   one-question-at-a-time interrogation on a small project.
   - **Mission** — one or two sentences: what are we building, and for whom?
   - **Scale & operational assumptions** — the operating envelope: roughly how many users /
     how much traffic, how much data, what latency/availability, and the growth horizon.
     Ask explicitly (users rarely volunteer it) with order-of-magnitude choices, smallest
     first (≈100 → ≈10k → ≈100k+). "Small, optimize for simplicity" is a legitimate answer
     that stops sessions over-engineering. Capture only NFRs that actually change
     architecture — the magnitude, not the mechanism. On an obviously-small project, save an
     exchange by proposing the envelope as a one-line confirm ("looks like ~100 users,
     optimize for simplicity — right?") rather than a separate question; ask the full
     question only when scale is genuinely open.
   - **Definition of done (project level)** — a concrete, *checkable* checklist. "Users
     can shorten and resolve a URL via the API" beats "the API works".
   - **Constraints** — the compact area-of-control: what agents **MAY** change, and what
     they **MUST NOT** touch. Name concrete paths/dirs where you can.
   - **Non-goals** — explicitly out of scope, so sessions don't wander.
4. **Draft, confirm, write.** Present the drafted `GOAL.md`. If the user can approve
   interactively, get approval, then write the file. If you can't get live approval
   (non-interactive run), write it anyway and clearly flag every assumption you made
   (inline or in your summary) for later review. Replace the template's `<!-- prompt -->`
   comments with real content; keep the section headings.

## The GOAL.md skeleton (don't depend on the template file existing)

```markdown
# GOAL

## Mission
<one or two sentences: what, and for whom>

## Scale & operational assumptions
- Users / traffic: <order of magnitude, e.g. ~100 users, low traffic>
- Data volume: <e.g. thousands of rows, not millions>
- Latency / availability: <e.g. best-effort; brief downtime OK>
- Growth horizon: <build for current scale | plan for N× growth>

## Definition of done (project level)
- [ ] <concrete, checkable outcome>
- [ ] <another>
- [ ] <project's configured checks pass>

## Constraints
- Agents MAY change: <paths/dirs/layers>
- Agents MUST NOT change: <paths/dirs/layers>

## Non-goals
- <explicitly out of scope>
```

## Quality bar

- Every definition-of-done item is something you could verify, not a vibe.
- Keep design/config detail **out** of the GOAL — host/base-URL, code length, algorithms,
  schema choices are task-level. The GOAL says what done looks like, not how.
- Scale is the one exception: the operating envelope (magnitude, data volume,
  latency/availability, growth horizon) is goal-level — stable, and it constrains every
  downstream choice. State the **magnitude, not the mechanism** (which database, whether to
  cache stays task-level), and make it concrete: an order of magnitude (~100 vs ~100k) not
  "scalable", a growth horizon that picks a posture ("build for current scale" / "plan for
  N× growth") not "could grow someday". "Best-effort, brief downtime OK" is a fine
  availability answer.
- Constraints name real boundaries (paths, layers, external systems) — what keeps sessions
  in their lane.
- If a DoD item implies a check that `.autobuild/config.yml`'s `checks:` doesn't yet run,
  say so — a DoD whose only check is the seeded `echo 'replace me'` placeholder is
  verifiable in name only. Point the user at **autobuild-configure**; don't bake a "fix your
  checks" note into `GOAL.md` (it stays the stable north star).
- If the user's intent spans several independent subsystems, say so: a sprawling GOAL
  produces an unfocused backlog. Suggest narrowing scope or splitting into phases.

## Next step

Once `GOAL.md` is written, suggest the **autobuild-plan-backlog** skill to break the goal
into a dependency-ordered set of tasks (and **autobuild-configure** if the checks aren't
real yet).
