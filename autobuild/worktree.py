"""Worktree management — each session gets its own isolated checkout + branch.
Ports worktree.sh."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .paths import Paths


def branch_name(tid: str) -> str:
    return f"autobuild/{tid}"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    )


def _branch_exists(root: Path, branch: str) -> bool:
    return _git(root, "show-ref", "--verify", "--quiet",
                f"refs/heads/{branch}", check=False).returncode == 0


def make_worktree(paths: Paths, sid: str, tid: str, base_branch: str) -> Path:
    """Create a worktree for the session on branch autobuild/<tid>, forking from
    base_branch. Reuse the branch if it already exists (a retry). Raises on failure."""
    branch = branch_name(tid)
    path = paths.worktrees_dir / sid
    paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    if _branch_exists(paths.root, branch):
        _git(paths.root, "worktree", "add", "-q", str(path), branch)
    else:
        _git(paths.root, "worktree", "add", "-q", "-b", branch, str(path), base_branch)
    return path


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
