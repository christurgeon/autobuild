"""Worktree-isolation hardening: a spawned session must do its work in its own
worktree, never on base_branch. These cover the three defenses:

  1. leak DETECTION  — the harness catches an agent that committed onto base_branch
     and hard-fails loudly instead of integrating onto a base it no longer controls;
  2. anchor REMOVAL  — the prompt stages the contract into the session dir and points
     the agent only at the worktree, never at the main checkout's paths;
  3. dirty-base GUARD — `run` refuses to start with uncommitted source in the base
     tree (which a stray `git add -A` could sweep), and init gitignores .autobuild/.
"""

import json
import os

import pytest

from autobuild import cli as cli_mod
from autobuild import loop as loop_mod
from autobuild.config import Config
from autobuild.loop import BaseBranchLeak, base_leak_commits, dirty_base_paths
from autobuild.paths import Paths
from autobuild.session import build_prompt, spawn_session
from autobuild.tasks import read_task
from autobuild.worktree import make_worktree


# ---- helpers ----------------------------------------------------------------

def setup(repo):
    paths = Paths(repo)
    paths.tasks_dir.mkdir(parents=True)
    paths.ensure_runtime_dirs()
    return paths


def add_task(paths, tid="task-001", status="in-progress"):
    p = paths.tasks_dir / f"{tid}.md"
    p.write_text(f"---\nid: {tid}\ntitle: {tid}\nstatus: {status}\n"
                 f"priority: 1\ndepends_on: []\n---\n\n## Goal\nx\n", encoding="utf-8")
    return p


def head(repo, ref="HEAD"):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          capture_output=True, text=True).stdout.strip()


def make_leak_session(paths, git, tid="task-001", base_sha=None):
    """A finished COMPLETE session whose meta records base_sha. Returns its sdir."""
    sid = f"sess-{tid}"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({
        "session": sid, "task": tid, "task_file": str(paths.tasks_dir / f"{tid}.md"),
        "worktree": str(paths.worktrees_dir / sid), "branch": f"autobuild/{tid}",
        "status": "in-progress", "base_sha": base_sha or head(paths.root),
    }))
    (sdir / "result.json").write_text(json.dumps({
        "task": tid, "status": "COMPLETE", "summary": "s", "commit": "", "followups": [],
    }))
    return sdir


# ---- 1. leak detection (the core invariant) ---------------------------------

def test_base_leak_commits_clean_when_base_unmoved(git_repo):
    paths = setup(git_repo)
    assert base_leak_commits(paths, "main", head(git_repo)) == []


