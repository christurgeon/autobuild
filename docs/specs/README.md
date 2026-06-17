# autobuild hardening specs

Design specs and hand-build task breakdowns for taking autobuild from "a harness that
built itself" to "safe to run a real, unattended project." Each spec was drafted, then
adversarially reviewed against the actual code; 🛡️ marks where the review changed the design.

## Contents

| File | What |
|------|------|
| `specs-1-2-permissions-timeout.md` | Design spec #1 (autonomy/permissions posture) + #2 (per-session timeout/budget/retry) |
| `specs-3-4-planner-verification.md` | Design spec #3 (planner) + #4 (acceptance-level verification) |
| `handbuild/BUILD-ORDER.md` | Developer build order + dependency graph for implementing #1 + #2 |
| `handbuild/task-101..107.md` | The seven dependency-ordered implementation tasks for #1 + #2 |

## Important
The `handbuild/task-*.md` files live under `docs/`, **not** in the repo's `tasks/`
directory, on purpose: they are for a human to implement by hand and must **not** be picked
up by `autobuild run`. You cannot safely dogfood the fixes to the loop's own permission and
liveness safety. Dogfood specs #3/#4 only after #1/#2 land.

## Build order (foundation first)
`101 → 102 → {103, 104} → 105 → 106 → 107`. See `handbuild/BUILD-ORDER.md` for the rationale.
