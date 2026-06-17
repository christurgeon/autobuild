import pytest

from autobuild.paths import Paths
from autobuild.worktree import (
    DependencyMergeConflict,
    DependencyMergeError,
    branch_name,
    make_worktree,
    prune_worktrees,
    remove_worktree,
)


def _dep_branch(git, repo, tmp_path, tid, base, filename, content):
    """Create autobuild/<tid> from `base` with one commit adding `filename`, using a
    throwaway worktree so the setup never touches the branch under test."""
    branch = branch_name(tid)
    setup = tmp_path / f"setup-{tid}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(setup), base)
    (setup / filename).write_text(content)
    git(setup, "add", "-A")
    git(setup, "commit", "-q", "-m", f"{tid}: add {filename}")
    git(repo, "worktree", "remove", "--force", str(setup))
    return branch


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


# ---- dependency-aware base --------------------------------------------------

def test_make_worktree_merges_single_dependency(git_repo, git, tmp_path):
    paths = Paths(git_repo)
    _dep_branch(git, git_repo, tmp_path, "task-001", "main", "dep.txt", "from dep\n")

    wt = make_worktree(paths, "sess-d", "task-002", "main", ["task-001"])

    # the dependent worktree sees the dependency's committed file
    assert (wt / "dep.txt").read_text() == "from dep\n"
    # and it was layered via a real (--no-ff) merge commit
    log = git(wt, "log", "--oneline").stdout
    assert "merge dependency task-001" in log


def test_make_worktree_merges_multiple_dependencies_in_declaration_order(git_repo, git, tmp_path):
    paths = Paths(git_repo)
    _dep_branch(git, git_repo, tmp_path, "task-001", "main", "a.txt", "a\n")
    _dep_branch(git, git_repo, tmp_path, "task-002", "main", "b.txt", "b\n")

    wt = make_worktree(paths, "sess-fan", "task-003", "main", ["task-001", "task-002"])

    # fan-in: both dependencies' files are present
    assert (wt / "a.txt").read_text() == "a\n"
    assert (wt / "b.txt").read_text() == "b\n"
    # deterministic order: task-001 merged first (older), task-002 second (newer/top)
    log = git(wt, "log", "--oneline").stdout
    assert log.index("merge dependency task-002") < log.index("merge dependency task-001")


def test_make_worktree_skips_dependency_already_in_base(git_repo, git, tmp_path):
    """auto-merge case: the dep already landed on base_branch. Re-merging would be a
    no-op at best; we must detect it (is-ancestor) and add no extra merge commit."""
    paths = Paths(git_repo)
    _dep_branch(git, git_repo, tmp_path, "task-001", "main", "dep.txt", "from dep\n")
    git(git_repo, "merge", "--no-ff", "-m", "land task-001", branch_name("task-001"))
    base_tip = git(git_repo, "rev-parse", "main").stdout.strip()

    wt = make_worktree(paths, "sess-am", "task-002", "main", ["task-001"])

    assert (wt / "dep.txt").read_text() == "from dep\n"  # present via base
    # no double-apply: the new branch points exactly at base, no dependency merge commit
    assert git(wt, "rev-parse", "HEAD").stdout.strip() == base_tip


def test_make_worktree_skips_missing_dependency_branch(git_repo):
    """A dep whose autobuild/<dep> branch no longer exists is skipped, not an error."""
    paths = Paths(git_repo)
    wt = make_worktree(paths, "sess-miss", "task-002", "main", ["task-001"])
    assert wt.is_dir()


def test_make_worktree_layers_transitive_chain(git_repo, git, tmp_path):
    """A<-B<-C: B is built atop A, so merging only B into C still brings A's work."""
    paths = Paths(git_repo)
    _dep_branch(git, git_repo, tmp_path, "task-A", "main", "a.txt", "a\n")
    # build B on top of A (as a real dependent session would)
    wt_b = make_worktree(paths, "sess-b", "task-B", "main", ["task-A"])
    (wt_b / "b.txt").write_text("b\n")
    git(wt_b, "add", "-A")
    git(wt_b, "commit", "-q", "-m", "task-B: add b.txt")
    remove_worktree(paths, "sess-b")

    wt_c = make_worktree(paths, "sess-c", "task-C", "main", ["task-B"])

    assert (wt_c / "a.txt").read_text() == "a\n"  # transitively from A via B
    assert (wt_c / "b.txt").read_text() == "b\n"


def test_make_worktree_conflicting_dependency_raises_and_leaves_clean(git_repo, git, diverging_dep):
    paths = Paths(git_repo)
    diverging_dep(git_repo, "task-001")  # main and autobuild/task-001 conflict on shared.txt

    with pytest.raises(DependencyMergeConflict) as exc:
        make_worktree(paths, "sess-x", "task-002", "main", ["task-001"])
    assert "task-001" in str(exc.value)

    # no half-merged tree: the worktree is left with no merge in progress, clean
    wt = paths.worktrees_dir / "sess-x"
    assert git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode != 0
    assert git(wt, "status", "--porcelain").stdout.strip() == ""


def test_make_worktree_non_conflict_merge_failure_is_distinct(git_repo, git, tmp_path, monkeypatch):
    """A dependency merge that fails for a reason OTHER than a content conflict (e.g.
    no committer identity) must NOT be mislabeled a conflict: it raises the distinct
    DependencyMergeError with an actionable message, and still leaves a clean tree."""
    paths = Paths(git_repo)
    _dep_branch(git, git_repo, tmp_path, "task-001", "main", "dep.txt", "from dep\n")

    # strip committer identity so the --no-ff merge commit fails cleanly (no conflict).
    # useConfigOnly stops git from auto-deriving an identity from username@hostname.
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    git(git_repo, "config", "--unset", "user.name")
    git(git_repo, "config", "--unset", "user.email")
    git(git_repo, "config", "user.useConfigOnly", "true")

    with pytest.raises(DependencyMergeError) as exc:
        make_worktree(paths, "sess-x", "task-002", "main", ["task-001"])
    msg = str(exc.value)
    assert "task-001" in msg
    assert "identity" in msg.lower() or "user.name" in msg.lower()  # actionable

    wt = paths.worktrees_dir / "sess-x"
    assert git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode != 0
    assert git(wt, "status", "--porcelain").stdout.strip() == ""


def test_dependency_merge_errors_are_distinct_types():
    # DependencyMergeError must not be caught by `except DependencyMergeConflict`,
    # and vice versa — callers distinguish a content conflict from other failures.
    assert not issubclass(DependencyMergeError, DependencyMergeConflict)
    assert not issubclass(DependencyMergeConflict, DependencyMergeError)