def test_base_leak_commits_flags_a_direct_commit_on_base(git_repo, git):
    """An agent that ran `git add -A && git commit` while HEAD was base leaves a
    non-merge commit on base's first-parent chain — the leak signature."""
    paths = setup(git_repo)
    base0 = head(git_repo)
    (git_repo / "leaked.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "agent leaked onto main")
    leaks = base_leak_commits(paths, "main", base0)
    assert leaks == [head(git_repo)]


def test_base_leak_commits_ignores_harness_merge(git_repo, git):
    """The harness only ever advances base via a --no-ff MERGE commit (auto-merge).
    Those must NOT be reported as leaks."""
    paths = setup(git_repo)
    base0 = head(git_repo)
    git(git_repo, "checkout", "-q", "-b", "autobuild/task-001")
    (git_repo / "feature.py").write_text("y = 2\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "feature on its own branch")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "--no-ff", "-m", "autobuild: merge task-001", "autobuild/task-001")
    assert base_leak_commits(paths, "main", base0) == []


def test_base_leak_commits_unresolvable_base_sha_does_not_false_alarm(git_repo, git):
    paths = setup(git_repo)
    assert base_leak_commits(paths, "main", "0" * 40) == []
    assert base_leak_commits(paths, "main", "") == []
    # an empty base_branch must not degrade to `base_sha..HEAD` and flag ordinary commits
    base0 = head(git_repo)
    (git_repo / "x.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "ordinary commit")
    assert base_leak_commits(paths, "", base0) == []


def test_reap_detects_leak_blocks_task_and_raises(git_repo, git):
    """End to end: a session whose run leaked a commit onto base is reaped → the task
    is blocked, a leak.json forensic marker is written, the work is NOT integrated, and
    BaseBranchLeak is raised so the supervisor halts instead of building on a bad base."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    base0 = head(git_repo)
    make_worktree(paths, "sess-task-001", "task-001", "main")
    # the agent escaped: committed straight onto main in the base checkout
    (git_repo / "swept.py").write_text("oops = True\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "escaped commit on main")
    leaked_head = head(git_repo)
    sdir = make_leak_session(paths, git, "task-001", base_sha=base0)

    with pytest.raises(BaseBranchLeak):
        loop_mod.reap_session(sdir, Config(integration="auto-merge"), paths)

    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"
    assert (sdir / "leak.json").exists()
    leak = json.loads((sdir / "leak.json").read_text())
    assert leaked_head in leak["commits"]
    # not integrated and not marked reaped: base must not be merged onto, and a re-run
    # must keep flagging until a human cleans base.
    assert head(git_repo) == leaked_head  # no integration merge added
    assert not (sdir / "reaped.json").exists()


def test_clean_session_reaps_normally_with_base_sha(git_repo, git):
    """Regression: a well-behaved session (work on its own branch) still reaps and
    integrates with base_sha recorded — the detector must not flag the honest path."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    base0 = head(git_repo)
    wt = make_worktree(paths, "sess-task-001", "task-001", "main")
    (wt / "feature.py").write_text("ok = 1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "honest work on the branch")
    sdir = make_leak_session(paths, git, "task-001", base_sha=base0)
    assert loop_mod.reap_session(sdir, Config(integration="auto-merge"), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- 2. anchor removal (the cause) ------------------------------------------

def test_build_prompt_points_only_at_worktree_and_session_dir():
    prompt = build_prompt("/s/dir", "/s/dir/task-001.md", "/w/tree", "task-001")
    # the stub + reaper still parse these two markers:
    import re
    assert re.search(r"session directory[^:]*is:\s*(\S+)", prompt).group(1) == "/s/dir"
    assert re.search(r"Work ONLY on task\s+(\S+)", prompt).group(1).rstrip(".") == "task-001"
    # contract is staged in the session dir, NOT referenced from the main checkout:
    assert "/s/dir/CLAUDE.md" in prompt
    assert "/s/dir/GOAL.md" in prompt
    # assertive confinement language is present:
    assert "/w/tree" in prompt
    assert "outside" in prompt.lower()


def test_build_prompt_names_no_main_checkout_path():
    """The prompt must not hand the agent any root/main-checkout path to resolve
    repo-relative work against — that was the original escape vector."""
    prompt = build_prompt("/proj/.autobuild/sessions/s", "/proj/.autobuild/sessions/s/t.md",
                          "/proj/.autobuild/worktrees/s", "task-001")
    # the only /proj/... paths allowed are the worktree and the session dir
    for token in prompt.split():
        if token.startswith("/proj/"):
            assert token.startswith("/proj/.autobuild/")


def test_spawn_stages_contract_and_records_base_sha(git_repo, monkeypatch):
    from autobuild import session as session_mod
    paths = setup(git_repo)
    paths.goal_file.write_text("# goal\n", encoding="utf-8")
    paths.claude_md.write_text("# contract\n", encoding="utf-8")
    add_task(paths, "task-001", status="todo")
    task = read_task(paths.tasks_dir / "task-001.md")
    monkeypatch.setattr(session_mod, "_process_group_id", lambda proc: 4242)
    monkeypatch.setattr(session_mod, "Popen",
                        lambda *a, **k: type("P", (), {"poll": lambda s: None})())
    rs = spawn_session(task, Config(), paths)

    assert (rs.sdir / "CLAUDE.md").read_text() == "# contract\n"
    assert (rs.sdir / "GOAL.md").read_text() == "# goal\n"
    assert (rs.sdir / "task-001.md").exists()
    meta = json.loads((rs.sdir / "meta.json").read_text())
    assert meta["base_sha"] == head(git_repo)
    # meta.json (agent-readable) must not name a main-checkout path
    assert meta["task_file"] == str(rs.sdir / "task-001.md")


def test_staged_contract_cannot_be_swept_into_the_feature_commit(git_repo, monkeypatch, git):
    """The staged copies live in the session dir, OUTSIDE the worktree tree, so an agent's
    `git add -A` in the worktree can never capture them."""
    from autobuild import session as session_mod
    paths = setup(git_repo)
    paths.goal_file.write_text("# goal\n", encoding="utf-8")
    paths.claude_md.write_text("# contract\n", encoding="utf-8")
    add_task(paths, "task-001", status="todo")
    task = read_task(paths.tasks_dir / "task-001.md")
    monkeypatch.setattr(session_mod, "_process_group_id", lambda proc: 4242)
    monkeypatch.setattr(session_mod, "Popen",
                        lambda *a, **k: type("P", (), {"poll": lambda s: None})())
    rs = spawn_session(task, Config(), paths)

    assert not str(rs.sdir).startswith(str(rs.worktree) + "/")  # sdir is outside the worktree
    (rs.worktree / "feature.py").write_text("x = 1\n")
    git(rs.worktree, "add", "-A")
    git(rs.worktree, "commit", "-q", "-m", "agent work")
    committed = git(rs.worktree, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert "CLAUDE.md" not in committed and "GOAL.md" not in committed
    assert not any("task-001" in f for f in committed)


# ---- 3. dirty-base guard + gitignore ----------------------------------------

def test_dirty_base_paths_flags_uncommitted_source(git_repo, git):
    paths = setup(git_repo)
    (git_repo / "untracked.py").write_text("x = 1\n")
    assert "untracked.py" in dirty_base_paths(paths)


def test_dirty_base_paths_ignores_tasks_and_autobuild_churn(git_repo, git):
    """Status transitions rewrite tasks/*.md and .autobuild/ is harness state — neither
    is user work a sweep would lose, so they must not trip the guard (or it would be
    useless mid-run)."""
    paths = setup(git_repo)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "--allow-empty", "-m", "seed")
    add_task(paths, "task-001")              # writes tasks/task-001.md
    (paths.ab_dir / "scratch").write_text("state\n")
    assert dirty_base_paths(paths) == []


def test_run_refuses_dirty_base(git_repo, monkeypatch):
    paths = setup(git_repo)
    paths.config_file.write_text("base_branch: main\n", encoding="utf-8")
    (git_repo / "uncommitted.py").write_text("x = 1\n")
    monkeypatch.delenv("AUTOBUILD_ALLOW_DIRTY_BASE", raising=False)
    with pytest.raises(loop_mod.DirtyBaseTree):
        loop_mod.run(paths, Config(base_branch="main"))


def test_run_dirty_base_override_env(git_repo, monkeypatch):
    paths = setup(git_repo)
    (git_repo / "uncommitted.py").write_text("x = 1\n")
    monkeypatch.setenv("AUTOBUILD_ALLOW_DIRTY_BASE", "1")
    # with no runnable tasks the loop drains immediately; the point is it does NOT raise.
    loop_mod.run(paths, Config(base_branch="main", max_iterations=1))


def test_init_gitignores_autobuild(git_repo):
    paths = Paths(git_repo)
    cli_mod.ab_init(paths)
    gi = (git_repo / ".gitignore").read_text()
    assert any(line.strip().rstrip("/") == ".autobuild" for line in gi.splitlines())


def test_init_appends_gitignore_without_duplicating(git_repo):
    paths = Paths(git_repo)
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    cli_mod.ab_init(paths)
    cli_mod.ab_init(paths)  # idempotent
    gi = (git_repo / ".gitignore").read_text()
    assert "node_modules/" in gi
    assert gi.count(".autobuild") == 1


def test_init_respects_existing_alternate_gitignore_spelling(git_repo):
    """A project already ignoring .autobuild via a different spelling must not get a
    redundant entry."""
    paths = Paths(git_repo)
    (git_repo / ".gitignore").write_text("/.autobuild/**\n", encoding="utf-8")
    cli_mod.ab_init(paths)
    assert (git_repo / ".gitignore").read_text().count(".autobuild") == 1
