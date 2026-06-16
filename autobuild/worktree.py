"""Worktree management — each session gets its own isolated checkout + branch.
Ports worktree.sh."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .paths import Paths


class DependencyMergeConflict(RuntimeError):
    """A dependency's autobuild/<dep> branch could not be merged into the new
    worktree (merge conflict). The merge is aborted before this is raised, so the
    worktree is left with no half-merged state."""


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
            _git(worktree, "merge", "--abort", check=False)  # leave no half-merged tree
            detail = (r.stdout + r.stderr).strip().splitlines()
            hint = detail[-1].strip() if detail else "merge conflict"
            raise DependencyMergeConflict(
                f"dependency {dep} ({dep_branch}) conflicts with {branch_name(tid)} — "
                f"cannot build a base for {tid}: {hint}")


def remove_worktree(paths: Paths, sid: str) -> None:
    """Detach the worktree, keeping its branch + commits."""
    path = paths.worktrees_dir / sid
    if not path.exists():
        return
    r = _git(paths.root, "worktree", "remove", "--force", str(path), check=False)
    if r.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)


def prune_worktrees(paths: Paths) -> None:
    _git(paths.root, "worktree", "prune", check=False)
