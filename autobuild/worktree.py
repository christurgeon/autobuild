"""Worktree management — each session gets its own isolated checkout + branch."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from .paths import Paths


class WorktreeError(RuntimeError):
    """Base for failures while building a session's worktree."""


class DependencyMergeConflict(WorktreeError):
    """A dependency's autobuild/<dep> branch could not be merged into the new
    worktree because of a *content conflict*. The merge is aborted before this is
    raised, so the worktree is left with no half-merged state."""


class DependencyMergeError(WorktreeError):
    """A dependency merge failed for a reason *other* than a content conflict — most
    commonly no committer identity configured (`git user.name` / `user.email`). Kept
    distinct from DependencyMergeConflict so the failure isn't misreported as a
    conflict and the message can be actionable. Aborted before raising."""


def branch_name(tid: str) -> str:
    return f"autobuild/{tid}"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    )


def _branch_exists(root: Path, branch: str) -> bool:
    return _git(root, "show-ref", "--verify", "--quiet",
                f"refs/heads/{branch}", check=False).returncode == 0


def make_worktree(paths: Paths, sid: str, tid: str, base_branch: str,
                  deps: Sequence[str] = ()) -> Path:
    """Create a worktree for the session on branch autobuild/<tid>, forking from
    base_branch (reusing the branch on a retry), then layer each dependency's
    autobuild/<dep> branch on top so a dependent task sees its dependencies' code in
    every integration mode — not just under auto-merge, where the dep already landed
    on base_branch. `deps` are dependency task ids in declaration (merge) order.

    Raises on worktree-creation failure, or DependencyMergeConflict if a dependency
    branch cannot be merged cleanly (aborted first, so no half-merged tree is left)."""
    branch = branch_name(tid)
    path = paths.worktrees_dir / sid
    paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    if _branch_exists(paths.root, branch):
        _git(paths.root, "worktree", "add", "-q", str(path), branch)
    else:
        _git(paths.root, "worktree", "add", "-q", "-b", branch, str(path), base_branch)
    _merge_dependencies(paths.root, path, tid, deps)
    return path


def _merge_dependencies(root: Path, worktree: Path, tid: str,
                        deps: Sequence[str]) -> None:
    """Merge each dependency's autobuild/<dep> branch into the worktree, in declaration
    order, with --no-ff. Skip a dependency whose branch is gone, or whose tip is already
    an ancestor of the worktree HEAD (it already landed on base_branch under auto-merge),
    so there is never a double-apply. A conflicting merge is aborted and re-raised as
    DependencyMergeConflict naming the dependency."""
    for dep in deps:
        dep_branch = branch_name(dep)
        if not _branch_exists(root, dep_branch):
            continue  # branch removed / never created -> nothing to layer
        tip = _git(root, "rev-parse", "--verify", f"refs/heads/{dep_branch}").stdout.strip()
        if _git(worktree, "merge-base", "--is-ancestor", tip, "HEAD",
                check=False).returncode == 0:
            continue  # already present in base_branch (auto-merge) -> no double-apply
        r = _git(worktree, "merge", "--no-ff", "-m",
                 f"autobuild: merge dependency {dep} into {tid}", dep_branch, check=False)
        if r.returncode != 0:
            # Distinguish a real content conflict (unmerged paths in the index) from a
            # merge that failed for another reason (e.g. unset committer identity).
            # Capture the unmerged paths BEFORE aborting, which clears them.
            unmerged = _git(worktree, "diff", "--name-only", "--diff-filter=U",
                            check=False).stdout.strip()
            _git(worktree, "merge", "--abort", check=False)  # leave no half-merged tree
            detail = (r.stdout + r.stderr).strip().splitlines()
            hint = detail[-1].strip() if detail else "git merge failed"
            if unmerged:
                raise DependencyMergeConflict(
                    f"dependency {dep} ({dep_branch}) conflicts with {branch_name(tid)} — "
                    f"cannot build a base for {tid}: {hint}")
            raise DependencyMergeError(
                f"merging dependency {dep} ({dep_branch}) into {branch_name(tid)} failed "
                f"without a content conflict — check git user.name / user.email is set: {hint}")


def remove_worktree(paths: Paths, sid: str) -> None:
    """Detach the worktree, keeping its branch + commits. `-f -f` (double-force) removes
    even a dirty/locked tree (and its admin dir) in one call — the common kill case. If
    that still fails (broken .git pointer / half-merge a SIGKILL'd agent left behind),
    fall back: abort any in-progress merge/rebase, clear the lock + stale index lock,
    rmtree the tree, and prune the now-dangling admin dir."""
    path = paths.worktrees_dir / sid
    admin = paths.root / ".git" / "worktrees" / sid
    if not path.exists() and not admin.exists():
        return
    r = _git(paths.root, "worktree", "remove", "--force", "--force", str(path), check=False)
    if r.returncode == 0:
        return
    if path.exists():
        _git(path, "merge", "--abort", check=False)
        _git(path, "rebase", "--abort", check=False)
    for stale in ("locked", "index.lock"):  # `locked` blocks both `remove` AND `prune`
        with suppress(FileNotFoundError):
            (admin / stale).unlink()
    shutil.rmtree(path, ignore_errors=True)
    _git(paths.root, "worktree", "prune", check=False)


def prune_worktrees(paths: Paths) -> None:
    _git(paths.root, "worktree", "prune", check=False)


def delete_branch(paths: Paths, tid: str, *, force: bool = False) -> bool:
    """Delete the task's autobuild/<tid> branch. Returns True iff the branch is gone
    afterward (deleted now, or already absent — idempotent).

    Two modes:

    - `force=False` (default) — SAFE delete (`git branch -d`): git refuses a branch not
      fully merged into the checked-out base_branch, so this returns False (branch kept)
      for an un-integrated deliverable. Called after an auto-merge integration, where the
      branch's commits already live on base_branch via the --no-ff merge commit, so the
      branch is redundant and would otherwise pile up. A still-pending dependent is fine:
      it forks from base_branch (which carries the dep) and _merge_dependencies skips the
      now-absent branch.

    - `force=True` — FORCE delete (`git branch -D`). The ONLY legitimate use is a timeout
      re-queue, where autobuild deliberately discards the killed session's incomplete,
      unverified partial branch so the retry re-forks fresh from base_branch. Never use
      `force=True` on a `pr`/`branch`-mode deliverable or a non-timeout task — it bypasses
      the merged-into-base safety check `-d` provides."""
    branch = branch_name(tid)
    if not _branch_exists(paths.root, branch):
        return True  # already gone — the desired end-state for both callers
    flag = "-D" if force else "-d"
    if _git(paths.root, "branch", flag, branch, check=False).returncode == 0:
        return True
    return not _branch_exists(paths.root, branch)
