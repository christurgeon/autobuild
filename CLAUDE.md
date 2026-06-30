# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo **is the `autobuild` tool** — a file-native **Python** harness that drains a
backlog of tasks toward a `GOAL.md` by spawning fresh, isolated Claude Code sessions
(`claude -p`) in parallel git worktrees. Read `README.md` for the full mental model.

**Critical distinction:** the files under `autobuild/templates/` (`autobuild/templates/CLAUDE.md`,
`autobuild/templates/GOAL.md`, `autobuild/templates/tasks/`, `autobuild/templates/config.yml`,
`autobuild/templates/skills/`) are *scaffolding copied into a user's target project* by
`autobuild init` (read out of the installed package via `importlib.resources`). They are
**not** instructions for developing autobuild itself. In particular, `autobuild/templates/CLAUDE.md`
is the **runtime contract a spawned session obeys** (plan → review → implement → write
`result.json`). Editing it changes how *target-project sessions* behave, not how you work on this
codebase. `autobuild/templates/skills/` are likewise target-project artifacts: Claude Code Skills
(`autobuild-author-goal`, `autobuild-plan-backlog`, `autobuild-configure`, `autobuild-triage`)
installed into the user's `.claude/skills/` to help a *human* author and operate a backlog
interactively — each carries a guard so a spawned single-task session won't invoke it. This root
file is the only one that governs work on the harness. (`examples/quotes-api/` is likewise *sample
target-project data* — a worked backlog the README links to — not guidance for developing the
harness.)

## Running and developing

A small, stdlib-first Python package managed with **uv**. The only runtime dependency is
**PyYAML**; the dev group adds **pytest**. Requires Python 3.11+ (uv provisions the interpreter).

```bash
uv sync                          # create the venv + install deps (incl. pytest)
uv run pytest                    # run the full suite (unit + a token-free e2e loop)

# Exercise the CLI against a throwaway target project (never run init in this repo):
uv run autobuild --help
mkdir -p /tmp/ab-scratch && cd /tmp/ab-scratch && git init
uv run autobuild init            # lay down GOAL.md, CLAUDE.md, tasks/, .autobuild/config.yml
uv run autobuild run             # outer loop: schedule -> spawn -> reap, until drained
uv run autobuild status          # task + session state
```

`uv tool install .` installs the `autobuild` command globally (entry point
`autobuild.cli:main`). The pytest suite is the safety net — there is no separate linter
configured. The e2e tests (`tests/test_e2e.py`) drive the whole loop against a **stub `claude`
on `PATH`** (`tests/fixtures/claude`, wired up in `tests/conftest.py`), so they spend no tokens.

## Architecture

`autobuild/cli.py` is the thin dispatcher: `argparse` maps a subcommand to a function (in
`cli.py` or `loop.py`). All commands assume they run from the **target project root**;
`Paths.from_cwd()` resolves `root = Path.cwd()` and derives `.autobuild/`, `tasks/`, `GOAL.md`,
etc. from it (the typed replacement for the bash `PROJECT_ROOT` and its path vars).

Config and task frontmatter are parsed with **PyYAML**, and the JSON sentinels
(`result.json`, `meta.json`, `reaped.json`) with the stdlib `json` module — no `grep`/`sed`
or `yq`. The three I/O layers everything else builds on:

- `load_config(config_file)` → a frozen `Config` (`config.py`); flat schema, all keys optional
  with the template defaults.
- `read_task` / `parse_frontmatter` / `set_status(path, status)` (`tasks.py`) — frontmatter
  read plus a **surgical, atomic** status rewrite (single-line regex; temp file + `os.replace`)
  that preserves comments and never reserializes a human task file.
- `Paths` (`paths.py`) — the one place every `.autobuild/` location is defined.

Module responsibilities:

- **`autobuild/cli.py`** — `argparse` dispatch + the `autobuild` entry point. `ab_init` copies
  the packaged templates into the project and installs the bundled skills under `.claude/skills/`
  (`_install_skills` / `_copy_resource_tree`, copy-if-absent so re-running init never clobbers a
  user's edits); `require_init` guards the other commands.
- **`autobuild/config.py`** — load `.autobuild/config.yml` into a typed, frozen `Config`
  (all keys optional; defaults match the template). Values are **validated at load**: every
  problem is aggregated into one `ConfigError` so a bad config (`integration: prr`,
  `max_parallel: 0`) fails fast with exit 2 *before* any session spawns.
- **`autobuild/tasks.py`** — the `Task` model, frontmatter I/O, `set_status`, and the follow-up
  id allocator (`next_task_id` / `create_task_file`).
- **`autobuild/scheduler.py`** — `runnable_tasks` (status `todo` + every `depends_on` id is
  `done`, sorted by `(priority, id)`) and `claim_tasks(n)`, which atomically flips up to N tasks
  `todo→claimed` under an exclusive `fcntl.flock` on `.autobuild/backlog.lock` so parallel runs
  never double-claim. The same `backlog_lock` is reused when allocating follow-up ids.
  `stuck_tasks` classifies a non-terminal task that can never run (missing / blocked / cyclic
  dependency) so the loop and `status` can name *why* instead of stalling silently.
- **`autobuild/worktree.py`** — `make_worktree` / `remove_worktree` / `prune_worktrees`: one
  isolated checkout + branch (`autobuild/<task-id>`) per session, forked from `base_branch`
  (the branch is reused on a retry), then each `done` dependency's `autobuild/<dep>` branch is
  **merged onto that base** so a dependent sees its dependencies' code in every integration mode,
  not just `auto-merge`. A dependency that won't merge raises `DependencyMergeConflict` (content
  conflict) or `DependencyMergeError` (other failure, e.g. no git identity), aborting first so no
  half-merged tree is left.
