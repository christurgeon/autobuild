"""The outer Ralph-style loop, the reaper, reconcile, status, and clean.

Key invariants this module upholds:
- the reaper is idempotent (a reaped.json marker guards re-integration);
- integration runs BEFORE the task is marked done, so a failed merge never leaves
  a falsely-'done' task;
- follow-up ids are allocated under the backlog lock, so concurrent reaps can't collide;
- a startup reconcile restores crash-safe resume from files + git alone (no PID file);
- the loop terminates when nothing runs AND nothing is runnable, instead of
  spinning to max_iterations on tasks stuck behind unsatisfiable deps.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from .config import Config
from .paths import Paths
from .progress import read_progress
from .retries import clear_retries, record_timeout, retry_count
from .scheduler import backlog_lock, claim_tasks, runnable_tasks, stuck_tasks
from .session import (
    RunningSession,
    _atomic_write_json,
    parse_leading_json,
    spawn_session,
    write_sentinel_if_absent,
)
from .tasks import (
    create_task_file,
    is_terminal,
    iter_tasks,
    next_task_id,
    read_task,
    set_status,
    task_index,
)
from .worktree import branch_name, delete_branch, prune_worktrees, remove_worktree

# --- logging -----------------------------------------------------------------

def _c(code: str) -> str:
    return f"\033[{code}m"


def log(msg: str) -> None:
    print(f"{_c('1;34')}[autobuild]{_c('0')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('1;32')}[ ok ]{_c('0')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33')}[warn]{_c('0')} {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- operator notifications --------------------------------------------------

_NOTIFY_TIMEOUT_SECONDS = 30  # bound a wedged notifier so it can never hang the run


def _notify(config: Config, event: str, message: str) -> None:
    """Best-effort operator notification — the single choke point for every event.

    When `config.notify_command` is non-empty, run it via the shell with the event type
    and message exposed as the environment variables `AUTOBUILD_EVENT` and
    `AUTOBUILD_MESSAGE` (the operator wires those to Telegram / push / email / whatever).
    Bounded by a timeout so a wedged notifier can't hang the run, and ALL failures
    (non-zero exit, timeout, OSError) are swallowed with a `warn(...)` — a notification
    problem must NEVER break or halt a run.

    The command is operator-controlled (≈ a shell), so this is not a security boundary —
    same posture as the permission allowlist (see the README security note)."""
    cmd = (config.notify_command or "").strip()
    if not cmd:
        return  # notifications disabled — no subprocess, no env churn
    env = {**os.environ, "AUTOBUILD_EVENT": event, "AUTOBUILD_MESSAGE": message}
    try:
        r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                           timeout=_NOTIFY_TIMEOUT_SECONDS)
        if r.returncode != 0:
            warn(f"notify_command exited {r.returncode} for event '{event}' (ignored)")
    except subprocess.TimeoutExpired:
        warn(f"notify_command timed out after {_NOTIFY_TIMEOUT_SECONDS}s for "
             f"event '{event}' (ignored)")
    except OSError as exc:
        warn(f"notify_command failed for event '{event}': {exc} (ignored)")


def _read_json(path: Path) -> dict | None:
    """Read `path` and parse it as a JSON object via parse_leading_json (tolerating
    stray trailing data after a valid leading object). Returns None if the file is
    unreadable, torn/partial, or parses to a non-object — returning a non-dict would
    crash callers that do `.get()` (reap_session, collect_status), wedging
    run/reap/status on a single poisoned file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_leading_json(text)


def _classify_sentinel(sdir: Path) -> str:
    """Classify a session's result.json:
      'reapable' — present and parses to a usable JSON object (a dict);
      'corrupt'  — present but unparseable (leading garbage / empty) or not an object
                   (`[]`, `"x"`) — shapes raw_decode cannot salvage;
      'absent'   — no result.json yet.

    Centralizing this is what lets every gate (`_harvest`, `reconcile`) treat a
    present-but-unparseable sentinel as a finished-but-failed session and BLOCK it,
    instead of skipping it on mere file existence and stranding the task forever."""
    result_file = sdir / "result.json"
    if not result_file.exists():
        return "absent"
    return "reapable" if isinstance(_read_json(result_file), dict) else "corrupt"


# --- worktree-isolation guards -----------------------------------------------

class BaseBranchLeak(RuntimeError):
    """A session advanced base_branch with commit(s) the harness did not create — an
    agent that escaped its worktree and committed onto the live base. Raised by the
    reaper to HALT the run: base is no longer something autobuild controls, so building
    further integrations on it would compound the damage. str() is the operator message."""


class DirtyBaseTree(RuntimeError):
    """`run` refused to start because the base working tree has uncommitted source — a
    stray `git add -A` in a session could sweep it into a task commit. str() is the
    operator message."""


def base_leak_commits(paths: Paths, base_branch: str, base_sha: str) -> list[str]:
    """Commits the harness did NOT create that have landed on base_branch's first-parent
    mainline since `base_sha`. The harness only ever advances base via a `--no-ff` MERGE
    commit (integrate's auto-merge); a session is supposed to commit solely on its own
    autobuild/<tid> branch. So a NON-MERGE commit on base's first-parent chain is, by
    construction, a worktree escape — an agent that committed straight onto base. Returns
    the offending shas (newest first), or [] when clean or when base/base_sha can't be
    resolved (we never false-alarm on a check we couldn't run)."""
    if not base_sha or not base_branch:
        # An empty right-hand side of the range resolves to HEAD (`base_sha..`), which
        # would mis-report ordinary HEAD commits as leaks. Unreachable via the CLI
        # (config rejects an empty base_branch) but guarded so the contract holds for a
        # directly-constructed Config too.
        return []
    r = _git(paths.root, "rev-list", "--first-parent", "--no-merges",
             f"{base_sha}..{base_branch}")
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.split() if line]


def dirty_base_paths(paths: Paths) -> list[str]:
    """Uncommitted paths in the base working tree that a `git add -A` could sweep,
    EXCLUDING `.autobuild/` (harness state) and `tasks/` (autobuild rewrites task status
    continuously — flagging it would make the guard useless mid-run). These two are not
    user work a sweep would lose; anything else (a modified source file, a stray
    untracked file) is. Returns [] when git can't report status."""
    r = _git(paths.root, "status", "--porcelain")
    if r.returncode != 0:
        return []
    dirty: list[str] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:           # rename: the destination is what would be swept
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        top = path.split("/", 1)[0]
        if top in (".autobuild", "tasks"):
            continue
        dirty.append(path)
    return dirty


# --- run lock ----------------------------------------------------------------

class RunLockHeld(RuntimeError):
    """Raised when the advisory run lock is already held — i.e. another
    `autobuild run` owns this project. str() is the lock file path."""


class NestedRunRefused(RuntimeError):
    """Raised when `autobuild run` is invoked from inside a spawned session (the child
    env carries AUTOBUILD_IN_SESSION=1, set by session._session_env). A session driving
    the harness would recursively spawn more sessions — a fork-bomb / token-burn sharp
    edge — so we refuse. str() is the operator-facing reason."""


def _assert_not_nested() -> None:
    """Refuse to start a run nested inside a spawned session. The marker is set in every
    child's env, so an agent that shells out to `autobuild run` (directly or via a script)
    trips this before any lock is taken or session is spawned. This is cheap accident
    prevention, not a security boundary — an operator who truly intends a nested run can
    `unset AUTOBUILD_IN_SESSION` first."""
    if os.environ.get("AUTOBUILD_IN_SESSION") == "1":
        raise NestedRunRefused(
            "refusing to run: AUTOBUILD_IN_SESSION=1 means this process is inside an "
            "autobuild-spawned session, and a session running 'autobuild run' would "
            "recursively spawn more sessions. If you really mean to, 'unset "
            "AUTOBUILD_IN_SESSION' first.")


