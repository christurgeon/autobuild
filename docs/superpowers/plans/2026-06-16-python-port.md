# autobuild bash→Python Port Implementation Plan

> **For agentic workers:** Executed inline (TDD) by the author session, who holds the
> full reconciliation context. Steps use checkbox (`- [ ]`) tracking. The bash in
> `lib/*.sh` + `bin/autobuild` is the reference spec; it is removed only once the port is green.

**Goal:** Port the bash v0 of autobuild to a small, stdlib-first Python package (uv-managed,
PyYAML the only runtime dep), preserving behavior 1:1 while fixing the data-corruption /
duplicate-side-effect / loop-hang defects the pre-port audit surfaced.

**Architecture:** A package `autobuild/` mirroring the bash modules. One long-lived `run`
supervisor spawns a fresh `claude -p` per task via `subprocess.Popen` in a git worktree on
`autobuild/<task-id>`; sessions signal completion by writing `result.json`; a reaper acts on
the sentinel, integrates the branch, and files follow-ups. All state lives in files + git;
`.autobuild/` is disposable and rebuilt from `tasks/` + git.

**Tech Stack:** Python 3.11+, uv, PyYAML (read-only), stdlib `json`/`subprocess`/`pathlib`/
`dataclasses`/`argparse`/`fcntl`. pytest for tests.

---

## Module map (mirrors the bash)

| Python | Ports from | Responsibility |
|---|---|---|
| `autobuild/paths.py` | `common.sh` paths | `Paths` dataclass: resolve `.autobuild/`, `tasks/`, sessions, worktrees, lock, config from a project root |
| `autobuild/config.py` | `cfg`, `cfg_list` | `Config` dataclass + `load_config()` (PyYAML, defaults) |
| `autobuild/tasks.py` | `fm`, `set_status`, `count_status`, id-alloc | `Task` model, frontmatter read, surgical+atomic status write, task index, id allocator |
| `autobuild/scheduler.py` | `scheduler.sh` | `runnable_tasks` (dep gating + priority), `claim_tasks` (atomic flock) |
| `autobuild/worktree.py` | `worktree.sh` | make/remove/prune git worktrees |
| `autobuild/session.py` | `session.sh` | `build_prompt`, `spawn_session` (Popen), meta.json, worktree/CLI failure sentinels |
| `autobuild/loop.py` | `loop.sh` | outer loop, reaper, reconcile, stalled-handling, integrate, follow-ups, status, clean |
| `autobuild/cli.py` | `bin/autobuild` | argparse dispatch + `main()` entry point |
| `autobuild/templates/` | `templates/` | bundled GOAL/CLAUDE/config/tasks, copied by `init` via importlib.resources |

## Shared types (locked — keep consistent across modules)

- `Config(model:str, max_parallel:int, base_branch:str, max_iterations:int, integration:str, checks:list[str], claude_cmd:str)`
  defaults: `claude-opus-4-8 / 3 / main / 50 / pr / [] / claude`.
- `Task(id:str, title:str, status:str, priority:int, depends_on:list[str], path:Path)`.
  Missing `priority` → `DEFAULT_PRIORITY = 999` (lowest; never silently promote). Missing `status` → `todo`. Missing `depends_on` → `[]`.
- Status values (task): `todo → claimed → in-progress → done | blocked`. Sentinel statuses (result.json): `COMPLETE | BLOCKED | NEEDS_HUMAN`.
- `TERMINAL = {"done", "blocked"}`.

## Bug-fix decisions (from the audit; scope = "port + corruption/hang fixes")

**Fixed for free by real parsers (`json`/PyYAML):** title injection into follow-up frontmatter;
follow-up `priority`/`notes` now carried (not hardcoded); `depends_on` parsed as a real list;
exact id matching via an in-memory `{id: Task}` index (no `grep "id:"` substring `task-1`==`task-10`).

**Fix now:**
1. **Idempotent reaper** — write a `reaped.json` marker as the last step; skip any session already marked. (Bash re-ran integrate + re-filed follow-ups every loop pass — the "skip" was a comment with no code.)
2. **Integrate-before-done** — set task `done` only after integration succeeds; on failure mark `blocked`. (Bash set `done` first, stranding un-integrated work.)
3. **Collision-safe id allocator** — under the backlog lock, allocate from `max(frontmatter id)+1` and reserve-by-create; non-empty unique slug; carry `priority`/`notes`.
4. **Startup reconciliation** — restores crash-safe resume without a PID file: orphaned `claimed` → `todo`; orphaned `in-progress` (no live proc, no result.json) → `BLOCKED` sentinel; prune stale worktrees.
5. **Atomic status write** — same-dir temp + `os.replace` (no cross-FS truncation).
6. **Priority** — default missing/non-numeric explicitly; order by `(priority, id)`.
7. **auto-merge conflict** — `git merge --abort` and report failure rather than leaving the repo mid-merge.
8. **Hang guard** — terminate when nothing is running AND nothing is runnable, reporting stuck/blocked tasks, instead of spinning to `max_iterations`.

**Deliberate mechanism changes (same guarantee, better crash behavior), flagged in PR:**
- Claim lock: `fcntl.flock` on `.autobuild/backlog.lock` (auto-released on death) instead of bash's `mkdir` lockdir.
- Supervision: in-memory `{sid: Popen}` + `poll()` instead of the `.autobuild/.running` PID file.
- `set_status` preserves the original guidance comment instead of bash's `# set by autobuild`.
- Session id: `uuid4` suffix instead of 4 random bytes.

**Deferred (preserve bash behavior, note with TODO):** dependency-cycle/unknown-dep *detection* beyond the hang guard; empty-branch integration checks; PID-reuse hardening (moot once PIDs aren't persisted).

## Task order (each: failing test → minimal code → green → commit)

1. Scaffold: `pyproject.toml` (uv, hatchling, entry point), package skeleton, move templates into the package, `uv sync`.
2. `config.py` — defaults + parse the real template config (incl. quoted `checks[]`).
3. `tasks.py` — frontmatter read (scalars + `depends_on` list + comment/body ignore); surgical atomic `set_status`; task index; id allocator.
4. `scheduler.py` — dep gating, priority ordering, atomic claim under contention.
5. `worktree.py` — make/remove/prune against a real tmp git repo (branch reuse).
6. `session.py` — prompt format (stub depends on it), Popen spawn + meta.json + in-progress, worktree-fail→BLOCKED, missing-claude→NEEDS_HUMAN.
7. `loop.py` — reaper per sentinel, idempotency (double-reap), integrate (auto-merge / pr-no-gh), follow-up filing, reconcile, stalled→BLOCKED, termination.
8. `cli.py` — init (templates land byte-identical), status, dispatch.
9. e2e — stub `claude` on PATH, full `run` in a throwaway repo, assert dependency-ordered branches + all `done`; partial-failure gating; stall→blocked.
10. Remove bash (`bin/autobuild`, `lib/*.sh`), update `README.md`, final green, push, PR.