- **`autobuild/session.py`** — `spawn_session`: builds the session dir + worktree + `meta.json`,
  **stages the contract/`GOAL.md`/task into the session dir** (`_stage_contract`) and points the
  prompt only at those copies + the worktree — never a main-checkout path, so the agent has no
  base-tree path to resolve work against (the original worktree-escape vector) — snapshots
  `base_branch`'s sha into `meta.json` (`base_sha`, for the leak check), sets the task
  `in-progress`, and launches a **fresh `claude -p`** in its **own process group**
  (`start_new_session=True`, so the whole agent subtree can be signalled at once) via
  `subprocess.Popen`. `_session_flags` builds the **permission posture**: a bypass posture
  (`dangerously_bypass_permissions`, or `permission_mode: bypassPermissions`) emits
  `--dangerously-skip-permissions`, but only when the sandbox gate is satisfied — otherwise it
  raises `BypassNotPermitted` and the spawn is refused; a fenced posture emits `--permission-mode`,
  an `--allowedTools` allowlist, a `.claude/**` write-deny, and `--strict-mcp-config` (plus
  `--max-turns` either way). `_session_env` also strips git push/transport credentials
  (`GH_TOKEN`/`GITHUB_TOKEN`, `SSH_AUTH_SOCK`, askpass + `GIT_SSH_COMMAND`, inline `GIT_CONFIG_*`
  injection) from the child env as defense-in-depth — the agent keeps its commit identity and
  `ANTHROPIC_*` auth. It returns an in-memory `RunningSession` carrying the child's `pgid`
  (also persisted to `meta.json`) and a **monotonic `deadline`** (`time.monotonic() +
  task_timeout_seconds`); the loop supervises it with `proc.poll()` plus that deadline — together
  replacing the bash `.running` PID file.