@contextlib.contextmanager
def run_lock(lock_file: Path):
    """Hold the project's exclusive run lock for the duration of the with-block.

    A single `run` owns the lock for its whole lifetime; this is what lets
    `reconcile()` safely recover orphaned in-progress sessions — re-queue them as a
    synthetic TIMEOUT, or block them — since the lock proves no other process is
    driving them. Acquisition is non-blocking: if another holder
    has it, raise RunLockHeld immediately rather than waiting. The lock is an
    fcntl.flock, so the kernel releases it automatically if the holder dies — the
    exact crash semantic we want, with no stale-PID games."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise RunLockHeld(str(lock_file)) from e
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# --- integration -------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _gh(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `gh` in `root`. Mirrors `_git` so the gh seam is monkeypatchable in tests
    and the retry helpers have a single place to invoke gh."""
    return subprocess.run(["gh", *args], cwd=str(root), capture_output=True, text=True)


# Markers in a failed `git push` stderr that mean a PERMANENT failure (auth / no remote).
# Retrying these only wastes time, so a push whose stderr matches stays single-shot.
# Transient failures (DNS, timeouts, rate limits — "could not resolve host", "connection
# timed out", "unable to access", HTTP 5xx) match none of these and so are retried.
_PERMANENT_PUSH_MARKERS = (
    "does not appear to be a git repository",
    "no configured push destination",
    "no such remote",
    "repository not found",
    "could not read from remote repository",
    "authentication failed",
    "permission denied",
    "access denied",
    "could not read username",
    "terminal prompts disabled",
)


def _push_is_permanent(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(m in s for m in _PERMANENT_PUSH_MARKERS)


# Markers in a failed `gh pr create` stderr that mean a PERMANENT failure (auth / wrong
# permissions / missing repo). gh formats HTTP errors as "HTTP 401: Bad credentials" /
# "HTTP 403: ..." and a missing repo via GraphQL as "Could not resolve to a Repository
# with the name ...". Retrying these only burns the budget with real backoff sleeps, so a
# matching failure stays single-shot — mirroring _push_is_permanent for `git push`.
_PERMANENT_PR_MARKERS = (
    "authentication failed",
    "bad credentials",
    "http 401",
    "401 unauthorized",
    "http 403",
    "403 forbidden",
    "gh auth login",
    "must be authenticated",
    "could not resolve to a repository",
    "repository not found",
    "resource not accessible",
    "permission denied",
    "access denied",
)

# Transient 403s that must stay retryable even though they carry a permanent-looking
# marker: GitHub primary/secondary rate limits ("HTTP 403: API rate limit exceeded",
# "You have exceeded a secondary rate limit") and abuse detection both surface as 403 but
# clear on their own. A match here vetoes the permanent classification.
_TRANSIENT_PR_OVERRIDES = (
    "rate limit",
    "abuse detection",
)


def _pr_create_is_permanent(stderr: str) -> bool:
    s = (stderr or "").lower()
    if any(m in s for m in _TRANSIENT_PR_OVERRIDES):
        return False
    return any(m in s for m in _PERMANENT_PR_MARKERS)


def _with_retries(attempt, retries: int, sleep):
    """Call `attempt()` up to `1 + max(0, retries)` times. `attempt` returns
    `(done, value)`: a truthy `done` returns `value` immediately (a success, or a
    permanent failure not worth retrying); a falsy `done` backs off (sleep 1s, 2s, … via
    the injected `sleep`) and retries. After the final attempt the last `value` is
    returned regardless — so the caller falls back to today's behavior on exhaustion."""
    total = max(0, retries) + 1
    value = None
    for i in range(total):
        done, value = attempt()
        if done:
            return value
        if i < total - 1:
            sleep(i + 1)  # increasing backoff: 1s, 2s, ...
    return value


def _push_branch(root: Path, branch: str, retries: int, sleep) -> subprocess.CompletedProcess:
    """Push `branch` to origin, retrying transient failures with backoff. `git push` of
    the same branch is idempotent, so a retry is safe. A permanent failure (auth / no
    remote) is not retried. Returns the final attempt's CompletedProcess (returncode 0 =
    pushed)."""
    def attempt():
        push = _git(root, "push", "-u", "origin", branch)
        if push.returncode == 0 or _push_is_permanent(push.stderr):
            return True, push
        return False, push
    return _with_retries(attempt, retries, sleep)


def _pr_exists(root: Path, branch: str) -> bool:
    """Best-effort: is there already an open PR for `branch`? Checked before treating a
    failed `gh pr create` as final, since `gh pr create` is NOT idempotent — a transient
    failure that actually opened the PR (or a concurrent create) must not be retried into
    a duplicate. Any error / unparseable output is treated as 'no PR' (we then retry)."""
    r = _gh(root, "pr", "list", "--head", branch, "--json", "url")
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout.strip() or "[]")
    except (json.JSONDecodeError, ValueError):
        return False
    return bool(data)


def _open_pr(root: Path, tid: str, branch: str, base: str, retries: int, sleep) -> tuple[bool, str]:
    """Open a PR via `gh pr create`, retrying transient failures with backoff. Treats
    gh's own 'already exists' error — and an existing PR found via `_pr_exists` — as
    success (no duplicate). On exhaustion returns today's exact (success, detail): the
    branch is pushed, so a human can open the PR from it, hence success=True."""
    def attempt():
        r = _gh(root, "pr", "create", "--head", branch, "--base", base,
                "--title", f"autobuild: {tid}", "--body", f"Automated by autobuild for {tid}.")
        if r.returncode == 0:
            return True, (True, f"opened PR for {branch}")
        if "already exists" in (r.stderr or "").lower() or _pr_exists(root, branch):
            return True, (True, f"PR already exists for {branch}")
        # A permanent failure (auth / wrong permissions / missing repo) won't succeed on
        # retry — short-circuit to the same fallback as exhaustion (branch is pushed, so
        # a human can open the PR) but single-shot, skipping the backoff sleeps. Checked
        # AFTER _pr_exists so a transient failure that actually opened the PR still wins.
        if _pr_create_is_permanent(r.stderr):
            return True, (True, f"PR creation failed for {branch}; pushed branch left for manual PR")
        # Branch IS pushed (visible on the remote); only the PR-open step failed -> a human
        # can open it from the pushed branch, so the work landed: done. (Today's message.)
        return False, (True, f"PR creation failed for {branch}; pushed branch left for manual PR")
    return _with_retries(attempt, retries, sleep)


def integrate(tid: str, config: Config, paths: Paths, sdir: Path, *, sleep=time.sleep) -> tuple[bool, str]:
    """Integrate the task's branch per config. Returns (success, detail). Failure means
    the work did not land: an auto-merge conflict, or (auto-merge only) a clean merge whose
    COMBINED base tree fails the post-merge checks — that merge is reverted. pr/branch always
    succeed because the branch itself is the deliverable. `sdir` is the session dir, used for
    the post-merge forensic log.

    In pr mode the two transient remote ops — `git push` and `gh pr create` — are wrapped
    in a bounded retry-with-backoff (`config.integration_max_retries` extra attempts) so a
    network blip / rate-limit doesn't cost a manual re-run; deterministic failures (auth,
    no remote, merge conflict) stay single-shot. `sleep` is injectable for tests."""
    branch = branch_name(tid)
    base = config.base_branch
    root = paths.root
    mode = config.integration
    retries = config.integration_max_retries

    if mode == "branch":
        return True, f"left branch {branch} for manual merge"

    if mode == "pr":
        if not which("gh"):
            return True, f"gh not found; left branch {branch} for manual PR"
        push = _push_branch(root, branch, retries, sleep)
        if push.returncode != 0:
            # Push failed (no remote / auth, or transient retries exhausted): nothing
            # reached the remote, so there is no PR. The local branch is still the
            # deliverable downstream tasks merge, so the task stays done — but say so
            # accurately instead of the misleading "PR creation failed" (which implies the
            # branch was pushed). Check remote/auth to get a PR.
            return True, f"push failed for {branch}; kept locally, no PR opened (check remote/auth)"
        return _open_pr(root, tid, branch, base, retries, sleep)

    if mode == "auto-merge":
        # Snapshot base's HEAD BEFORE the merge so a failed post-merge verification can
        # undo it precisely (simpler and more local than re-reading meta.json's base_sha).
        pre_merge_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        r = _git(root, "merge", "--no-ff", "-m", f"autobuild: merge {tid}", branch)
        if r.returncode != 0:
            _git(root, "merge", "--abort")  # don't leave the repo mid-merge
            return False, f"auto-merge conflict for {branch}"
        return _post_merge_verify(tid, branch, config, paths, sdir, pre_merge_sha)

    return False, f"unknown integration mode '{mode}'"


