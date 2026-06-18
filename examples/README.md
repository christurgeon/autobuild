# Examples

A worked example of a real autobuild project, so you can see what a filled-out
backlog looks like instead of starting from the skeletal `autobuild init` templates.

## `quotes-api/`

A tiny FastAPI service that serves random quotes — small enough to read in one sitting,
but with a dependency graph rich enough to show autobuild actually fanning out.

```
quotes-api/
  GOAL.md                     <- the north star, filled in (mission, definition of done, constraints)
  .autobuild/
    config.yml                <- real checks (ruff + pytest), integration: pr
  tasks/
    task-001-scaffold.md      <- root: project skeleton + healthcheck
    task-002-storage.md       <- depends on 001
    task-003-random-endpoint.md   <- depends on 002  ┐ run in
    task-004-add-endpoint.md      <- depends on 002  ┘ parallel
    task-005-tag-filter.md        <- depends on 003
    task-006-docs.md              <- depends on 003 + 004 (a join)
```

The dependency graph:

```
001 ──> 002 ──┬──> 003 ──> 005
              └──> 004
        003, 004 ──> 006
```

So a run would do `task-001` alone, then `task-002`, then **`003` and `004` in parallel**,
then `005` (gated behind `003`) and finally `006` (gated behind both `003` and `004`).

## Notes

- **Every task starts `todo`.** That's the only status a human authors. The harness drives
  the rest of the lifecycle (`claimed → in-progress → done | blocked`) — you don't hand-edit
  those. See the README's task state machine.
- **`CLAUDE.md` is omitted on purpose.** `autobuild init` also drops a `CLAUDE.md` (the
  contract every session obeys: plan → review → implement → write `result.json`). It's
  identical for every project, so it's not duplicated here — see
  [`autobuild/templates/CLAUDE.md`](../autobuild/templates/CLAUDE.md).
- **`.autobuild/` holds only `config.yml` here.** At runtime the harness also creates
  `sessions/`, `worktrees/`, and `backlog.lock` under `.autobuild/` — all disposable machine
  state, rebuilt from `tasks/` + git, so none of it is checked in.

## Trying it

This is documentation, not a runnable target — but you can copy it into a fresh repo and run
autobuild against it (swap the `checks` for whatever your project really uses):

```bash
cp -r examples/quotes-api ~/quotes-api && cd ~/quotes-api
git init && git add -A && git commit -m "seed"
autobuild status     # see the backlog and its dependency gating
autobuild run        # drain it (needs a real `claude` on PATH)
```