- **`autobuild/loop.py`** — the outer Ralph-style loop (`run`, holding a single-supervisor
  `fcntl.flock` on `.autobuild/run.lock`), the reaper (`reap_all` / `reap_session`), session
  harvesting (`_harvest` / `_classify_sentinel`), the **per-session timeout** (deadline-bounded
  waits via `_next_wait` / `_wait_until_next_event`, and `_kill_group` / `_signal_session` —
  `SIGTERM` → `kill_grace_seconds` → `SIGKILL` over the session's process group), `reconcile`
  (startup crash recovery), `verify_checks`, `integrate`, `file_followups`, `status`,
  `write_run_summary`, and `clean`. The CLI `reap` maps to `reap` here (a lock-aware
  wrapper), not `reap_all` directly. `_notify(config, event, message)` is the single,
  best-effort **notification choke point**: when `config.notify_command` is set it runs that
  shell command with `AUTOBUILD_EVENT`/`AUTOBUILD_MESSAGE` in the env, bounded by a timeout
  and swallowing every failure (a broken notifier never breaks a run). It fires on three
  coarse events — `done` (run end, in `_finish_run`), `halt` (the `BaseBranchLeak` path), and
  `needs_human` (`_reap_session_locked`) — and is operator-controlled shell, not a security
  boundary. `_supervise` returns the terminal reason
  (`drained` / `settled` / `max_iterations`); `_run_locked` passes it (or `halted` from the
  `BaseBranchLeak` handler) to `_finish_run`, which writes `.autobuild/run-summary.json`
  (counts + per-task integration outcome/attempts/wall-time + stuck list, reusing
  `collect_status`) and prints a short digest — best-effort, so a summary-write failure
  never masks the real outcome or a re-raised halt.
  Two **worktree-isolation guards** also live here: `_assert_base_clean` (run refuses to start
  with a dirty base tree — uncommitted source a stray `git add -A` could sweep; `tasks/` +
  `.autobuild/` exempt, override `AUTOBUILD_ALLOW_DIRTY_BASE=1`) and `base_leak_commits`, which
  the reaper runs on every session **before integrating**: a non-merge commit on `base_branch`'s
  first-parent chain since the session's `base_sha` is, by construction, a session that committed
  onto base (the harness only ever advances base via `--no-ff` merges). The response is scoped to
  `config.integration`: in `auto-merge` (deliverables merge onto base) it raises `BaseBranchLeak`
  to halt the whole run; in `pr`/`branch` (base is never integrated onto) it blocks just that task
  and continues. Either way it writes a `leak.json` marker.
- **`autobuild/preflight.py`** — `autobuild doctor`: cheap, side-effect-free environment
  checks each returning `(level, name, detail)` (PASS/WARN/FAIL). `doctor` prints a report
  and exits non-zero if any check FAILs (WARN never fails); `assert_run_preflight` runs the
  **critical** subset (`claude` on PATH + git identity) at the start of `_run_locked`, before
  claiming or spawning, raising `PreflightError` so a misconfigured host aborts early instead
  of wasting sessions. doctor only *reports* (the base-tree-clean check is a WARN); `run` keeps
  enforcing via `_assert_base_clean`. Imports loop helpers, so loop imports it lazily.
- **`autobuild/paths.py`** — the frozen `Paths` dataclass.

### The session lifecycle / sentinel protocol (the core data flow)

This is the part that requires reading multiple files to understand:

1. `claim_tasks` flips a runnable `todo` task → `claimed` under the backlog lock.
2. `spawn_session` creates `.autobuild/sessions/<sid>/` + a worktree, stages the
   contract/`GOAL.md`/task into the session dir and snapshots `base_sha` into `meta.json`, sets
   the task `in-progress`, and launches a **fresh `claude -p` process** (the Ralph property: no
   context carries between sessions — all state is files + git). It returns a `RunningSession`
   the loop tracks in memory.
3. The session follows `autobuild/templates/CLAUDE.md`'s contract and ends by writing
   `.autobuild/sessions/<sid>/result.json` with `status: COMPLETE | BLOCKED | NEEDS_HUMAN`.
4. `reap_session` reads that sentinel and acts. **Before anything else** it runs the
   worktree-escape check (`base_leak_commits`): if the session left a non-merge commit on
   `base_branch` it blocks the task and writes a `leak.json` marker, then — in `auto-merge` —
   raises `BaseBranchLeak` to halt (base is corrupt and must not be merged onto), or — in
   `pr`/`branch` — just blocks that task and lets the run continue. Otherwise, for
   `COMPLETE` it **verifies first**: unless
   `verify_checks: false`, it re-runs `config.checks` against the session's worktree and, if any
   fail, blocks the task and keeps the branch instead of integrating (trust, but verify). For
   `auto-merge` a second gate (`_post_merge_verify`, opt-out `verify_after_merge`) re-runs the
   checks against the **combined base tree** after the `--no-ff` merge and **hard-resets base back
   to its pre-merge HEAD** if they fail — catching semantic merge skew two independently-green
   branches can produce with no textual conflict. If they
   pass it **integrates** (`integrate` → `pr` via `gh` / `auto-merge` / `branch`) and only then
   `set_status`es the task `done` — so a failed auto-merge leaves the task `blocked`, not falsely
   `done`. `BLOCKED`/`NEEDS_HUMAN` set the task `blocked`; a synthetic `TIMEOUT` is never integrated
   or verified (its tree is incomplete) and files no follow-ups — instead, under the backlog lock,
   `_handle_timeout` either **re-queues** the task to `todo` (force-deleting the partial branch so
   the retry re-forks from base) while its attempt count is within `timeout_max_retries`, or once
   the budget is spent leaves it terminal `timeout` and clears its ledger entry. For `COMPLETE` it
   files any `followups[]` as new `tasks/*.md` (ids allocated under the backlog lock),
   removes the worktree, and drops a `reaped.json` marker. That marker makes the reaper
   **idempotent**: a second pass over the same session is a no-op.
5. Three safety nets catch sessions that don't end cleanly. In-loop, `_harvest` never drops a
   session without reaping it: a session that **crosses its monotonic deadline** while still
   running is killed (`_kill_group`) and given a synthetic `TIMEOUT` — unless a `result.json` was
   written during the grace window, which is re-classified and still wins; a process that exits
   with no usable sentinel — absent, or present but unparseable (`_classify_sentinel`) — is given
   a synthetic `BLOCKED`. At startup `reconcile` (only while it holds the run lock) returns
   orphaned `claimed` tasks to `todo` and recovers orphaned `in-progress` sessions by writing a
   synthetic `TIMEOUT` (a crash leaves the same killed-before-result state a deadline does), so
   the reaper re-queues them — re-forking from base, bounded by `timeout_max_retries` — or leaves
   them terminal `timeout` once the budget is spent. A killed `run` therefore self-heals from
   files alone (no PID file needed) instead of stranding orphans for a human to reset.

The loop terminates when nothing is running **and** nothing is runnable (backlog drained, or
settled behind blocked/unsatisfiable deps), or when `max_iterations` trips. When a pass makes no
progress it blocks on the next live session finishing instead of busy-sleeping.

### Task status state machine

`todo` → `claimed` → `in-progress` → terminal (`done` | `blocked` | `timeout`). A session killed
past its deadline is, under the backlog lock, either **re-queued** to `todo` for another attempt
(its partial `autobuild/<tid>` branch force-deleted so the retry re-forks from base) or — once its
distinct-attempt count in the `.autobuild/retries.json` ledger exceeds `timeout_max_retries` — left
in terminal `timeout`: a settled failure kept distinct from `blocked` so a human can tell "ran out
of time" from "agent hit a wall". A dependent of a terminal `timeout` task sees a
`timed-out-dependency` blocker via `stuck_tasks`. `done`, `blocked`, and `timeout` are the terminal
states (`is_terminal` / the `TERMINAL` set); everything else counts as in-flight, which is what
keeps the loop running. (Because a re-queue creates newly-runnable work mid-`_harvest`, the loop's
settle check yields back to claiming when `runnable_tasks` is non-empty rather than terminating on
"nothing running" alone.) The template task file documents the human vocabulary
`todo | claimed | in-progress | review | done | blocked`; the harness also applies `timeout`.

## Conventions and gotchas

- **State is disposable and crash-safe.** Everything under `.autobuild/` (sessions, worktrees,
  `backlog.lock`) can be deleted and rebuilds from `tasks/` + git. There is no `.running` PID
  file anymore — running sessions live in memory, and `reconcile` recovers orphans on the next
  `run`. Re-running `autobuild run` after a kill resumes from the files; design changes to honor
  this idempotency.
- **Keep the reaper idempotent.** Integration happens *before* the task is marked `done`, and
  the `reaped.json` marker guards against double-integration / double follow-up filing. Preserve
  both properties when touching `loop.py`.
- **The backlog lock is `fcntl.flock` on `.autobuild/backlog.lock`** (auto-released if the holder
  dies — no stale lockdir to strand). Any mutation that must be serialized across parallel runs
  (claiming tasks, allocating follow-up ids) goes through `backlog_lock`.
- **`run` is single-supervisor.** It holds a separate `fcntl.flock` on `.autobuild/run.lock` for
  its whole lifetime; a second `autobuild run` is refused (`RunLockHeld`, non-zero exit) rather
  than fighting over the same sessions. `reap` is lock-aware — it only performs the dangerous
  in-progress recovery reconcile sweep (which re-queues orphans as a synthetic `TIMEOUT`) when it
  can take the lock (i.e. no live `run`).
- **Status writes are surgical and atomic.** `set_status` rewrites only the `status:` line via
  regex + `os.replace`; never round-trip a human task file through `yaml.dump` (it would strip
  comments and reorder keys). Follow-up task files are generated, so those *are* serialized with
  `yaml.safe_dump`.
- **Per-session timeouts are monotonic and in-memory.** A session's deadline lives only on its
  `RunningSession` (`time.monotonic()`-based) and is enforced solely by the live supervisor's
  `_harvest`; a killed `run` does not resume in-memory deadlines, but `reconcile` still recovers
  its orphans by re-queueing them as a synthetic `TIMEOUT` — so a crash self-heals like a deadline
  kill rather than stranding work. The child runs in its own process group so `_kill_group` can
  signal the whole agent subtree (`os.killpg`); the `pgid` is persisted to `meta.json`.
- Naming: CLI subcommands map to `run` / `status` / `reap` / `clean` in `loop.py` (plus
  `ab_init` in `cli.py`); module-internal helpers are prefixed `_`.
- Minimal dependencies by design: **PyYAML** is the only runtime dep; everything else (process
  orchestration via `subprocess`, JSON sentinels, the claim lock) is the standard library. Match
  that style unless you're deliberately adding a dependency.
- The core has been hardened by dogfooding autobuild on its own backlog — the run-lock,
  dependency-aware worktree bases, reaper checks-verification, config validation, stuck-dependency
  surfacing, the session **permission posture** (bypass/sandbox gate, fenced allowlist),
  **per-session timeouts** (process-group spawn + monotonic deadline + `SIGTERM`/`SIGKILL` kill),
  and **worktree isolation** (session-dir contract staging instead of main-checkout anchors,
  `base_leak_commits` escape detection, the dirty-base guard) are all in place with tests, along
  with the sentinel/termination races those exposed. Match that bar: a real defect plus a
  regression test beats new scope.
