# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo **is the `autobuild` tool** — a file-native **Python** harness that drains a
backlog of tasks toward a `GOAL.md` by spawning fresh, isolated Claude Code sessions
(`claude -p`) in parallel git worktrees. Read `README.md` for the full mental model.

**Critical distinction:** the files under `autobuild/templates/` (`autobuild/templates/CONTRACT.md`,
`autobuild/templates/GOAL.md`, `autobuild/templates/tasks/`, `autobuild/templates/config.yml`,
`autobuild/templates/skills/`) are *target-project artifacts* read out of the installed package
via `importlib.resources`. `autobuild init` copies `GOAL.md`, `tasks/`, `config.yml` and the
skills into a user's project; it does **not** copy the contract. They are **not** instructions
for developing autobuild itself. In particular, `autobuild/templates/CONTRACT.md` is the
**runtime contract a spawned session obeys** (plan → review → implement → write `result.json`);
the harness stages it into each session dir. Editing it changes how *target-project sessions*
behave, not how you work on this codebase. Do not quote the contract's heading or opening line
verbatim near the top of this file: a root `CLAUDE.md` whose first 10 lines carry them is taken
as a legacy contract (`contract.is_legacy_contract`), and this repo dogfoods itself. `autobuild/templates/skills/` are likewise target-project artifacts: Claude Code Skills
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
uv run autobuild init            # lay down GOAL.md, tasks/, .autobuild/config.yml
uv run autobuild run             # outer loop: schedule -> spawn -> reap, until drained
uv run autobuild status          # task + session state
```

`uv tool install .` installs the `autobuild` command globally (entry point
`autobuild.cli:main`). The pytest suite is the safety net — there is no separate linter
configured. The e2e tests (`tests/test_e2e.py`) drive the whole loop against a **stub `claude`
on `PATH`** (`tests/fixtures/claude`, wired up in `tests/conftest.py`), so they spend no tokens.

Because the stub is hand-coded to match the harness, it can't catch drift between
`templates/CONTRACT.md` (the session contract) and real agent behavior. `tools/live_smoke.py`
closes that: an **opt-in, ad-hoc** runner that drives one real `claude -p` task end-to-end and
asserts the whole contract held. It spends real tokens + needs a `claude` login, so it lives
**outside `tests/`** (pyproject `testpaths = ["tests"]`) and is **never collected by pytest/CI**
— run it by hand after touching the contract, the spawn, or the reaper:
`uv run python tools/live_smoke.py` (env knobs: `AUTOBUILD_SMOKE_MODEL`, `AUTOBUILD_SMOKE_KEEP`,
`AUTOBUILD_SMOKE_DIR`).

## Architecture

`autobuild/cli.py` is the thin dispatcher: `argparse` maps a subcommand to a function (in
`cli.py` or the `loop.py` facade). All commands assume they run from the **target project root**;
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

- **`autobuild/cli.py`** — `argparse` dispatch + the `autobuild` entry point. `run` exits 0 only if
  every task is `done` at run end; otherwise `EXIT_UNFINISHED` (3), including a task blocked by
  an earlier run. `loop.run` returns the not-`done` tasks, read under the run lock. A halted run
  keeps 2 and an interrupted run keeps `128 + signum`; 1 = refused (run lock / nested), 2 also
  = config / preflight / dirty base. `reap` exits 0 unless it halts (2). `ab_init` copies
  the packaged templates into the project and installs the bundled skills under `.claude/skills/`
  (`_install_skills` / `_copy_resource_tree`, copy-if-absent so re-running init never clobbers a
  user's edits); `require_init` guards the other commands.
- **`autobuild/config.py`** — load `.autobuild/config.yml` into a typed, frozen `Config`
  (all keys optional; defaults match the template). Values are **validated at load**: every
  problem is aggregated into one `ConfigError` so a bad config (`integration: prr`,
  `max_parallel: 0`) fails fast with exit 2 *before* any session spawns.
- **`autobuild/tasks.py`** — the `Task` model, frontmatter I/O, `set_status`, and the follow-up
  id allocator (`next_task_id` / `create_task_file`). `iter_tasks` reads through `load_task`,
  which **never raises** for a bad file: a file that does not start with `---` (e.g. a
  `tasks/README.md`) or vanished mid-scan is omitted; a file that starts with `---` but has a YAML
  error, an unclosed block, a non-mapping block, no `status:` line, or undecodable bytes becomes
  an in-memory `invalid` pseudo-task (`Task.error` says why; id recovered from its `id:` line,
  else the stem). `read_task` is unchanged (raises). `index_tasks` is the single id index
  (**first file in sorted-path order wins**); `duplicate_ids` lists every id declared by more than
  one file.
- **`autobuild/scheduler.py`** — `runnable_tasks` (status `todo` + every `depends_on` id is
  `done`, sorted by `(priority, id)`) and `claim_tasks(n)`, which atomically flips up to N tasks
  `todo→claimed` under an exclusive `fcntl.flock` on `.autobuild/backlog.lock` so parallel runs
  never double-claim. The same `backlog_lock` is reused when allocating follow-up ids.
  `stuck_tasks` classifies a non-terminal task that can never run (missing / blocked / cyclic
  dependency) so the loop and `status` can name *why* instead of stalling silently. It also names
  `duplicate-id` / `invalid-task-file` (the task's own file) and `duplicate-dependency` /
  `invalid-dependency` (a transitive dep). `runnable_tasks` never schedules a duplicated id, nor a
  dependent of one (even if every copy is `done`).
- **`autobuild/worktree.py`** — `make_worktree` / `remove_worktree` / `prune_worktrees`: one
  isolated checkout + branch (`autobuild/<task-id>`) per session, forked from `base_branch`
  (the branch is reused on a retry), then each `done` dependency's `autobuild/<dep>` branch is
  **merged onto that base** so a dependent sees its dependencies' code in every integration mode,
  not just `auto-merge`. A dependency that won't merge raises `DependencyMergeConflict` (content
  conflict) or `DependencyMergeError` (other failure, e.g. no git identity), aborting first so no
  half-merged tree is left.
- **`autobuild/session.py`** — `spawn_session`: builds the session dir + worktree + `meta.json`,
  **stages the contract/`GOAL.md`/task into the session dir** (`contract.stage_contract`) and
  points the prompt only at those copies + the worktree — never a main-checkout path, so the agent has no
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
  `ANTHROPIC_*` auth. It also **sets `AUTOBUILD_IN_SESSION=1`** in the child env (on a fresh dict,
  never mutating `os.environ`) so a nested `autobuild run` invoked from inside a session can refuse
  to recursively spawn more sessions (see `loop._assert_not_nested`). It returns an in-memory
  `RunningSession` carrying the child's `pgid`
  (also persisted to `meta.json`) and a **monotonic `deadline`** (`time.monotonic() +
  task_timeout_seconds`); the loop supervises it with `proc.poll()` plus that deadline — together
  replacing the bash `.running` PID file.
- **`autobuild/loop.py`** — a thin facade: re-exports the public names `cli.py` and the tests
  use (`run`, `status`, `reap`, `clean`, `RunLockHeld`, `RunHalted`, `BaseBranchLeak`, …). The loop itself is
  split by responsibility across the modules below. A re-export does not redirect calls inside the
  defining module, so a test that monkeypatches an internal (`_git`, `which`, `os.killpg`, …) must
  patch the module where it lives (e.g. `integrate._git`), not `loop`.
- **`autobuild/common.py`** — shared low-level helpers: console logging (`log`/`ok`/`warn`),
  `_now`, the `_git`/`_gh` subprocess seams, `current_branch`, sentinel parsing (`_read_json`,
  `_classify_sentinel`), and `_notify(config, event, message)`, the single, best-effort
  **notification choke point**: when `config.notify_command` is set it runs that shell command
  with `AUTOBUILD_EVENT`/`AUTOBUILD_MESSAGE` in the env, bounded by a timeout and swallowing every
  failure (a broken notifier never breaks a run). It fires on four coarse events — `done` (run
  end, in `_finish_run`, including reason `interrupted`), `rate_limited` (once per run, from
  `_supervise`, when a session first hits a usage/rate limit), `halt` (the `RunHalted` path), and `needs_human`
  (`_reap_session_locked`) — and is operator-controlled shell, not a security boundary.
- **`autobuild/guards.py`** — the single-supervisor `run_lock` (`fcntl.flock` on
  `.autobuild/run.lock`, `RunLockHeld`), the nested-run refusal (`_assert_not_nested`,
  `NestedRunRefused`), and two **worktree-isolation guards**: `_assert_base_clean` (run refuses to
  start with a dirty base tree — uncommitted source a stray `git add -A` could sweep; `tasks/` +
  `.autobuild/` exempt, override `AUTOBUILD_ALLOW_DIRTY_BASE=1`; raises `DirtyBaseTree`) and
  `base_leak_commits`, which the reaper runs on every session **before integrating**: a non-merge
  commit on `base_branch`'s first-parent chain since the session's `base_sha` is, by construction,
  a session that committed onto base (the harness only ever advances base via `--no-ff` merges).
  The response is scoped to `config.integration`: in `auto-merge` (deliverables merge onto base)
  the reaper raises `BaseBranchLeak` to halt the whole run; in `pr`/`branch` (base is never
  integrated onto) it blocks just that task and continues. Either way it writes a `leak.json`
  marker. Every run-halting condition subclasses `RunHalted`: `BaseBranchLeak`,
  `BaseBranchNotCheckedOut`, and `MergeUndoFailed`.
- **`autobuild/verify.py`** — `verify_checks` (re-run `config.checks` in the session worktree
  before integrating) and `_write_checks_log`.
- **`autobuild/integrate.py`** — `integrate` (`pr` / `auto-merge` / `branch`), the bounded
  push/PR retry-with-backoff helpers (`_push_branch`, `_open_pr`, permanent-failure classifiers),
  and `_post_merge_verify` (the auto-merge combined-tree gate). Before an auto-merge, `integrate`
  raises `BaseBranchNotCheckedOut` if the main checkout is not on `base_branch`. `_undo_merge`
  undoes a merge that fails the post-merge checks with `git reset --merge` (fallback: reset the
  merge-footprint paths to HEAD, then `reset --merge` again; raises `MergeUndoFailed` to halt if
  that fails). It never uses `reset --hard`, which would revert uncommitted `tasks/` status.
- **`autobuild/reaper.py`** — `reap_session` / `_reap_session_locked` (under a per-session
  `_session_reap_lock`), `_handle_timeout`, `_requeue_fresh` (re-queue a killed session's task
  WITHOUT a `retries.json` entry — used for `INTERRUPTED`), `file_followups`, `reap_all` (takes
  a `should_stop` callable; the supervisor passes `interrupt.requested` so a shutdown stops
  reaping between sessions), and `reap_stalled`. During a shutdown a COMPLETE reap whose checks
  or integration fail is abandoned (no status change, no `reaped.json`): the failure is likely
  the terminal SIGINT hitting the check/git child, so the next run retries it.
  After the base-leak check, a session whose task id is duplicated or whose file is `invalid` is
  **deferred**: warn, return `False`, no status write, no `reaped.json` — the next pass after the
  human fixes the file reaps it normally. (The leak branch skips its `set_status` for such a task.)
  `_handle_timeout` re-reads under the lock with `load_task`, so a file corrupted mid-reap takes
  the "superseded" path.
- **`autobuild/procs.py`** — process-group control: `_signal_session`, `_kill_groups` (parallel:
  `SIGTERM` every live group → ONE shared `kill_grace_seconds` → `SIGKILL` the rest; a
  `should_stop` callable cuts the grace short), `_kill_group` (the one-session form), and the
  guarded `_kill_orphan_group` used by reconcile.
- **`autobuild/interrupt.py`** — a leaf module (no autobuild imports) holding the cooperative
  Ctrl-C / SIGTERM flag: `install()` (context manager `run` wraps `_run_locked` in; restores the
  previous handlers), `requested()` / `forced()` / `signum()` / `reset()`, and `RunInterrupted`.
  The handler only sets a flag — raising from it could drop a session between `Popen` and
  `running.append`, or cut a reap between merge and `reaped.json`. First signal = graceful,
  second = skip the kill grace and restore `SIG_DFL`, third = default kill.
- **`autobuild/reconcile.py`** — `reconcile` (startup crash recovery) and `reap`, the lock-aware
  wrapper the CLI `reap` maps to (not `reap_all` directly).
- **`autobuild/supervisor.py`** — the outer Ralph-style loop: `run` (holds the run lock),
  `_run_locked`, `_supervise`, session harvesting (`_harvest`), and the **per-session timeout**
  (deadline-bounded waits via `_next_wait` / `_wait_until_next_event`). `_supervise` returns the
  terminal reason (`drained` / `settled` / `max_iterations` / a budget cap / `rate_limited` /
  `interrupted`); `_run_locked` passes it (or `halted` from the `RunHalted` handler) to
  `_finish_run`. A session that leaves no usable result and whose output ends in a usage/rate
  limit (`progress.detect_limit`) gets a synthetic `RATE_LIMITED` instead of `BLOCKED`/`TIMEOUT`
  (`_write_synthetic`; `reconcile` does the same for orphans). Once any of this run's sessions is
  reaped `RATE_LIMITED`, `_supervise` stops claiming (folded into `over_budget`), fires one
  `rate_limited` notification, drains in-flight work, and returns `rate_limited`.
  `_supervise` checks `interrupt.requested()` at consistent points (top of each pass, after each
  spawn + append — un-claiming the unspawned rest — and after the post-claim harvest). On
  `interrupted`, `_run_locked` calls `_shutdown` inside the same `try` as the halt handler:
  `_kill_groups` → `INTERRUPTED` sentinels (`write_sentinel_if_absent`, so a real result
  written during the grace wins) → reap only those → `_finish_run("interrupted")` → raise
  `RunInterrupted` (CLI exit `128 + signum`). The halt handler likewise kills via `_kill_groups`
  and writes `INTERRUPTED` sentinels (unreaped) for the sessions it kills.
- **`autobuild/costs.py`** — `_run_spend` (the per-run cost-budget sum, scoped to this run's
  `run_sids`) and `_settle_session_costs` (bounded, best-effort): real `claude` flushes its
  cost-bearing stream-json `result` event just AFTER `result.json`; `_harvest` waits for the
  process to exit (bounded) before reaping, and as a backstop the final draining session(s) are given a short grace to flush before the summary reads them
  (#49).
- **`autobuild/report.py`** — `collect_status` / `status`, `write_run_summary`, `_finish_run`
  (settles costs, writes `.autobuild/run-summary.json` — counts + per-task integration
  outcome/attempts/wall-time + cost + stuck list + this run's `rate_limited` sessions from
  `rate_limited_sessions` — prints a short digest, and fires `done`;
  best-effort, so a summary-write failure never masks the real outcome or a re-raised halt), and
  `clean`.
- **`autobuild/contract.py`** — the session contract. `resolve_contract(paths)` returns
  `(text, source)`: the `.autobuild/CONTRACT.md` override, else a root `CLAUDE.md` that
  `is_legacy_contract` (marker phrases in its first 10 lines; back-compat), else the packaged
  `templates/CONTRACT.md`. `stage_contract` writes the result to `<sdir>/CONTRACT.md` and copies
  `GOAL.md` + the task. `checks_block(checks)` renders the **Checks** block `build_prompt`
  puts in the prompt: `.autobuild/config.yml` is gitignored, so the prompt is the only place a
  session learns the commands the reaper re-runs. Leaf module (no loop imports).
- **`autobuild/preflight.py`** — `autobuild doctor`: cheap, side-effect-free environment
  checks each returning `(level, name, detail)` (PASS/WARN/FAIL). `doctor` prints a report
  and exits non-zero if any check FAILs (WARN never fails); `assert_run_preflight` runs the
  **critical** subset (`claude` on PATH, git identity, and — auto-merge only — `base_branch`
  checked out in the main checkout) at the start of `_run_locked`, before
  claiming or spawning, raising `PreflightError` so a misconfigured host aborts early instead
  of wasting sessions. `_check_contract` reports the contract source and WARNs on a legacy
  root contract. doctor only *reports* (the base-tree-clean check is a WARN); `run` keeps
  enforcing via `_assert_base_clean`. `_check_task_files` WARNs on `invalid` task files and
  duplicated ids (including a duplicate whose copies are all `done`) and names ignored non-task
  `tasks/*.md` files. `_check_gitignore` WARNs when `.gitignore` has no rule besides
  `.autobuild/` (sessions run the checks before committing, so unignored caches/build output
  can land in task commits and conflict across parallel branches). Imports from `common`/`guards` only (never `loop`), so
  `supervisor` can import it without a cycle.
- **`autobuild/progress.py`** — parse a session's stream-json `session.out` (one JSON event
  per line) into a small `SessionProgress` (`messages` = assistant-event count, a liveness
  proxy; `cost_usd` from the terminal `result` event; `finished`). **Total by construction**
  (a torn/partial/non-JSON line is skipped, never raised) because `autobuild status` and the
  supervisor read the file while `claude` is still writing it. Sessions are spawned with
  `--output-format stream-json --verbose` (so `session.out` streams live instead of buffering
  to 0 bytes) and `stdin=DEVNULL` (drops the CLI's ~3s no-stdin wait); `status` surfaces the
  parsed progress, and the cost budget sums each session's `cost_usd`. `SessionProgress.limit`
  / `detect_limit(sdir)` report a usage/rate limit iff the stream ENDS in one — a `rate_limit_event`
  with `status: "rejected"`, or an `assistant` event with `error: "rate_limit" | "billing_error"`
  (shapes verified against the CLI 2.1.330 schema; no live limited transcript yet). A later
  allowed event or normal assistant message clears it; free text is never matched.
- **`autobuild/paths.py`** — the frozen `Paths` dataclass.

### The session lifecycle / sentinel protocol (the core data flow)

This is the part that requires reading multiple files to understand:

1. `claim_tasks` flips a runnable `todo` task → `claimed` under the backlog lock.
2. `spawn_session` creates `.autobuild/sessions/<sid>/` + a worktree, stages the
   contract (`resolve_contract`: override → legacy root → packaged) as `CONTRACT.md`, plus
   `GOAL.md` and the task, into the session dir, puts `config.checks` in the prompt's Checks
   block, and snapshots `base_sha` into `meta.json`, sets
   the task `in-progress`, and launches a **fresh `claude -p` process** (the Ralph property: no
   context carries between sessions — all state is files + git). It returns a `RunningSession`
   the loop tracks in memory.
3. The session follows its staged `CONTRACT.md` and ends by writing
   `.autobuild/sessions/<sid>/result.json` with `status: COMPLETE | BLOCKED | NEEDS_HUMAN`.
4. `reap_session` reads that sentinel and acts. **Before anything else** it runs the
   worktree-escape check (`base_leak_commits`): if the session left a non-merge commit on
   `base_branch` it blocks the task and writes a `leak.json` marker, then — in `auto-merge` —
   raises `BaseBranchLeak` to halt (base is corrupt and must not be merged onto), or — in
   `pr`/`branch` — just blocks that task and lets the run continue. Otherwise, for
   `COMPLETE` it **verifies first**: unless
   `verify_checks: false`, it re-runs `config.checks` against the session's worktree and, if any
   fail, blocks the task and keeps the branch instead of integrating (trust, but verify). For
   `auto-merge` the reaper first requires `base_branch` to be checked out (else
   `BaseBranchNotCheckedOut` halts before the merge; the session stays reapable), and a second
   gate (`_post_merge_verify`, opt-out `verify_after_merge`) re-runs the
   checks against the **combined base tree** after the `--no-ff` merge and **undoes the merge with
   `git reset --merge`** if they fail (fallback: reset the merge-footprint paths to HEAD, then
   `reset --merge` again; halts with `MergeUndoFailed` if that fails) — catching semantic merge skew two independently-green
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
   a synthetic `BLOCKED`. A session that wrote a valid `result.json` but is still running is
   **not reaped until its process exits** (it stays in `running`; `reap_all(skip=...)` passes it
   over), so the reaper never removes a worktree under a live agent, reads the agent's final
   `result.json`, and the exit flushes its cost. It is killed and then reaped once it lingers past
   `_RESULT_LINGER_SECONDS` (30s) or its deadline. At startup `reconcile` (only while it holds the run lock) returns
   orphaned `claimed` tasks to `todo` and recovers orphaned `in-progress` sessions by writing a
   synthetic `TIMEOUT` (a crash leaves the same killed-before-result state a deadline does), so
   the reaper re-queues them — re-forking from base, bounded by `timeout_max_retries` — or leaves
   them terminal `timeout` once the budget is spent. A killed `run` therefore self-heals from
   files alone (no PID file needed) instead of stranding orphans for a human to reset.

The loop terminates when nothing is running **and** nothing is runnable (backlog drained, or
settled behind blocked/unsatisfiable deps), or when `max_iterations` trips.
"Drained" means every task is terminal, not that every task succeeded: `_report_drained` prints
"backlog drained — COMPLETE" only when all are `done`, else a warning with the per-status counts. When a pass makes no
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
"nothing running" alone.) `INTERRUPTED` (operator signal, or a halt killing in-flight sessions)
and `RATE_LIMITED` (usage/rate limit) are synthetic sentinels: the reaper re-queues the task to
`todo` like a timeout re-queue but **without** touching `retries.json`, so neither spends timeout
budget (a rate limit needs no budget of its own: the run stops claiming on the first). The leak check
still runs first. The template task file documents the human vocabulary
`todo | claimed | in-progress | review | done | blocked`; the harness also applies `timeout`.
`invalid` is read-only: `load_task` assigns it in memory to an unparseable task file and the
harness never writes it. It is non-terminal and never runnable, so a backlog with an invalid file
**settles** (the stuck report names it) and never reports `drained`.

## Conventions and gotchas

- **State is disposable and crash-safe.** Everything under `.autobuild/` (sessions, worktrees,
  `backlog.lock`) can be deleted and rebuilds from `tasks/` + git. There is no `.running` PID
  file anymore — running sessions live in memory, and `reconcile` recovers orphans on the next
  `run`. Re-running `autobuild run` after a kill resumes from the files; design changes to honor
  this idempotency.
- **Task files.** A `tasks/*.md` that starts with `---` is a task and must parse (else it is
  `invalid` and never scheduled); a file without frontmatter is ignored. Ids must be unique — a
  duplicated id is never scheduled, never satisfies a dependency, and defers its reap.
  An optional `model:` overrides `config.model` for that task's session (`Task.model`;
  recorded in `meta.json`). A bare `model:` or `null` means no override; a non-string,
  empty, multi-word or `-`-prefixed value makes the file `invalid`.
- **Keep the reaper idempotent.** Integration happens *before* the task is marked `done`, and
  the `reaped.json` marker guards against double-integration / double follow-up filing. Preserve
  both properties when touching `reaper.py` / `integrate.py`.
- **The backlog lock is `fcntl.flock` on `.autobuild/backlog.lock`** (auto-released if the holder
  dies — no stale lockdir to strand). Any mutation that must be serialized across parallel runs
  (claiming tasks, allocating follow-up ids) goes through `backlog_lock`.
- **`run` is single-supervisor.** It holds a separate `fcntl.flock` on `.autobuild/run.lock` for
  its whole lifetime; a second `autobuild run` is refused (`RunLockHeld`, non-zero exit) rather
  than fighting over the same sessions. `reap` is lock-aware — it only performs the dangerous
  in-progress recovery reconcile sweep (which re-queues orphans as a synthetic `TIMEOUT`) when it
  can take the lock (i.e. no live `run`).
- **auto-merge requires `base_branch` checked out** in the main checkout, because `git merge`
  lands on whatever is checked out. A mismatch refuses the run (critical preflight) or halts it
  before merging (`BaseBranchNotCheckedOut`). The post-merge undo uses `reset --merge`, never
  `reset --hard`, so uncommitted `tasks/` status and human edits survive.
- **`run` refuses to nest.** A spawned session carries `AUTOBUILD_IN_SESSION=1` (set in
  `session._session_env`); `run` checks it via `_assert_not_nested` *before* the run lock and raises
  `NestedRunRefused` (non-zero exit) so a session can't recursively spawn more sessions (fork-bomb /
  token-burn). It's cheap accident prevention, not a security boundary — `unset AUTOBUILD_IN_SESSION`
  is the deliberate escape hatch. Tests/dogfooding scrub the var in `conftest.hermetic_env`.
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
- Naming: CLI subcommands map to `run` / `status` / `reap` / `clean`, re-exported by `loop.py` (plus
  `ab_init` in `cli.py`); module-internal helpers are prefixed `_`.
- **Keep modules small.** Non-test Python modules under `autobuild/` must stay under ~400 lines.
  When a change would push a module past that, split it by responsibility (as `loop.py` was split
  into `supervisor` / `reaper` / `integrate` / …). Tests may be longer.
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
