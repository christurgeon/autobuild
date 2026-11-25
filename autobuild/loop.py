"""Facade over the loop modules: re-exports the names `cli.py` and the tests use.

The implementation lives in cohesive modules:
- `common`     — console logging, timestamps, `_notify`, the `_git`/`_gh` seams, sentinel parsing;
- `guards`     — base-leak / dirty-base checks, the run lock, the nested-run refusal;
- `verify`     — the pre-integration checks gate;
- `integrate`  — pr / auto-merge / branch integration (+ push/PR retries, post-merge verify);
- `reaper`     — `reap_session` / `reap_all`, timeout re-queue, follow-up filing;
- `procs`      — process-group signalling and kills;
- `interrupt`  — the cooperative Ctrl-C / SIGTERM flag (leaf module) + `RunInterrupted`;
- `reconcile`  — startup crash recovery and the one-shot `reap` command;
- `costs`      — per-run spend and the run-end cost-settle grace;
- `supervisor` — `run`, the scheduling loop, harvesting, deadline-bounded waits;
- `report`     — `status`, the run summary, and `clean`.

Key invariants these modules uphold:
- the reaper is idempotent (a reaped.json marker guards re-integration);
- integration runs BEFORE the task is marked done, so a failed merge never leaves
  a falsely-'done' task;
- follow-up ids are allocated under the backlog lock, so concurrent reaps can't collide;
- a startup reconcile restores crash-safe resume from files + git alone (no PID file);
- the loop terminates when nothing runs AND nothing is runnable, instead of
  spinning to max_iterations on tasks stuck behind unsatisfiable deps.

A re-export does not redirect calls made inside the defining module, so tests that
monkeypatch an internal must patch the module where it lives (e.g. `integrate._git`).
"""

from __future__ import annotations

from .common import (  # noqa: F401
    _NOTIFY_TIMEOUT_SECONDS,
    _c,
    _classify_sentinel,
    current_branch,
    _gh,
    _git,
    _notify,
    _now,
    _read_json,
    log,
    ok,
    warn,
)
from .costs import _cost_pending, _run_spend, _settle_session_costs  # noqa: F401
from .guards import (  # noqa: F401
    BaseBranchLeak,
    BaseBranchNotCheckedOut,
    DirtyBaseTree,
    MergeUndoFailed,
    NestedRunRefused,
    RunHalted,
    RunLockHeld,
    _assert_base_clean,
    _assert_not_nested,
    base_leak_commits,
    dirty_base_paths,
    run_lock,
)
from .integrate import (  # noqa: F401
    _open_pr,
    _post_merge_checks_pass,
    _pr_create_is_permanent,
    _pr_exists,
    _push_branch,
    _push_is_permanent,
    _undo_merge,
    _with_retries,
    integrate,
)
from .interrupt import RunInterrupted  # noqa: F401
from .procs import _kill_group, _kill_groups, _kill_orphan_group, _signal_session  # noqa: F401
from .reaper import (  # noqa: F401
    _handle_timeout,
    _reap_session_locked,
    _requeue_fresh,
    _session_reap_lock,
    file_followups,
    reap_all,
    reap_session,
    reap_stalled,
)
from .reconcile import reap, reconcile  # noqa: F401
from .report import (  # noqa: F401
    _finish_run,
    _run_end_message,
    clean,
    collect_status,
    rate_limited_sessions,
    status,
    write_run_summary,
)
from .session import _atomic_write_json, write_sentinel_if_absent  # noqa: F401
from .supervisor import (  # noqa: F401
    _harvest,
    _next_wait,
    _run_locked,
    _shutdown,
    _supervise,
    _wait_until_next_event,
    run,
)
from .verify import _write_checks_log, verify_checks  # noqa: F401