def _post_merge_verify(tid: str, branch: str, config: Config, paths: Paths, sdir: Path,
                       pre_merge_sha: str) -> tuple[bool, str]:
    """After an auto-merge lands, re-run config.checks against the COMBINED base working
    tree (paths.root). Two independently-green branches can still merge into a red base with
    no textual conflict (semantic skew: A renames a helper, B adds a caller of the old name);
    the pre-merge gate only ever saw each branch alone, so this is the only place that catches
    it. On the first failing check, `git reset --hard <pre_merge_sha>` removes the merge commit
    (never leave base red), write a forensic log, and report failure — the reaper then blocks
    the task and keeps the branch. Skipped (merge stands) when verify_after_merge is off or
    there are no checks; independent of verify_checks (which gates the pre-merge worktree run)."""
    base = config.base_branch
    landed = f"merged {branch} into {base}"
    if not config.verify_after_merge or not config.checks:
        return True, landed
    for cmd in config.checks:
        r = subprocess.run(cmd, shell=True, cwd=str(paths.root),
                           capture_output=True, text=True)
        if r.returncode != 0:
            _write_checks_log(sdir, cmd, r.returncode, r.stdout, r.stderr,
                              name="post-merge-checks.log")
            _git(paths.root, "reset", "--hard", pre_merge_sha)  # undo the merge
            return False, f"post-merge checks failed for {branch}; merge reverted"
    return True, landed


# --- verification gate -------------------------------------------------------

def _write_checks_log(sdir: Path, cmd: str, code: int, stdout: str, stderr: str,
                      name: str = "checks.log") -> None:
    """Persist why verification failed: the command, its exit code, and the tail of
    its combined output (last 50 lines) so a human can inspect without re-running.
    `name` selects the log file — the pre-merge gate writes `checks.log`, the post-merge
    combined-tree gate writes `post-merge-checks.log`."""
    tail = "\n".join(((stdout or "") + (stderr or "")).splitlines()[-50:])
    (sdir / name).write_text(
        f"command: {cmd}\nexit: {code}\n\n--- output (tail) ---\n{tail}\n",
        encoding="utf-8",
    )


def verify_checks(config: Config, paths: Paths, sdir: Path, sid: str) -> tuple[bool, str]:
    """Re-run config.checks against the session's worktree before integrating —
    the harness's own verification of a COMPLETE, not the agent's self-report.

    Returns (passed, outcome) where outcome is one of "skipped" (verification
    disabled or no checks), "passed", or "failed: <cmd>". Stops at the first failing
    command and writes <sdir>/checks.log. A missing worktree is treated as a failure:
    we cannot verify the tree, so we refuse to trust it."""
    if not config.verify_checks or not config.checks:
        return True, "skipped"

    worktree = paths.worktrees_dir / sid
    if not worktree.is_dir():
        _write_checks_log(sdir, "(pre-flight)", -1, "",
                          f"worktree {worktree} is missing; cannot verify checks")
        return False, "failed: worktree missing"

    for cmd in config.checks:
        r = subprocess.run(cmd, shell=True, cwd=str(worktree),
                           capture_output=True, text=True)
        if r.returncode != 0:
            _write_checks_log(sdir, cmd, r.returncode, r.stdout, r.stderr)
            return False, f"failed: {cmd}"
    return True, "passed"


# --- follow-ups --------------------------------------------------------------

def file_followups(result: dict, sdir: Path, paths: Paths) -> list[str]:
    """Create tasks/ files for each followups[] entry, allocating ids under the
    backlog lock so concurrent reaps can't collide."""
    followups = result.get("followups") or []
    created: list[str] = []
    if not followups:
        return created
    with backlog_lock(paths.lock_file):
        for fu in followups:
            if not isinstance(fu, dict):
                continue
            title = str(fu.get("title", "")).strip()
            if not title:
                continue
            try:
                priority = int(fu.get("priority", 3))
            except (TypeError, ValueError):
                priority = 3
            tid = next_task_id(paths.tasks_dir)
            create_task_file(paths.tasks_dir, tid, title, priority, str(fu.get("notes", "")))
            created.append(tid)
            ok(f"filed follow-up {tid}: {title}")
    return created


# --- reaper ------------------------------------------------------------------

class _ReapLockBusy(RuntimeError):
    """Another process holds this session's reap lock (it's reaping it now)."""


