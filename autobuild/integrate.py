"""Integration of a verified task branch: pr (push + gh pr create, with bounded
retry-with-backoff), auto-merge (with the post-merge combined-tree verification), or
branch."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from shutil import which

from .common import _gh, _git, current_branch, warn
from .config import Config
from .guards import BaseBranchNotCheckedOut, MergeUndoFailed
from .paths import Paths
from .verify import _write_checks_log
from .worktree import branch_name, remove_worktree

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
    no remote, merge conflict) stay single-shot. `sleep` is injectable for tests.

    auto-merge raises BaseBranchNotCheckedOut when the main checkout is not on base_branch,
    and MergeUndoFailed when a failed post-merge check cannot be undone. Both are raised
    before the reaper marks the task or writes reaped.json, so the session stays reapable."""
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
        # `git merge` targets whatever is checked out; merging onto another branch would
        # mark the task done while base never moves. Halt instead (a human ran checkout).
        head = current_branch(root)
        if head != base:
            raise BaseBranchNotCheckedOut(
                f"auto-merge merges into the checked-out branch; the main checkout is on "
                f"{head or 'a detached HEAD'}, not {base} — `git checkout {base}` and re-run "
                f"(the finished session is kept and merges on the next run)")
        # Snapshot base BEFORE the merge so a failed post-merge verification can undo it
        # precisely (simpler and more local than re-reading meta.json's base_sha).
        pre_merge_sha = _git(root, "rev-parse", f"refs/heads/{base}").stdout.strip()
        r = _git(root, "merge", "--no-ff", "-m", f"autobuild: merge {tid}", branch)
        if r.returncode != 0:
            _git(root, "merge", "--abort")  # don't leave the repo mid-merge
            return False, f"auto-merge conflict for {branch}"
        return _post_merge_verify(tid, branch, config, paths, sdir, pre_merge_sha)

    return False, f"unknown integration mode '{mode}'"


def _post_merge_verify(tid: str, branch: str, config: Config, paths: Paths, sdir: Path,
                       pre_merge_sha: str) -> tuple[bool, str]:
    """After an auto-merge lands, re-run config.checks against the COMBINED tree. Two
    independently-green branches can still merge into a red base with no textual conflict
    (semantic skew: A renames a helper, B adds a caller of the old name); the pre-merge gate
    only ever saw each branch alone, so this is the only place that catches it.

    The checks run in a temporary detached worktree at the merge commit, never in the main
    checkout: files they generate (caches, build output) would otherwise make the base tree
    dirty and the next `run` refuse to start. On the first failing check, write a forensic
    log, `_undo_merge`, and report failure — the reaper then blocks the task and keeps the
    branch. Skipped (merge stands) when verify_after_merge is off or there are no checks;
    independent of verify_checks (which gates the pre-merge worktree run)."""
    base = config.base_branch
    landed = f"merged {branch} into {base}"
    if not config.verify_after_merge or not config.checks:
        return True, landed
    name = f"{sdir.name}-post-merge"
    remove_worktree(paths, name)  # a crash may have left one behind
    paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    tree = paths.worktrees_dir / name
    r = _git(paths.root, "worktree", "add", "--detach", str(tree), "HEAD")
    if r.returncode != 0:  # cannot verify the combined tree, so do not keep the merge
        _write_checks_log(sdir, "(git worktree add)", r.returncode, r.stdout, r.stderr,
                          name="post-merge-checks.log")
        _undo_merge(paths, pre_merge_sha)
        return False, f"post-merge checks could not run for {branch}; merge reverted"
    try:
        for cmd in config.checks:
            r = subprocess.run(cmd, shell=True, cwd=str(tree), capture_output=True, text=True)
            if r.returncode != 0:
                _write_checks_log(sdir, cmd, r.returncode, r.stdout, r.stderr,
                                  name="post-merge-checks.log")
                _undo_merge(paths, pre_merge_sha)
                return False, f"post-merge checks failed for {branch}; merge reverted"
    finally:
        remove_worktree(paths, name)
    return True, landed


def _undo_merge(paths: Paths, pre_merge_sha: str) -> None:
    """Move base back to `pre_merge_sha` with `git reset --merge`, which keeps uncommitted
    work outside the merge (`reset --hard` would revert the uncommitted `tasks/` status
    writes and any human edit). It refuses only when the main checkout has a local edit to a
    path the merge touched; that edit is someone's work, so halt (MergeUndoFailed) rather
    than discard it."""
    r = _git(paths.root, "reset", "--merge", pre_merge_sha)
    if r.returncode != 0:
        merge_sha = _git(paths.root, "rev-parse", "HEAD").stdout.strip()
        raise MergeUndoFailed(
            f"could not undo merge {merge_sha} after failed post-merge checks: "
            f"{r.stderr.strip()} — base is left on the merge commit; run "
            f"`git reset --merge {pre_merge_sha}` by hand, then re-run")
