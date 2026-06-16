import pytest

from autobuild.paths import Paths
from autobuild.worktree import branch_name, make_worktree, prune_worktrees, remove_worktree


def test_make_worktree_creates_branch_from_base(git_repo, git):
    paths = Paths(git_repo)
    wt = make_worktree(paths, "sess-1", "task-001", "main")
    assert wt.is_dir()
    assert wt == paths.worktrees_dir / "sess-1"
    # branch exists and the worktree is a real checkout of the base commit
    assert git(git_repo, "show-ref", "--verify", "--quiet",
               f"refs/heads/{branch_name('task-001')}", check=False).returncode == 0
    assert (wt / ".git").exists()


def test_make_worktree_reuses_existing_branch(git_repo):
    paths = Paths(git_repo)
    make_worktree(paths, "sess-1", "task-001", "main")
    remove_worktree(paths, "sess-1")  # branch survives the worktree removal
    # second attempt (a retry) must reuse the branch, not fail trying to recreate it
    wt2 = make_worktree(paths, "sess-2", "task-001", "main")
    assert wt2.is_dir()


def test_make_worktree_raises_when_base_missing(git_repo):
    paths = Paths(git_repo)
    with pytest.raises(Exception):
        make_worktree(paths, "sess-1", "task-001", "does-not-exist")


def test_remove_worktree_keeps_branch_and_commits(git_repo, git):
    paths = Paths(git_repo)
    make_worktree(paths, "sess-1", "task-001", "main")
    remove_worktree(paths, "sess-1")
    assert not (paths.worktrees_dir / "sess-1").exists()
    assert git(git_repo, "show-ref", "--verify", "--quiet",
               f"refs/heads/{branch_name('task-001')}", check=False).returncode == 0


def test_remove_missing_worktree_is_noop(git_repo):
    paths = Paths(git_repo)
    remove_worktree(paths, "never-made")  # must not raise


def test_prune_worktrees_runs(git_repo):
    paths = Paths(git_repo)
    make_worktree(paths, "sess-1", "task-001", "main")
    # nuke the dir out from under git, then prune should reconcile without error
    import shutil
    shutil.rmtree(paths.worktrees_dir / "sess-1")
    prune_worktrees(paths)