@contextlib.contextmanager
def _session_reap_lock(sdir: Path):
    """Exclusive, NON-BLOCKING per-session lock around a reap. Raises _ReapLockBusy if
    another process is already reaping this session (so we skip it — it's being handled).
    fcntl.flock auto-releases on close and on holder death, so a crashed reaper strands
    nothing (the next pass reaps) — unlike an O_EXCL marker, which would lose the work."""
    f = open(sdir / ".reap.lock", "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise _ReapLockBusy(str(sdir)) from e
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def reap_session(sdir: Path, config: Config, paths: Paths) -> bool:
    """Reap one finished session from its result.json sentinel. Idempotent AND
    concurrency-safe: the reaped.json marker guards re-runs, and a per-session
    NON-BLOCKING flock serializes a `run` and a concurrent `reap` so only one integrates
    (no duplicate PRs / follow-ups). Returns True if it acted, False if skipped/not finished.
    NOTE: the lock closes the *concurrent* double-integration; a crash *mid-integrate*
    (push ok, then death before the marker) is not covered — `integrate` isn't idempotent
    across a crash. That's a separate concern (out of scope here)."""
    if (sdir / "reaped.json").exists():
        return False  # fast path (best-effort): already reaped
    result = _read_json(sdir / "result.json")
    if result is None:
        return False  # not finished, or corrupt -> leave for clean (no lock needed)
    try:
        with _session_reap_lock(sdir):
            # Re-check UNDER the lock: a concurrent reaper may have finished between the
            # fast-path check above and our acquiring the lock. THIS is the real
            # idempotency guard — keep it here in reap_session, not in _session_reap_lock.
            if (sdir / "reaped.json").exists():
                return False
            return _reap_session_locked(sdir, result, config, paths)
    except _ReapLockBusy:
        return False  # another process is reaping this session right now — it's handled


def _handle_timeout(tid: str, sid: str, task, config: Config, paths: Paths) -> bool:
    """Decide a timed-out task's fate under the backlog lock — atomic against claim_tasks
    and a concurrent reap. Returns True iff the task was re-queued for another attempt.

    Guarded on the task still being `in-progress` (re-read under the lock): if a concurrent
    run already moved it on, this reap was superseded — record nothing, change nothing.
    Otherwise record this timeout (a set of session-ids, so re-recording is idempotent) and
    branch on the distinct-attempt count:
      - within timeout_max_retries -> force-delete the partial branch (fresh base) and
        re-queue to `todo`; the same run re-claims it next pass;
      - exhausted -> leave the task terminal `timeout` and clear its ledger entry, so a
        later manual re-open starts with a fresh budget.
    A partial branch that won't delete (degenerate worktree state) makes us give up
    terminally rather than retry onto surviving partial work."""
    def give_up(reason: str) -> bool:
        set_status(task.path, "timeout")
        clear_retries(paths.retries_ledger, tid)
        warn(f"{sid}: {tid} TIMEOUT — {reason}; left terminal (timeout)")
        return False

    with backlog_lock(paths.lock_file):
        if read_task(task.path).status != "in-progress":
            return False  # superseded by a concurrent run — nothing to do
        count = record_timeout(paths.retries_ledger, tid, sid)  # distinct attempts so far
        if count > config.timeout_max_retries:
            return give_up(f"retries exhausted ({config.timeout_max_retries})")
        remove_worktree(paths, sid)  # free the branch from its worktree before -D
        if not delete_branch(paths, tid, force=True):
            # The branch outlived its worktree removal (a genuine git failure, not a
            # checkout pin) — don't re-queue onto surviving partial work; the worktree is
            # already gone, so point the human at the branch to delete by hand.
            return give_up(f"could not delete {branch_name(tid)} for a clean retry — "
                           f"delete it by hand to allow a retry")
        set_status(task.path, "todo")
        warn(f"{sid}: {tid} TIMEOUT — re-queued (attempt {count}/"
             f"{config.timeout_max_retries + 1}); discarded partial {branch_name(tid)}, "
             f"re-forking from base")
        return True


def _reap_session_locked(sdir: Path, result: dict, config: Config, paths: Paths) -> bool:
    """The reap body, run while holding the session's reap lock and after confirming
    reaped.json is absent. `result` is the already-parsed result.json."""
    tid = str(result.get("task", ""))
    status = str(result.get("status", ""))
    sid = sdir.name
    task = task_index(paths.tasks_dir).get(tid)
    integrated = False
    requeued = False
    followups: list[str] = []
    checks_outcome = "n/a"

    # Worktree-escape check FIRST, before any integration: a NON-MERGE commit on
    # base_branch since spawn means the session (or something outside autobuild) committed
    # straight onto base instead of its own branch. Block the task and record forensics
    # either way; the *response* is scoped to the integration mode, because that is the
    # only mode where base movement actually corrupts autobuild's model:
    #   - auto-merge: dependents fork from base and deliverables MERGE onto it, so an
    #     unexpected base commit poisons every later integration. HALT the whole run
    #     (raise) and write NO reaped.json, so re-runs keep flagging until base is cleaned.
    #   - pr/branch: autobuild never advances base — each deliverable is its own branch —
    #     so one session's base movement can't corrupt the others. Block just this task
    #     (its work may have escaped) and keep going; do NOT halt unrelated in-flight work
    #     on what may simply be a concurrent commit to base.
    # Runs for every status (an agent can leak then report BLOCKED/TIMEOUT, not just COMPLETE).
    meta = _read_json(sdir / "meta.json") or {}
    base_branch = str(meta.get("base_branch") or config.base_branch)
    leaks = base_leak_commits(paths, base_branch, str(meta.get("base_sha", "")))
    if leaks:
        if task:
            set_status(task.path, "blocked")
        _atomic_write_json(sdir / "leak.json",
                           {"detected_at": _now(), "task": tid, "base_branch": base_branch,
                            "base_sha": meta.get("base_sha", ""), "commits": leaks,
                            "integration": config.integration})
        warn(f"!! BASE LEAK !! {sid}: {tid} — {base_branch} advanced with "
             f"{len(leaks)} commit(s) autobuild did not create: {', '.join(leaks)}")
        if config.integration == "auto-merge":
            warn(f"   auto-merge integrates onto {base_branch}, so this corrupts every "
                 f"later merge — HALTING. {tid} blocked. Inspect {sdir / 'leak.json'}, move "
                 f"the commit(s) onto a branch and reset {base_branch}, then re-run.")
            # Raise BEFORE the remove_worktree/delete_branch cleanup below: the worktree
            # and the autobuild/<tid> branch must survive so an operator can recover.
            raise BaseBranchLeak(
                f"{base_branch} carries {len(leaks)} commit(s) autobuild did not create "
                f"(session {sid}, task {tid}); refusing to integrate onto a corrupted base")
        # pr/branch: localized — block this task, keep its branch for recovery, and let the
        # rest of the run proceed. Mark reaped so we don't re-flag it every pass.
        warn(f"   {config.integration} mode never integrates onto {base_branch}, so other "
             f"work is unaffected; {tid} blocked (its deliverable may have escaped). "
             f"Inspect {sdir / 'leak.json'}; the run continues.")
        remove_worktree(paths, sid)
        _atomic_write_json(sdir / "reaped.json",
                           {"reaped_at": _now(), "status": status, "integrated": False,
                            "requeued": False, "checks": "n/a", "followups": [],
                            "leak": leaks})
        return True

    if status == "COMPLETE":
        # Trust, but verify: re-run the checks against the committed tree ourselves
        # before integrating. A tree that fails verification is blocked, not merged.
        verified, checks_outcome = verify_checks(config, paths, sdir, sid)
        if not verified:
            if task:
                set_status(task.path, "blocked")
            warn(f"{sid}: {tid} verification {checks_outcome} — not integrated; "
                 f"branch {branch_name(tid)} kept (see {sdir / 'checks.log'})")
            # No follow-ups: a tree that fails checks did not meet the contract, so
            # its self-reported follow-ups are part of the same untrusted output.
        else:
            success, detail = integrate(tid, config, paths, sdir)
            if success:
                if task:
                    set_status(task.path, "done")
                integrated = True
                ok(f"{sid}: {tid} COMPLETE — {detail}")
            else:
                if task:
                    set_status(task.path, "blocked")
                warn(f"{sid}: {tid} integration failed — {detail}")
            followups = file_followups(result, sdir, paths)
    elif status == "BLOCKED":
        if task:
            set_status(task.path, "blocked")
        warn(f"{sid}: {tid} BLOCKED — {result.get('summary', '')}")
    elif status == "NEEDS_HUMAN":
        if task:
            set_status(task.path, "blocked")
        warn(f"{sid}: {tid} NEEDS_HUMAN — see {sdir / 'result.json'}")
        _notify(config, "needs_human",
                f"{tid}: {result.get('summary') or 'needs human attention'} (session {sid})")
    elif status == "TIMEOUT":
        # A killed-past-deadline session: NEVER integrated or verified (its tree is
        # incomplete), and no follow-ups filed. Re-queue for another attempt or, once the
        # budget is spent, leave the task terminal `timeout` (decided under the backlog lock).
        if task:
            requeued = _handle_timeout(tid, sid, task, config, paths)
        else:
            # No task file (deleted/renamed mid-flight) — still reaped by the shared tail,
            # but log it so a timed-out session never vanishes without a diagnostic.
            warn(f"{sid}: {tid} TIMEOUT — task file not found; reaped without retry")
    else:
        warn(f"{sid}: unrecognized status '{status}'")
        return False

    remove_worktree(paths, sid)
    # Under auto-merge the branch's commits now live on base_branch via the merge commit,
    # so the autobuild/<tid> branch is redundant — delete it (safely) instead of letting it
    # accumulate. Other modes keep the branch: it is the deliverable (pr/branch) and a
    # dependent may still need it to layer (the commits live nowhere else). A re-queued
    # timeout already force-deleted its branch in _handle_timeout (fresh base).
    if integrated and config.integration == "auto-merge":
        delete_branch(paths, tid)
    _atomic_write_json(sdir / "reaped.json",
                       {"reaped_at": _now(), "status": status, "integrated": integrated,
                        "requeued": requeued, "checks": checks_outcome, "followups": followups})
    return True


def reap_all(config: Config, paths: Paths) -> int:
    n = 0
    if not paths.sessions_dir.is_dir():
        return 0
    for sdir in sorted(paths.sessions_dir.iterdir()):
        if sdir.is_dir() and reap_session(sdir, config, paths):
            n += 1
    return n


def _signal_session(rs: RunningSession, sig: int) -> None:
    """Signal the session's whole process group (preferred) or the child directly.
    ESRCH — the group/child already exited — is suppressed so one dead session never
    aborts the harvest of the others."""
    with contextlib.suppress(ProcessLookupError):
        if rs.pgid is not None:
            os.killpg(rs.pgid, sig)
        elif rs.proc is not None:
            rs.proc.send_signal(sig)


def _kill_group(rs: RunningSession, grace: float) -> None:
    """Terminate a session and reap its child. If already exited, just reap. Otherwise
    SIGTERM -> wait(grace) -> SIGKILL -> wait(), leaving no zombie."""
    proc = rs.proc
    if proc is None:
        return
    if proc.poll() is not None:
        return  # already exited: poll() reaped it
    _signal_session(rs, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return  # exited within grace -> reaped
    except subprocess.TimeoutExpired:
        pass
    _signal_session(rs, signal.SIGKILL)
    proc.wait()  # SIGKILL is uncatchable; wait unbounded so the child is always reaped
    #              (a bounded wait could time out and leak a zombie the loop won't reclaim)


def reap_stalled(running: list[RunningSession], paths: Paths) -> None:
    """A live handle whose process exited without writing result.json gets a
    BLOCKED sentinel, so a crashed agent can't stall the loop."""
    for rs in running:
        if rs.has_result():
            continue
        if rs.proc is not None and rs.proc.poll() is not None:
            warn(f"{rs.sid} exited without a result; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "session process exited without writing result.json")


def _kill_orphan_group(pgid: object) -> bool:
    """SIGKILL an orphaned session's process group — a crashed run's detached `claude`
    subtree (reparented to init). Returns True if we signalled it.

    Heavily guarded, because os.killpg(pgid) maps to kill(-pgid): pgid 0 == OUR OWN group
    (would SIGKILL the running harness), pgid 1 == kill(-1) == a BROADCAST SIGKILL to every
    process we may signal, and the harness's own group must never be hit by a corrupted
    meta. We also suppress ESRCH (group already gone) AND EPERM (a recycled pgid we may not
    signal) so one un-killable orphan never aborts the whole reconcile sweep.

    CAVEAT — pgid reuse: a recorded pgid shares the pid number space, so after the original
    leader exited and was reaped the OS can recycle the number into an unrelated group (>1);
    worst case this signals that group. The window is bounded — reconcile runs only under
    the run lock, at startup shortly after a crash, on a host where autobuild is the only
    thing spawning detached groups, so a collision needs that exact number recycled since
    the crash. A portable leader-identity check isn't feasible without a new dependency (no
    /proc on macOS); a stronger guard is deferred as future hardening."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    if pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False  # group already gone — nothing to reclaim
    except PermissionError:
        warn(f"cannot signal orphaned process group {pgid} (permission denied); skipping")
        return False


# --- reconcile (startup crash recovery) -------------------------------------

def reconcile(paths: Paths, *, sweep_in_progress: bool = False) -> None:
    """Restore resume-ability after a `run` crash. Orphaned `claimed` tasks (claim
    succeeded, spawn never finished) go back to `todo`.

    The orphaned `in-progress` recovery sweep is gated on `sweep_in_progress`,
    which the caller sets only when it holds the run lock. Without the lock we
    cannot tell a genuinely orphaned session from one a *concurrent* live run is
    actively driving (its child process is invisible to us), so a reap that runs
    alongside a run must NOT sweep — doing so would kill and re-queue live sessions
    and remove their worktrees out from under the run. A fresh `run` (or a `reap`
    that can take the lock) holds it, proving no other process owns these sessions.
    Each orphan is recovered like a deadline kill: a synthetic TIMEOUT sentinel the
    reaper re-queues (or terminal-`timeout`s once the retry budget is spent)."""
    for t in iter_tasks(paths.tasks_dir):
        if t.status == "claimed":
            set_status(t.path, "todo")

    if sweep_in_progress:
        index = task_index(paths.tasks_dir)
        if paths.sessions_dir.is_dir():
            for sdir in sorted(paths.sessions_dir.iterdir()):
                if not sdir.is_dir():
                    continue
                if (sdir / "reaped.json").exists():
                    continue
                kind = _classify_sentinel(sdir)
                if kind == "reapable":
                    continue  # a finished session — the reaper handles it, not us
                meta = _read_json(sdir / "meta.json")
                if not meta:
                    continue
                tid = str(meta.get("task", ""))
                task = index.get(tid)
                if task and task.status == "in-progress":
                    reason = ("result.json present but could not be parsed as a JSON object"
                              if kind == "corrupt"
                              else "no result.json was written")
                    # Reclaim the crashed run's detached process tree BEFORE blocking — so the
                    # orphan is dead before any later reap touches its worktree.
                    pgid = meta.get("pgid")
                    if _kill_orphan_group(pgid):
                        warn(f"killed orphaned process group {pgid} for {tid}")
                    # A crash/env-kill leaves the SAME state a deadline kill does: a session
                    # killed before it could write result.json, its worktree unverified and
                    # possibly partial. Recover it exactly like a timeout — a synthetic TIMEOUT
                    # sentinel the reaper re-queues (force-deleting the partial branch so the
                    # retry re-forks from base, bounded by timeout_max_retries) or, once the
                    # budget is spent, leaves terminal `timeout`. base_leak_commits still runs
                    # first at reap for every status, so a worktree escape onto base is blocked
                    # (and, under auto-merge, halts), not silently retried. We recover BOTH the
                    # absent- and corrupt-sentinel cases: a crashed run can't tell a self-exit
                    # from an env-kill, so retryable-and-budget-bounded is the safe default
                    # (mirrors _harvest, where a killed-without-result session is TIMEOUT).
                    warn(f"orphaned in-progress session {sdir.name}; recovering {tid} as TIMEOUT")
                    write_sentinel_if_absent(sdir, tid, "TIMEOUT",
                                             f"orphaned: run restarted while session was in-progress; {reason}")
    prune_worktrees(paths)


def reap(paths: Paths, config: Config) -> None:
    """One-shot reap. If the run lock is free (no active run) we take it and do the
    full crash-recovery reconcile, exactly as a fresh `run` would. If a run holds
    it, we skip the in-progress sweep entirely and only collect finished sentinels
    — idempotent result.json handling that never touches the live run's sessions."""
    paths.ensure_runtime_dirs()
    try:
        with run_lock(paths.run_lock):
            reconcile(paths, sweep_in_progress=True)
            reap_all(config, paths)
    except RunLockHeld:
        warn("an 'autobuild run' is active (holds run.lock); collecting finished "
             "sessions only, leaving its in-progress sessions alone")
        reap_all(config, paths)
    status(paths, config)


# --- outer loop --------------------------------------------------------------

def _active(running: list[RunningSession]) -> list[RunningSession]:
    return [rs for rs in running if not rs.has_result() and rs.alive()]


def _harvest(running: list[RunningSession], config: Config, paths: Paths) -> list[RunningSession]:
    """Reap finished sessions, BLOCK exited-without-result ones, and return the
    still-running survivors.

    A session is never dropped from tracking without first being reaped or stalled.
    This closes the race where a session's process exits in the window between a
    bare `reap_stalled` and a liveness filter: such a session used to be silently
    dropped, leaving a valid result.json unreaped (task stuck in-progress forever).
    """
    survivors: list[RunningSession] = []
    # Snapshot `now` once: a kill can block up to `grace`, so a session that crosses its
    # deadline DURING an earlier kill is caught on the next pass — fine, deadlines (>=1800s)
    # are coarse vs grace (<=10s). Deliberate; do not recompute per session.
    now = time.monotonic()
    for rs in running:
        kind = _classify_sentinel(rs.sdir)
        if kind == "reapable":
            continue  # (1) a valid sentinel beats everything (even the deadline) — reap_all
            #             handles it (even if still alive, so a session that writes its
            #             result then lingers can't hang)
        if rs.alive():
            if rs.deadline is not None and now >= rs.deadline:
                # (3) past deadline, still running, no usable result — kill, then
                # RE-CLASSIFY: a result written during the grace window must still win.
                warn(f"{rs.sid} exceeded its deadline; killing {rs.tid}")
                _kill_group(rs, config.kill_grace_seconds)
                if _classify_sentinel(rs.sdir) == "reapable":
                    continue  # finished during grace — reap_all takes the COMPLETE
                # Note the deliberate asymmetry: an absent/corrupt result under a *killed*
                # session is TIMEOUT (retryable), whereas the same shape from a process
                # that exited ON ITS OWN (the branch below) is terminal BLOCKED — a killed
                # agent's torn/missing write shouldn't be held against it as a hard failure.
                write_sentinel_if_absent(
                    rs.sdir, rs.tid, "TIMEOUT",
                    f"session exceeded task_timeout_seconds "
                    f"({config.task_timeout_seconds}s) and was killed")
                continue
            survivors.append(rs)  # (4) within deadline, still running — let it finish
            #                       (an absent or torn/mid-write sentinel under a live
            #                       process is never BLOCKED)
            continue
        # (2) Process exited without a usable result — BLOCK it (never silently drop).
        if kind == "corrupt":
            warn(f"{rs.sid} exited with an unparseable result.json; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "result.json present but could not be parsed as a JSON object")
        else:  # absent
            warn(f"{rs.sid} exited without a result; marking {rs.tid} BLOCKED")
            write_sentinel_if_absent(rs.sdir, rs.tid, "BLOCKED",
                                     "session process exited without writing result.json")
    reap_all(config, paths)
    return survivors


def _next_wait(running: list[RunningSession], sleep_seconds: float, now: float) -> float:
    """How long to sleep before the next harvest: the poll interval, but never past the
    nearest session deadline (so an over-deadline session is killed promptly), and never
    negative."""
    deadlines = [rs.deadline for rs in running if rs.deadline is not None]
    if not deadlines:
        return sleep_seconds
    return max(0.0, min(sleep_seconds, min(deadlines) - now))


def _wait_until_next_event(running: list[RunningSession], sleep_seconds: float) -> None:
    """Block until the next deadline-bounded poll tick, waking early if a child exits.
    Invariant: every entry in `running` has a live `proc` (proc-less handles are reaped/
    blocked by `_harvest` before we get here), so this never tight-spins on `wait == 0`."""
    wait = _next_wait(running, sleep_seconds, time.monotonic())
    for rs in running:
        if rs.proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                rs.proc.wait(timeout=wait)
            return
    time.sleep(wait)


def run(paths: Paths, config: Config, *, sleep_seconds: float = 2.0,
        monotonic=time.monotonic) -> None:
    """Drive the outer loop until the backlog drains. Holds the run lock for the
    whole lifetime: a second `run` is refused (RunLockHeld) and a `reap` running
    alongside this one cannot reconcile the sessions this run owns. Because we hold
    the lock, the startup reconcile is free to recover genuinely orphaned sessions
    (re-queue them as a synthetic TIMEOUT, or block them).

    `monotonic` is the wall-clock source for the optional run budget; injectable so
    tests can jump past the deadline without sleeping (mirrors `sleep_seconds`)."""
    _assert_not_nested()  # refuse a run nested inside a spawned session, before any lock
    with run_lock(paths.run_lock):
        _run_locked(paths, config, sleep_seconds=sleep_seconds, monotonic=monotonic)


def _assert_base_clean(paths: Paths) -> None:
    """Refuse to start if the base working tree has uncommitted source — an escaped
    session's `git add -A` could otherwise sweep it into a task commit (exactly the
    blast-radius amplifier seen in the field). Override with AUTOBUILD_ALLOW_DIRTY_BASE=1
    for the rare case where that risk is understood and accepted."""
    if os.environ.get("AUTOBUILD_ALLOW_DIRTY_BASE") == "1":
        return
    dirty = dirty_base_paths(paths)
    if dirty:
        shown = ", ".join(dirty[:5]) + (f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else "")
        raise DirtyBaseTree(
            f"base working tree has {len(dirty)} uncommitted change(s): {shown}. "
            f"Commit or stash them first — a session that escapes its worktree could "
            f"sweep them into a task commit. Set AUTOBUILD_ALLOW_DIRTY_BASE=1 to override.")


def _run_locked(paths: Paths, config: Config, *, sleep_seconds: float,
                monotonic=time.monotonic) -> None:
    paths.ensure_runtime_dirs()
    _assert_base_clean(paths)
    # Critical preflight (claude on PATH, git identity) BEFORE claiming or spawning, so a
    # misconfigured host aborts early with one clear message instead of N wasted sessions.
    # Imported lazily: preflight imports loop's helpers, so a top-level import would cycle.
    from .preflight import assert_run_preflight
    assert_run_preflight(paths, config)
    budget = (f", run_budget={config.run_budget_seconds}s"
              if config.run_budget_seconds > 0 else "")
    if config.run_budget_usd > 0:
        budget += f", run_budget=${config.run_budget_usd:.2f}"
    log(f"starting loop (max_parallel={config.max_parallel}, "
        f"max_iterations={config.max_iterations}{budget})")
    reconcile(paths, sweep_in_progress=True)

    running: list[RunningSession] = []
    try:
        reason = _supervise(paths, config, running, sleep_seconds=sleep_seconds,
                            monotonic=monotonic)
    except BaseBranchLeak as exc:
        # base is corrupt (a session escaped onto it): stop now and KILL any sessions
        # still running — they'd only burn tokens on a doomed run, and the same leak
        # check would refuse to integrate them anyway. Leave a summary (reason `halted`)
        # so even an aborted run is legible, notify the operator of the halt, then
        # re-raise for the CLI to report. `running` is mutated in place by _supervise,
        # so it holds the live set.
        for rs in running:
            _kill_group(rs, config.kill_grace_seconds)
        _finish_run(paths, config, "halted")
        _notify(config, "halt", str(exc))
        raise
    _finish_run(paths, config, reason)
    status(paths, config)


def _run_spend(paths: Paths, sids: set[str], cache: dict[str, float]) -> float:
    """Cumulative USD spend across the sessions THIS run spawned (`sids`) — the sum of each
    session's captured cost (`total_cost_usd` from its stream-json `result` event).

    Scoped to `sids`, NOT every session dir on disk: session dirs persist after reaping
    (only `clean` removes them), so summing all of them would pre-charge a fresh `run` with
    a prior run's spend and make a budget-capped backlog un-resumable. Counting only this
    run's own sessions keeps the budget per-run (consistent with run_budget_seconds) while
    still totalling a single run's real spend, retries included.

    `cache` memoizes FINISHED sessions, whose cost can never change again, so each pass only
    re-reads this run's still-running sessions (≤ max_parallel small files). A session with
    no `result` event yet — running, killed, or crashed — contributes 0; its cost is unknown,
    never guessed."""
    total = 0.0
    for sid in sids:
        if sid in cache:
            total += cache[sid]
            continue
        prog = read_progress(paths.sessions_dir / sid / "session.out")
        cost = prog.cost_usd or 0.0
        if prog.finished:
            cache[sid] = cost  # frozen: a finished session's cost is immutable
        total += cost
    return total


def _supervise(paths: Paths, config: Config, running: list[RunningSession], *,
               sleep_seconds: float, monotonic=time.monotonic) -> str:
    """The scheduling loop. Appends spawned sessions to `running` and keeps it in sync
    with the live set IN PLACE (`running[:] = ...`) so a caller catching BaseBranchLeak
    can still see — and kill — what was running when the halt fired.

    Returns the terminal reason the run ended for — `drained` (backlog empty),
    `settled` (only blocked/unsatisfiable work left), or one of the budget caps with work
    left unstarted: `max_iterations`, `run_budget_seconds`, `run_budget_usd` — so the caller
    records it in the run summary instead of re-deriving it. (The `halted` reason is supplied
    by the BaseBranchLeak handler.)"""
    iteration = 0
    # Optional whole-run WALL-CLOCK budget: a monotonic deadline computed ONCE here (not per
    # poll tick — same reasoning as max_iterations below: a long fleet of poll ticks must not
    # be what trips the cap). Past it, we fold into `over_budget` so the SAME drain-and-report
    # path handles it — stop claiming, drain in-flight, report — never forking the termination
    # logic. `monotonic` is injectable so tests jump past the deadline without sleeping. Like
    # the per-session deadlines, this clock is in-memory: a killed/resumed run does NOT resume
    # it (it restarts the budget), which is fine and consistent.
    budget_deadline = (monotonic() + config.run_budget_seconds
                       if config.run_budget_seconds > 0 else None)
    # Optional whole-run COST budget (issue #41): the financial complement to the wall-clock
    # one. Cost is only known once a session finishes (its stream-json `result` event), so it
    # can't preempt a running session mid-flight — instead, like the others, it folds into
    # `over_budget` to stop CLAIMING new work and drain. `run_sids` is the set of sessions
    # THIS run spawned (so the budget is per-run, not pre-charged by persisted prior-run
    # session dirs); `cost_cache` freezes finished-session costs so each pass only re-reads
    # the in-flight ones.
    budget_usd = config.run_budget_usd if config.run_budget_usd > 0 else None
    run_sids: set[str] = set()
    cost_cache: dict[str, float] = {}
    while True:
        running[:] = _harvest(running, config, paths)

        free = config.max_parallel - len(running)
        # max_iterations bounds SCHEDULING ROUNDS (passes that start new work), not poll
        # ticks: a session that spans many _wait_until_next_event polls must not exhaust
        # the budget — a long session is bounded by its own task_timeout_seconds. So only a
        # pass that actually claims work counts against the cap, and once it's spent we stop
        # claiming but keep draining what's already running (never abandoning in-flight work).
        # (Counting every pass made the cap a ~max_iterations*sleep_seconds wall-clock limit
        # that tripped mid-run and stranded finished-but-unreaped work as in-progress.)
        over_iterations = iteration >= config.max_iterations
        # The wall-clock budget is, by contrast, deliberately elapsed-time based.
        over_time = budget_deadline is not None and monotonic() >= budget_deadline
        # The cost budget is real-spend based: sum this run's session costs (computed once
        # per pass and reused below, only when a budget is set so the disabled case does no
        # I/O). `>=` so the cap is a ceiling. In-flight sessions read 0 until they finish, so
        # actual spend can overshoot by up to ~max_parallel * a session's cost (soft budget,
        # same posture as run_budget_seconds).
        spent_usd = _run_spend(paths, run_sids, cost_cache) if budget_usd is not None else 0.0
        over_cost = budget_usd is not None and spent_usd >= budget_usd
        over_budget = over_iterations or over_time or over_cost
        claimed = claim_tasks(free, paths) if (free > 0 and not over_budget) else []
        if claimed:
            iteration += 1
        for t in claimed:
            rs = spawn_session(t, config, paths)
            run_sids.add(rs.sid)  # count this session against the run's cost budget
            log(f"spawn {rs.sid} -> {rs.tid}")
            running.append(rs)

        # Final harvest before deciding the backlog is settled: a session that
        # finishes in this window must be reaped here, not silently stranded.
        running[:] = _harvest(running, config, paths)

        if not running:
            tasks = iter_tasks(paths.tasks_dir)
            index = {t.id: t for t in tasks}
            # A reap in this pass can create newly-runnable work with nothing left running
            # to trigger the next claim — most notably a timeout RE-QUEUE flipping a task
            # back to `todo`. Loop back and claim it instead of settling prematurely (which
            # would strand the retry). Only while we're still allowed to start work: over
            # budget we stop claiming and report below.
            if not over_budget and runnable_tasks(tasks, index):
                continue
            pending = [t for t in tasks if not is_terminal(t.status)]
            if over_budget and pending:
                # Budget spent with work still left: report the cap accurately instead of
                # claiming a clean drain. In-flight sessions were drained above, so the only
                # thing unfinished is work we declined to start. Name WHICH cap tripped so a
                # human knows what to raise — the wall-clock budget takes precedence (the hard
                # real-time wall; raise run_budget_seconds), then the cost ceiling (raise
                # run_budget_usd), then the iteration cap — and return that reason so the run
                # summary records which cap ended the run.
                if over_time:
                    warn(f"hit run_budget_seconds ({config.run_budget_seconds}s); stopping "
                         f"with {len(pending)} unfinished task(s) — see 'autobuild status'")
                    return "run_budget_seconds"
                if over_cost:
                    warn(f"hit run_budget_usd (${config.run_budget_usd:.2f}, spent "
                         f"${spent_usd:.2f}); stopping with {len(pending)} unfinished "
                         f"task(s) — see 'autobuild status'")
                    return "run_budget_usd"
                warn(f"hit max_iterations ({config.max_iterations}); stopping with "
                     f"{len(pending)} unfinished task(s) — see 'autobuild status'")
                return "max_iterations"
            elif not pending:
                ok("backlog drained — COMPLETE")
                return "drained"
            else:
                stuck = stuck_tasks(tasks, index)
                if stuck:
                    warn(f"backlog settled: {len(stuck)} task(s) cannot proceed:")
                    for tid in sorted(stuck):
                        warn(f"  {tid}: {stuck[tid]}")
                    other = len(pending) - len(stuck)
                    if other:
                        warn(f"  ...plus {other} other unfinished task(s) — see 'autobuild status'")
                else:
                    warn(f"backlog settled with {len(pending)} unfinished task(s) "
                         f"— see 'autobuild status'")
                return "settled"

        if not claimed:
            # No new work to start this pass (at capacity, or nothing runnable yet).
            # Block until a running session finishes rather than busy-spinning. If a
            # reap had unblocked a task, claim_tasks above would have claimed it.
            _wait_until_next_event(running, sleep_seconds)


# --- status ------------------------------------------------------------------

def collect_status(paths: Paths) -> dict:
    tasks = iter_tasks(paths.tasks_dir)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    stuck = stuck_tasks(tasks, {t.id: t for t in tasks})
    stuck_list = [{"task": tid, "reason": stuck[tid]} for tid in sorted(stuck)]

    sessions = []
    if paths.sessions_dir.is_dir():
        for sdir in sorted(paths.sessions_dir.iterdir()):
            if not sdir.is_dir():
                continue
            result = _read_json(sdir / "result.json")
            state = result.get("status") if result else "pending"
            if (sdir / "reaped.json").exists():
                state = f"{state} (reaped)"
            # Live per-session progress (issue #40), parsed from the stream-json session.out:
            # the assistant-message count and the final result event's cost. Idle time comes
            # free from the file's mtime (its last write = the agent's last activity); None
            # when the session hasn't emitted anything yet.
            out_file = sdir / "session.out"
            prog = read_progress(out_file)
            try:
                idle = max(0.0, time.time() - out_file.stat().st_mtime)
            except OSError:
                idle = None
            sessions.append({"session": sdir.name, "state": state,
                             "messages": prog.messages, "cost_usd": prog.cost_usd,
                             "idle_seconds": idle})

    wt = _git(paths.root, "worktree", "list")
    worktrees = [ln for ln in wt.stdout.splitlines() if "/.autobuild/worktrees/" in ln]
    br = _git(paths.root, "branch", "--list", "autobuild/*")
    branches = [ln.strip("* ").strip() for ln in br.stdout.splitlines() if ln.strip()]

    return {"counts": counts, "tasks": tasks, "sessions": sessions,
            "worktrees": worktrees, "branches": branches, "stuck": stuck_list}


def status(paths: Paths, config: Config) -> dict:
    report = collect_status(paths)
    print(f"\n{_c('1;37')}TASKS BY STATE{_c('0')}")
    if report["counts"]:
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    else:
        print("  (none)")

    print(f"\n{_c('1;37')}TASKS{_c('0')}")
    for t in report["tasks"]:
        print(f"  {t.id:<12} p{t.priority:<3} {t.status:<12} {t.title}")

    if report["stuck"]:
        print(f"\n{_c('1;31')}STUCK{_c('0')}")
        for s in report["stuck"]:
            print(f"  {s['task']:<12} {s['reason']}")

    print(f"\n{_c('1;37')}SESSIONS{_c('0')}")
    for s in report["sessions"]:
        bits = f"msgs={s['messages']}"
        if s.get("cost_usd") is not None:
            bits += f"  cost=${s['cost_usd']:.4f}"
        # idle is only meaningful for a still-live session; a reaped one isn't "idle".
        if s.get("idle_seconds") is not None and "(reaped)" not in s["state"]:
            bits += f"  idle={s['idle_seconds']:.0f}s"
        print(f"  {s['session']:<34} {s['state']:<18} {bits}")
    if not report["sessions"]:
        print("  (none)")

    print(f"\n{_c('1;37')}ACTIVE WORKTREES / BRANCHES{_c('0')}")
    print(f"  worktrees: {len(report['worktrees'])}   branches: {len(report['branches'])}")
    for b in report["branches"]:
        print(f"    {b}")
    print()
    return report


# --- run summary -------------------------------------------------------------

def _parse_iso(ts: object) -> datetime | None:
    """Parse an autobuild timestamp (`_now()`'s `%Y-%m-%dT%H:%M:%SZ`, UTC) back to an
    aware datetime, or None for anything unparseable — so a torn/missing timestamp
    just drops the duration rather than crashing the summary."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _duration_seconds(started: object, reaped_at: object) -> float | None:
    """Best-effort wall-time between a session's `started` (meta.json) and `reaped_at`
    (reaped.json). None when either timestamp is missing/unparseable or the clock ran
    backwards — we omit a duration we can't trust rather than guess one."""
    a, b = _parse_iso(started), _parse_iso(reaped_at)
    if a is None or b is None:
        return None
    secs = (b - a).total_seconds()
    return secs if secs >= 0 else None


def _session_outcomes(paths: Paths) -> dict[str, dict]:
    """Map task-id -> its latest session's integration outcome, harvested from the
    per-session forensics (`meta.json` + `reaped.json`, or `leak.json` when the session
    halted the run before it could be reaped). A task with several sessions (timeout
    retries / re-runs) reports its most recent one; sessions sort chronologically by id,
    so a later entry overwrites an earlier. Sessions with neither a reaped.json nor a
    leak.json contribute nothing — they have no terminal outcome yet."""
    outcomes: dict[str, dict] = {}
    if not paths.sessions_dir.is_dir():
        return outcomes
    for sdir in sorted(paths.sessions_dir.iterdir()):
        if not sdir.is_dir():
            continue
        meta = _read_json(sdir / "meta.json") or {}
        reaped = _read_json(sdir / "reaped.json")
        if reaped is not None:
            tid = str(meta.get("task", "")) or str(reaped.get("task", ""))
            outcome = {
                "session": sdir.name,
                "result": reaped.get("status"),
                "integrated": bool(reaped.get("integrated", False)),
                "checks": reaped.get("checks", "n/a"),
                "requeued": bool(reaped.get("requeued", False)),
                "followups": list(reaped.get("followups") or []),
            }
            leak = reaped.get("leak")
            if leak:
                outcome["leak"] = list(leak)
            duration = _duration_seconds(meta.get("started"), reaped.get("reaped_at"))
        else:
            # The auto-merge base-leak halt blocks the task and writes leak.json but —
            # deliberately — NO reaped.json (so a re-run keeps flagging the corrupt base).
            # Surface that session's outcome from leak.json so a halted run is still legible.
            leak = _read_json(sdir / "leak.json")
            if leak is None:
                continue  # no terminal outcome yet — skip
            tid = str(meta.get("task", "")) or str(leak.get("task", ""))
            outcome = {
                "session": sdir.name,
                "result": None,
                "integrated": False,
                "checks": "n/a",
                "requeued": False,
                "followups": [],
                "leak": list(leak.get("commits") or []),
            }
            duration = _duration_seconds(meta.get("started"), leak.get("detected_at"))
        if not tid:
            continue
        if duration is not None:
            outcome["duration_seconds"] = duration
        # The session's cost (issue #41): the total_cost_usd from its stream-json result
        # event, or None if it never emitted one (killed/crashed before finishing).
        outcome["cost_usd"] = read_progress(sdir / "session.out").cost_usd
        outcomes[tid] = outcome
    return outcomes


def write_run_summary(paths: Paths, config: Config, reason: str) -> dict:
    """Build and atomically write `.autobuild/run-summary.json` — the end-of-run digest of
    what the run did. Reuses `collect_status` for counts/tasks/stuck and layers each task's
    per-session integration outcome (from reaped.json), follow-ups filed, timeout-retry
    attempts (from the retries ledger), and best-effort wall-time on top. Returns the plain
    dict it wrote (also the message body the notifications hook formats)."""
    report = collect_status(paths)
    outcomes = _session_outcomes(paths)
    rows = []
    for t in report["tasks"]:
        out = outcomes.get(t.id)
        row = {
            "id": t.id,
            "status": t.status,
            "attempts": retry_count(paths.retries_ledger, t.id),
            "integration": out,
            "followups": list(out["followups"]) if out else [],
            # this task's latest session cost (issue #41); None if unknown/not finished
            "cost_usd": out.get("cost_usd") if out else None,
        }
        rows.append(row)
    # Project total = sum of the per-task row costs, so total == sum(rows) by construction
    # (no disagreement between the headline figure and the rows that compose it).
    total_cost = sum(r["cost_usd"] for r in rows if r["cost_usd"])
    summary = {
        "generated_at": _now(),
        "reason": reason,
        "counts": report["counts"],
        "total_cost_usd": total_cost,
        "tasks": rows,
        "stuck": report["stuck"],
    }
    _atomic_write_json(paths.run_summary, summary)
    return summary


def _print_run_digest(summary: dict) -> None:
    """Print the short at-a-glance digest (the JSON is the complete record). One line for
    the reason, one for the counts, and one per blocked/timed-out task with its reason."""
    log(f"run ended: {summary['reason']}")
    counts = summary["counts"]
    if counts:
        log("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    stuck = {s["task"]: s["reason"] for s in summary["stuck"]}
    for row in summary["tasks"]:
        if row["status"] not in ("blocked", "timeout"):
            continue
        warn(f"  {row['id']} {row['status']}: {_task_digest_reason(row, stuck)}")


def _task_digest_reason(row: dict, stuck: dict[str, str]) -> str:
    """A one-line 'why' for a blocked/timed-out task in the digest: prefer the
    integration forensics (leak / failed checks / reported result), then fall back to
    the stuck-dependency reason, then the bare status."""
    bits: list[str] = []
    integ = row.get("integration")
    if integ:
        if integ.get("leak"):
            bits.append("base-leak")
        elif str(integ.get("checks", "")).startswith("failed"):
            bits.append(str(integ["checks"]))
        elif integ.get("result"):
            bits.append(str(integ["result"]))
    if row["id"] in stuck:
        bits.append(stuck[row["id"]])
    return "; ".join(bits) or row["status"]


def _run_end_message(summary: dict) -> str:
    """The `done`-event notification body, built from the run summary: the terminal reason
    plus the per-status counts, so the operator learns what merged / blocked / timed out
    without opening run-summary.json."""
    counts = summary.get("counts") or {}
    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no tasks"
    return f"run ended ({summary.get('reason', '?')}): {counts_str}"


def _finish_run(paths: Paths, config: Config, reason: str) -> None:
    """Write the run summary, print its digest, and notify the operator of the run end —
    all best-effort. A failure here must never mask the run's real outcome (or a re-raised
    BaseBranchLeak halt), so any error is warned and swallowed rather than propagated.

    The `done` notification fires for every non-halted end (drained / settled /
    max_iterations); the `halt` end gets its own `halt` event from the BaseBranchLeak
    handler, so we suppress the `done` event here to avoid a double notification."""
    summary = None
    try:
        summary = write_run_summary(paths, config, reason)
        _print_run_digest(summary)
    except Exception as exc:  # noqa: BLE001 — summary is advisory; never let it break a run
        warn(f"could not write run summary: {exc}")
    if reason != "halted" and summary is not None:
        _notify(config, "done", _run_end_message(summary))


# --- clean -------------------------------------------------------------------

def clean(paths: Paths) -> None:
    """Remove reaped sessions and prune detached worktrees — but only when no run is
    active. Taking the run lock is what makes deletion safe: without it, clean could
    rmtree a live session's worktree or a COMPLETE-but-unreaped session (losing the
    result). Refuses (and warns) rather than racing a live run."""
    paths.ensure_runtime_dirs()
    try:
        with run_lock(paths.run_lock):
            _clean_locked(paths)
    except RunLockHeld:
        warn("an 'autobuild run' is active (holds run.lock); skipping clean so it can't "
             "delete live sessions or prune their worktrees")


def _clean_locked(paths: Paths) -> None:
    log("cleaning reaped sessions and pruning detached worktrees")
    prune_worktrees(paths)
    if paths.sessions_dir.is_dir():
        import shutil
        for sdir in sorted(paths.sessions_dir.iterdir()):
            # Only `reaped.json` means truly finished. A bare `result.json` can be a
            # COMPLETE session the reaper hasn't integrated yet — deleting it loses work.
            if sdir.is_dir() and (sdir / "reaped.json").exists():
                shutil.rmtree(sdir, ignore_errors=True)
    ok("clean")
