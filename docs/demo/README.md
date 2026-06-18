# Demo recording

`../demo.gif` is a **real, token-free** autobuild run, recorded against the repo's
stub `claude` (`tests/fixtures/claude`). The orchestration is genuine — scheduling,
parallel worktrees, reaping, dependency gating, the `status` view — only the agent's
edits are canned, so it spends no tokens and is fully reproducible.

## Regenerate

```bash
uv tool install .            # put `autobuild` on PATH
uv tool install asciinema    # recorder
# agg (cast -> gif renderer): grab the static binary from
#   https://github.com/asciinema/agg/releases  and put it on PATH
docs/demo/record.sh          # writes docs/demo.gif
```

- `seed.sh` lays down the throwaway demo project (GOAL, four tasks with a dependency
  chain, a stub-`claude` on PATH, `integration: branch` so no network is needed).
- `record.sh` drives `autobuild run` + `autobuild status` with a typed-command effect,
  records the cast, and renders the GIF.

The demo blocks `task-002` (via `STUB_STATUS_task_002=BLOCKED`) so the gated `task-004`
visibly stays unrun — showing dependency gating, not just a happy path.
