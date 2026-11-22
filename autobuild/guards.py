"""Run guards: worktree-isolation checks (base-leak detection, dirty-base refusal), the
single-supervisor run lock, and the nested-run refusal."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

from .common import _git
from .paths import Paths

class RunHalted(RuntimeError):
    """Base for every condition that HALTS a run (and a one-shot `reap`): the supervisor
    kills live sessions, writes a `halted` summary, notifies, and re-raises; the CLI maps
    it to exit 2. str() is the operator message."""


class BaseBranchNotCheckedOut(RunHalted):
    """auto-merge found the main checkout on a branch other than base_branch (or on a
    detached HEAD). Raised before the merge, so nothing moved and the session stays
    reapable once base_branch is checked out again."""


class MergeUndoFailed(RunHalted):
    """A post-merge check failed and autobuild could not undo the merge: base is left on
    the merge commit. Raised before the task is marked or reaped.json is written."""


class BaseBranchLeak(RunHalted):
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
