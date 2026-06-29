import json
import os
import signal
import subprocess
import time

import pytest

from autobuild import loop as loop_mod
from autobuild.config import Config
from autobuild.loop import (
    collect_status,
    file_followups,
    reap_all,
    reap_session,
    reap_stalled,
    reconcile,
    status,
)
from autobuild.paths import Paths
from autobuild.retries import record_timeout, retry_count
from autobuild.session import RunningSession, spawn_session
from autobuild.tasks import read_task
from autobuild.worktree import branch_name, make_worktree


def _spawn_real(paths, stub_bin, tid="task-001", **env):
    """Spawn one real session through spawn_session (stub on PATH); return the handle."""
    stub_bin(**env)
    add_task(paths, tid, status="todo")
    task = read_task(paths.tasks_dir / f"{tid}.md")
    return spawn_session(task, Config(claude_cmd="claude", task_timeout_seconds=1800,
                                      kill_grace_seconds=1), paths)


def _await_ready(sdir, timeout=5.0):
    """Block until a STUB_SLEEP child has installed its signal handlers (it touches
    <sdir>/stub-ready), so a kill in the test can't race the handler install."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (sdir / "stub-ready").exists():
            return
        time.sleep(0.01)
    raise AssertionError("stub never signaled ready")


def setup(repo):
    paths = Paths(repo)
    paths.tasks_dir.mkdir(parents=True)
    paths.ensure_runtime_dirs()
    return paths


def add_task(paths, tid, status="in-progress", priority=1, depends_on=()):
    deps = "[" + ", ".join(depends_on) + "]"
    p = paths.tasks_dir / f"{tid}.md"
    p.write_text(f"---\nid: {tid}\ntitle: {tid}\nstatus: {status}\n"
                 f"priority: {priority}\ndepends_on: {deps}\n---\n\n## Goal\nx\n", encoding="utf-8")
    return p


def make_session(paths, tid, status, *, summary="s", commit="", followups=None):
    sid = f"sess-{tid}"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({
        "session": sid, "task": tid, "task_file": str(paths.tasks_dir / f"{tid}.md"),
        "worktree": str(paths.worktrees_dir / sid), "branch": f"autobuild/{tid}",
        "status": "in-progress",
    }))
    (sdir / "result.json").write_text(json.dumps({
        "task": tid, "status": status, "summary": summary,
        "commit": commit, "followups": followups or [],
    }))
    return sdir


# ---- reaper acts on each sentinel ------------------------------------------

def test_reap_complete_branch_mode(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"
    assert (sdir / "reaped.json").exists()


def test_reap_blocked(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "BLOCKED", summary="cannot proceed")
    reap_session(sdir, Config(integration="branch"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_reap_needs_human_preserves_sentinel(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "NEEDS_HUMAN")
    reap_session(sdir, Config(integration="branch"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"
    assert (sdir / "result.json").exists()  # left for the human


def test_reap_unknown_status_is_noop(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "WAT")
    assert reap_session(sdir, Config(integration="branch"), paths) is False
    assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"
    assert not (sdir / "reaped.json").exists()


def test_double_reap_is_idempotent(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE",
                        followups=[{"title": "follow me", "priority": 2}])
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    tasks_after_first = sorted(p.name for p in paths.tasks_dir.glob("*.md"))
    # second pass must not re-file the follow-up or change anything
    assert reap_session(sdir, Config(integration="branch"), paths) is False
    assert sorted(p.name for p in paths.tasks_dir.glob("*.md")) == tasks_after_first


def test_reap_finds_task_when_filename_differs_from_id(git_repo):
    paths = setup(git_repo)
    # filename != id; reaper must locate by frontmatter id, not filename
    p = paths.tasks_dir / "renamed-thing.md"
    p.write_text("---\nid: task-042\ntitle: t\nstatus: in-progress\npriority: 1\ndepends_on: []\n---\n")
    sdir = make_session(paths, "task-042", "BLOCKED")
    reap_session(sdir, Config(integration="branch"), paths)
    assert read_task(p).status == "blocked"


# ---- integration ------------------------------------------------------------

def test_reap_complete_auto_merge_lands_on_base(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    # real branch with a commit ahead of main, via a worktree
    wt = make_worktree(paths, "sess-task-001", "task-001", "main")
    (wt / "feature.txt").write_text("hi")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "feature work")
    make_session(paths, "task-001", "COMPLETE", commit="x")
    reap_session(paths.sessions_dir / "sess-task-001", Config(integration="auto-merge"), paths)
    # the feature commit is now reachable from main
    log = git(git_repo, "log", "--oneline", "main").stdout
    assert "feature work" in log
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


def test_reap_pr_mode_without_gh_leaves_branch_and_marks_done(git_repo, monkeypatch):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    make_worktree(paths, "sess-task-001", "task-001", "main")
    make_session(paths, "task-001", "COMPLETE")
    monkeypatch.setattr(loop_mod, "which", lambda name: None)  # gh absent
    reap_session(paths.sessions_dir / "sess-task-001", Config(integration="pr"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- integration retry-with-backoff (pr mode: push + gh pr create) ----------

def _cp(returncode=0, stdout="", stderr=""):
    """A stand-in subprocess.CompletedProcess for stubbing _git / _gh."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _pr_integrate(monkeypatch, paths, *, git_fn, gh_fn, retries=2):
    """Run integrate() in pr mode with gh present and stubbed _git/_gh seams; capture
    the injected backoff sleeps. Returns (success, detail, slept)."""
    monkeypatch.setattr(loop_mod, "which", lambda name: "gh")
    monkeypatch.setattr(loop_mod, "_git", git_fn)
    monkeypatch.setattr(loop_mod, "_gh", gh_fn)
    slept: list[int] = []
    success, detail = loop_mod.integrate(
        "task-001", Config(integration="pr", integration_max_retries=retries),
        paths, sleep=slept.append)
    return success, detail, slept


def test_integrate_pr_push_retries_once_then_succeeds(git_repo, monkeypatch):
    paths = setup(git_repo)
    pushes = {"n": 0}

    def git_fn(root, *args):
        if args[:1] == ("push",):
            pushes["n"] += 1
            if pushes["n"] == 1:  # transient blip on the first try
                return _cp(1, stderr="fatal: unable to access ...: Could not resolve host github.com")
            return _cp(0)
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=git_fn, gh_fn=lambda root, *a: _cp(0))
    assert success and detail == "opened PR for autobuild/task-001"
    assert pushes["n"] == 2     # one retry after the blip
    assert slept == [1]         # a single 1s backoff before the retry


def test_integrate_pr_push_permanent_failure_is_single_shot(git_repo, monkeypatch):
    paths = setup(git_repo)
    pushes = {"n": 0}

    def git_fn(root, *args):
        if args[:1] == ("push",):
            pushes["n"] += 1
            return _cp(1, stderr="fatal: 'origin' does not appear to be a git repository")
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=git_fn, gh_fn=lambda root, *a: _cp(0))
    # auth/no-remote is permanent -> not retried, falls back to today's exact message
    assert success
    assert detail == "push failed for autobuild/task-001; kept locally, no PR opened (check remote/auth)"
    assert pushes["n"] == 1
    assert slept == []


def test_integrate_pr_push_transient_exhausted_falls_back(git_repo, monkeypatch):
    paths = setup(git_repo)
    pushes = {"n": 0}
    gh_calls = {"n": 0}

    def git_fn(root, *args):
        if args[:1] == ("push",):
            pushes["n"] += 1
            return _cp(1, stderr="error: connection timed out")  # transient, never recovers
        return _cp(0)

    def gh_fn(root, *args):
        gh_calls["n"] += 1
        return _cp(0)

    success, detail, slept = _pr_integrate(monkeypatch, paths, git_fn=git_fn, gh_fn=gh_fn)
    assert success
    assert detail == "push failed for autobuild/task-001; kept locally, no PR opened (check remote/auth)"
    assert pushes["n"] == 3      # 1 + 2 retries, capped
    assert slept == [1, 2]       # increasing backoff before each retry
    assert gh_calls["n"] == 0    # never reached the PR-open step


def test_integrate_pr_create_already_exists_is_success_no_duplicate(git_repo, monkeypatch):
    paths = setup(git_repo)
    gh_calls = {"create": 0, "list": 0}

    def gh_fn(root, *args):
        if args[:2] == ("pr", "create"):
            gh_calls["create"] += 1
            return _cp(1, stderr=(
                'a pull request for branch "autobuild/task-001" into branch "main" '
                'already exists:\nhttps://github.com/x/y/pull/7'))
        if args[:2] == ("pr", "list"):
            gh_calls["list"] += 1
            return _cp(0, stdout="[]")
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=lambda root, *a: _cp(0), gh_fn=gh_fn)
    assert success and detail == "PR already exists for autobuild/task-001"
    assert gh_calls["create"] == 1   # gh's own "already exists" -> success, NOT retried
    assert gh_calls["list"] == 0     # short-circuited on stderr, no list needed
    assert slept == []               # no duplicate, no backoff


def test_integrate_pr_create_detects_existing_pr_via_list(git_repo, monkeypatch):
    paths = setup(git_repo)
    gh_calls = {"create": 0, "list": 0}

    def gh_fn(root, *args):
        if args[:2] == ("pr", "create"):
            gh_calls["create"] += 1
            return _cp(1, stderr="error: failed to create pull request")  # opaque failure
        if args[:2] == ("pr", "list"):
            gh_calls["list"] += 1
            return _cp(0, stdout='[{"url": "https://github.com/x/y/pull/9"}]')  # a PR exists
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=lambda root, *a: _cp(0), gh_fn=gh_fn)
    assert success and detail == "PR already exists for autobuild/task-001"
    assert gh_calls["create"] == 1   # found via list -> treated as success, not retried
    assert gh_calls["list"] == 1
    assert slept == []


def test_integrate_pr_create_retries_exhausted_falls_back(git_repo, monkeypatch):
    paths = setup(git_repo)
    gh_calls = {"create": 0, "list": 0}

    def gh_fn(root, *args):
        if args[:2] == ("pr", "create"):
            gh_calls["create"] += 1
            return _cp(1, stderr="error: server returned HTTP 502")  # transient
        if args[:2] == ("pr", "list"):
            gh_calls["list"] += 1
            return _cp(0, stdout="[]")  # no existing PR
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=lambda root, *a: _cp(0), gh_fn=gh_fn)
    # exhausted -> today's exact (success, message): branch pushed, human can open the PR
    assert success
    assert detail == "PR creation failed for autobuild/task-001; pushed branch left for manual PR"
    assert gh_calls["create"] == 3   # 1 + 2 retries, no more
    assert slept == [1, 2]


def test_integrate_pr_zero_retries_is_single_attempt(git_repo, monkeypatch):
    paths = setup(git_repo)
    gh_calls = {"create": 0}

    def gh_fn(root, *args):
        if args[:2] == ("pr", "create"):
            gh_calls["create"] += 1
            return _cp(1, stderr="error: server returned HTTP 502")
        if args[:2] == ("pr", "list"):
            return _cp(0, stdout="[]")
        return _cp(0)

    success, detail, slept = _pr_integrate(
        monkeypatch, paths, git_fn=lambda root, *a: _cp(0), gh_fn=gh_fn, retries=0)
    assert success
    assert detail == "PR creation failed for autobuild/task-001; pushed branch left for manual PR"
    assert gh_calls["create"] == 1   # 0 retries -> a single attempt
    assert slept == []


def test_integrate_auto_merge_conflict_is_not_retried(git_repo, monkeypatch, diverging_dep):
    # The auto-merge conflict path stays single-shot: no sleep, one merge attempt.
    paths = setup(git_repo)
    dep = diverging_dep(git_repo)            # main and autobuild/<dep> conflict on a line
    merges = {"n": 0}
    real_git = loop_mod._git

    def git_fn(root, *args):
        if args[:1] == ("merge",) and "--abort" not in args:
            merges["n"] += 1
        return real_git(root, *args)

    monkeypatch.setattr(loop_mod, "_git", git_fn)
    slept: list[int] = []
    success, detail = loop_mod.integrate(
        dep, Config(integration="auto-merge", integration_max_retries=2),
        paths, sleep=slept.append)
    assert success is False and "conflict" in detail
    assert merges["n"] == 1   # single-shot
    assert slept == []


# ---- checks verification gate ----------------------------------------------

def _branch_exists(git, repo, tid):
    return git(repo, "show-ref", "--verify", "--quiet",
               f"refs/heads/autobuild/{tid}", check=False).returncode == 0


def _worktree_with_commit(paths, git, *, broken):
    """A real worktree on autobuild/task-001 with one commit ahead of main. When
    broken, the commit creates a BROKEN file so `test ! -f BROKEN` fails."""
    wt = make_worktree(paths, "sess-task-001", "task-001", "main")
    (wt / ("BROKEN" if broken else "feature.txt")).write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "broken work" if broken else "feature work")
    return wt


def test_reap_failing_check_blocks_keeps_branch_and_logs(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=True)
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x")
    cfg = Config(integration="auto-merge", checks=["test ! -f BROKEN"])

    assert reap_session(sdir, cfg, paths) is True
    # blocked, NOT merged, branch preserved, checks.log written
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"
    assert "broken work" not in git(git_repo, "log", "--oneline", "main").stdout
    assert _branch_exists(git, git_repo, "task-001")
    assert (sdir / "checks.log").exists()
    log = (sdir / "checks.log").read_text()
    assert "test ! -f BROKEN" in log
    reaped = json.loads((sdir / "reaped.json").read_text())
    assert reaped["checks"] == "failed: test ! -f BROKEN"
    assert reaped["integrated"] is False


def test_reap_passing_check_integrates(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=False)
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x")
    cfg = Config(integration="auto-merge", checks=["test ! -f BROKEN"])

    assert reap_session(sdir, cfg, paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"
    assert "feature work" in git(git_repo, "log", "--oneline", "main").stdout
    assert json.loads((sdir / "reaped.json").read_text())["checks"] == "passed"
    assert not (sdir / "checks.log").exists()


def test_verify_checks_false_bypasses_gate(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=True)  # would fail the check...
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x")
    cfg = Config(integration="auto-merge", checks=["test ! -f BROKEN"], verify_checks=False)

    assert reap_session(sdir, cfg, paths) is True
    # ...but the gate is disabled, so today's trust-the-agent behavior: merged + done
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"
    assert "broken work" in git(git_repo, "log", "--oneline", "main").stdout
    assert json.loads((sdir / "reaped.json").read_text())["checks"] == "skipped"


def test_empty_checks_skips_gate(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=False)
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x")
    cfg = Config(integration="auto-merge", checks=[])  # no checks => no gate

    assert reap_session(sdir, cfg, paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"
    assert json.loads((sdir / "reaped.json").read_text())["checks"] == "skipped"


def test_failing_check_does_not_file_followups(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=True)
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x",
                        followups=[{"title": "discovered work", "priority": 2}])
    cfg = Config(integration="branch", checks=["test ! -f BROKEN"])

    reap_session(sdir, cfg, paths)
    # a tree that fails verification gets no follow-ups filed
    assert [p.name for p in paths.tasks_dir.glob("*.md")] == ["task-001.md"]
    assert json.loads((sdir / "reaped.json").read_text())["followups"] == []


def test_failing_check_reap_is_idempotent(git_repo, git):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    _worktree_with_commit(paths, git, broken=True)
    sdir = make_session(paths, "task-001", "COMPLETE", commit="x")
    cfg = Config(integration="auto-merge", checks=["test ! -f BROKEN"])

    assert reap_session(sdir, cfg, paths) is True
    # second pass is a no-op: the reaped.json guard prevents re-running checks
    assert reap_session(sdir, cfg, paths) is False
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


# ---- follow-up filing -------------------------------------------------------

def test_file_followups_creates_tasks_with_priority_and_notes(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")
    result = {"followups": [
        {"title": "Wire up CI", "priority": 2, "notes": "use the stub"},
        {"title": "Add docs", "priority": 4},
    ]}
    created = file_followups(result, sdir, paths)
    assert created == ["task-002", "task-003"]
    t2 = read_task(paths.tasks_dir / "task-002-wire-up-ci.md")
    assert t2.priority == 2 and t2.status == "todo"
    assert read_task(paths.tasks_dir / "task-003-add-docs.md").priority == 4


def test_file_followups_empty_is_noop(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")
    assert file_followups({"followups": []}, sdir, paths) == []


# ---- reconcile + stalled ----------------------------------------------------

def test_reconcile_resets_orphaned_claimed_to_todo(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="claimed")  # claimed but spawn never finished
    reconcile(paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"


def test_reconcile_blocks_orphaned_in_progress_when_sweeping(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    reconcile(paths, sweep_in_progress=True)
    # an orphaned in-progress session gets a BLOCKED sentinel...
    result = json.loads((sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"
    # ...which the reaper then turns into a blocked task
    reap_all(Config(integration="branch"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_reconcile_spares_in_progress_without_sweep(git_repo):
    """The dangerous in-progress -> BLOCKED sweep is gated: a reconcile that does
    not own the run lock (sweep_in_progress=False) must leave in-progress alone."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    reconcile(paths)  # default: do not sweep
    assert not (sdir / "result.json").exists()
    assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"


# ---- run lock ---------------------------------------------------------------

def test_second_run_refused_while_run_lock_held(git_repo):
    """A second `run` while one is active exits non-zero (raises RunLockHeld)
    without mutating task status, sessions, or worktrees."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo")
    with loop_mod.run_lock(paths.run_lock):  # simulate the active run holding it
        with pytest.raises(loop_mod.RunLockHeld):
            loop_mod.run(paths, Config(integration="branch"), sleep_seconds=0)
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"
    assert list(paths.sessions_dir.iterdir()) == []


def test_run_lock_released_after_run_returns(git_repo):
    """The lock is advisory and released when the run ends, so a later run can
    re-acquire it (flock auto-release is the crash semantic we rely on)."""
    paths = setup(git_repo)
    loop_mod.run(paths, Config(integration="branch"), sleep_seconds=0)  # no tasks -> returns
    with loop_mod.run_lock(paths.run_lock):  # would raise if still held
        pass


def test_reap_alongside_active_run_spares_live_in_progress(git_repo):
    """A reap that cannot take the run lock (a run is active) must NOT block a live
    in-progress session nor remove its worktree — it can't see the run's children."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    wt = make_worktree(paths, "sess-live", "task-001", "main")
    sdir = paths.sessions_dir / "sess-live"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    with loop_mod.run_lock(paths.run_lock):  # the owning run holds the lock
        loop_mod.reap(paths, Config(integration="branch"))
    assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"
    assert not (sdir / "result.json").exists()
    assert wt.exists()


def test_reap_alongside_run_leaves_live_session_process_and_worktree(git_repo):
    """End-to-end shape: a genuinely live session (a long-sleeping process standing
    in for `claude`) the owning run supervises, plus the held run lock. A concurrent
    reap must leave the task in-progress, write no sentinel, keep the worktree, and
    never touch the live process — the data-loss the run lock prevents."""
    import subprocess as sp

    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    wt = make_worktree(paths, "sess-live", "task-001", "main")
    sdir = paths.sessions_dir / "sess-live"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    proc = sp.Popen(["sleep", "30"])
    try:
        with loop_mod.run_lock(paths.run_lock):  # the owning run holds the lock
            loop_mod.reap(paths, Config(integration="branch"))
        assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"
        assert not (sdir / "result.json").exists()
        assert wt.exists()
        assert proc.poll() is None  # reap never reached for the live process
    finally:
        proc.terminate()
        proc.wait()


def test_fresh_run_reconciles_orphaned_in_progress_after_crash(git_repo):
    """After a run is killed (lock auto-released), a fresh run takes the lock and
    reconciles orphaned in-progress sessions to blocked — crash recovery works."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-orphan"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    loop_mod.run(paths, Config(integration="branch"), sleep_seconds=0)
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_reap_without_active_run_recovers_orphaned_in_progress(git_repo):
    """When no run is active, reap can take the lock and perform the same
    crash-recovery sweep a fresh run would."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-orphan"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    loop_mod.reap(paths, Config(integration="branch"))
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_reap_stalled_blocks_dead_process_without_result(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)

    class DeadProc:
        def poll(self):
            return 1  # exited

    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, DeadProc())
    reap_stalled([rs], paths)
    result = json.loads((sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"


def test_reap_stalled_leaves_live_process_alone(git_repo):
    paths = setup(git_repo)
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)

    class LiveProc:
        def poll(self):
            return None  # still running

    rs = RunningSession(sid, "task-001", sdir, None, None, LiveProc())
    reap_stalled([rs], paths)
    assert not (sdir / "result.json").exists()


# ---- bug fix: malformed sentinel must not silently strand a task -----------

def test_reap_salvages_sentinel_with_trailing_data(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    # a valid JSON object followed by stray trailing output an agent appended
    (sdir / "result.json").write_text(
        '{"task": "task-001", "status": "COMPLETE", "summary": "s", "commit": "", "followups": []}\n</content>\n',
        encoding="utf-8",
    )
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- bug fix: a session exiting in the termination window is never dropped ---

class _Exited:
    def __init__(self, code=0):
        self._code = code

    def poll(self):
        return self._code


class _Alive:
    def poll(self):
        return None


def test_harvest_reaps_exited_session_that_has_result(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")  # result.json on disk
    rs = RunningSession("sess-task-001", "task-001", sdir,
                        paths.worktrees_dir / "sess-task-001", None, _Exited())
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


def test_harvest_blocks_exited_session_without_result(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, _Exited(1))
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_harvest_keeps_live_session(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    rs = RunningSession(sid, "task-001", sdir, None, None, _Alive())
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == [rs]


# ---- bug fix: a present-but-unparseable sentinel must not strand a task ------
# raw_decode salvages trailing junk after a valid object; these are the shapes it
# CANNOT salvage (leading garbage, empty, non-object) and which used to fall through
# every gate (reconcile / _harvest / reap_session) and strand the task forever.
CORRUPT_SENTINELS = {
    "leading-garbage": 'oops a preamble\n{"task": "task-001", "status": "COMPLETE"}\n',
    "empty": "",
    "whitespace": "   \n\t\n",
    "json-list": "[]",
    "json-string": '"x"',
}


@pytest.mark.parametrize("shape", sorted(CORRUPT_SENTINELS))
def test_classify_sentinel_corrupt(git_repo, shape):
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text(CORRUPT_SENTINELS[shape], encoding="utf-8")
    assert loop_mod._classify_sentinel(sdir) == "corrupt"


def test_classify_sentinel_absent_and_reapable(git_repo):
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    assert loop_mod._classify_sentinel(sdir) == "absent"
    # a valid object plus trailing junk is salvageable -> reapable, not corrupt
    (sdir / "result.json").write_text(
        '{"task": "task-001", "status": "COMPLETE", "summary": "s"}\n</content>\n', encoding="utf-8")
    assert loop_mod._classify_sentinel(sdir) == "reapable"


@pytest.mark.parametrize("shape", sorted(CORRUPT_SENTINELS))
def test_harvest_blocks_exited_session_with_corrupt_sentinel(git_repo, shape):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    (sdir / "result.json").write_text(CORRUPT_SENTINELS[shape], encoding="utf-8")
    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, _Exited())
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_harvest_spares_live_session_with_torn_sentinel(git_repo):
    """A corrupt result.json under a STILL-RUNNING process is a torn/in-flight write;
    leave it to finish — never BLOCK a live session."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text('{"task": "task-001"', encoding="utf-8")  # mid-write
    rs = RunningSession(sid, "task-001", sdir, None, None, _Alive())
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == [rs]
    assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"


def test_harvest_reaps_valid_result_even_while_process_alive(git_repo):
    """A valid sentinel is reaped even if the process hasn't exited yet, so a session
    that writes its result then lingers cannot hang the loop (no regression)."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")
    rs = RunningSession("sess-task-001", "task-001", sdir,
                        paths.worktrees_dir / "sess-task-001", None, _Alive())
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


@pytest.mark.parametrize("shape", sorted(CORRUPT_SENTINELS))
def test_reconcile_blocks_in_progress_with_corrupt_sentinel(git_repo, shape):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    (sdir / "result.json").write_text(CORRUPT_SENTINELS[shape], encoding="utf-8")
    reconcile(paths, sweep_in_progress=True)
    reap_all(Config(integration="branch"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"


def test_reconcile_leaves_reapable_sentinel_for_reaper(git_repo):
    """A valid sentinel on an in-progress task is the reaper's job; reconcile must not
    clobber it with BLOCKED."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "COMPLETE")
    reconcile(paths, sweep_in_progress=True)
    assert json.loads((sdir / "result.json").read_text())["status"] == "COMPLETE"
    reap_all(Config(integration="branch"), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- status surfaces stuck tasks -------------------------------------------

def test_collect_status_includes_stuck(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo", depends_on=["task-999"])
    report = collect_status(paths)
    assert report["stuck"] == [{"task": "task-001", "reason": "missing-dependency: task-999"}]


def test_collect_status_stuck_empty_when_none(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo")
    assert collect_status(paths)["stuck"] == []


def test_status_prints_stuck_section_when_present(git_repo, capsys):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo", depends_on=["task-999"])
    status(paths, Config(integration="branch"))
    out = capsys.readouterr().out
    assert "STUCK" in out
    assert "task-001" in out
    assert "missing-dependency: task-999" in out


def test_status_omits_stuck_section_when_none(git_repo, capsys):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo")
    status(paths, Config(integration="branch"))
    assert "STUCK" not in capsys.readouterr().out


# ---- task-101: harness BLOCK paths are atomic and don't clobber a real result -

def test_reap_stalled_block_write_is_atomic_no_temp_residue(git_repo):
    """The rerouted BLOCK write still produces a sentinel and leaves no partial temp."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, _Exited(1))
    reap_stalled([rs], paths)
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []


def test_harvest_block_write_is_atomic_no_temp_residue(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, _Exited(1))
    loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []


def test_reconcile_block_write_is_atomic_no_temp_residue(git_repo):
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    reconcile(paths, sweep_in_progress=True)
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []


def test_harvest_block_path_refuses_to_clobber_late_valid_result(git_repo):
    """Belt-and-suspenders: even if a valid result lands in the BLOCK window, the
    guarded write refuses to overwrite it (the data-loss task-101 closes)."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sid = "sess-task-001"
    sdir = make_session(paths, "task-001", "COMPLETE", summary="real result")
    rs = RunningSession(sid, "task-001", sdir, paths.worktrees_dir / sid, None, _Exited(0))
    loop_mod._harvest([rs], Config(integration="branch"), paths)
    # the real COMPLETE result is reaped, not overwritten by a BLOCKED sentinel
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- task-105: _kill_group + signal handling -------------------------------

def test_kill_group_reaps_running_child(git_repo, stub_bin):
    paths = setup(git_repo)
    rs = _spawn_real(paths, stub_bin, STUB_SLEEP="30", STUB_IGNORE_SIGTERM="1")
    _await_ready(rs.sdir)                          # SIG_IGN installed -> kill exercises SIGKILL
    assert rs.proc.poll() is None                 # alive
    loop_mod._kill_group(rs, grace=1)
    assert rs.proc.poll() is not None             # killed + reaped, no zombie


def test_kill_group_already_exited_is_noop(git_repo, stub_bin):
    paths = setup(git_repo)
    rs = _spawn_real(paths, stub_bin)             # normal stub exits immediately
    rs.proc.wait()
    loop_mod._kill_group(rs, grace=1)             # must not raise (ESRCH-safe)
    assert rs.proc.poll() is not None


def test_signal_session_esrch_suppressed(monkeypatch):
    def raise_esrch(pgid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(loop_mod.os, "killpg", raise_esrch)
    rs = RunningSession("s", "task-001", None, None, None, None, pgid=12345)
    loop_mod._signal_session(rs, signal.SIGTERM)  # ProcessLookupError suppressed -> no raise


# ---- task-105: harvest deadline ordering + TIMEOUT + timeout status ---------

def test_reapable_beats_deadline(git_repo):
    """A valid result present at harvest time is reaped COMPLETE even past deadline;
    never killed/overwritten by TIMEOUT."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "COMPLETE")          # result already on disk
    rs = RunningSession("sess-task-001", "task-001", sdir,
                        paths.worktrees_dir / "sess-task-001", None, _Alive(),
                        pgid=None, deadline=time.monotonic() - 5)  # past deadline
    survivors = loop_mod._harvest([rs], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


def test_finish_during_grace_beats_timeout(git_repo, stub_bin):
    """Child ignores SIGTERM and writes COMPLETE on it; after kill, re-classify sees the
    result -> reaped COMPLETE, no TIMEOUT."""
    paths = setup(git_repo)
    rs = _spawn_real(paths, stub_bin, STUB_SLEEP="30", STUB_COMPLETE_ON_SIGTERM="1")
    _await_ready(rs.sdir)                                       # handler installed first
    rs.deadline = time.monotonic() - 1                          # already past deadline
    survivors = loop_mod._harvest([rs], Config(integration="branch",
                                               kill_grace_seconds=5), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


def test_hung_past_deadline_times_out(git_repo, stub_bin):
    """Child ignores SIGTERM, never writes -> SIGKILL -> TIMEOUT sentinel; with no retry
    budget (timeout_max_retries=0) the task is terminal `timeout`, not blocked/done; no zombie."""
    paths = setup(git_repo)
    rs = _spawn_real(paths, stub_bin, STUB_SLEEP="30", STUB_IGNORE_SIGTERM="1")
    _await_ready(rs.sdir)
    rs.deadline = time.monotonic() - 1
    survivors = loop_mod._harvest([rs], Config(integration="branch", kill_grace_seconds=1,
                                               timeout_max_retries=0), paths)
    assert survivors == []
    assert rs.proc.poll() is not None                          # reaped, no zombie
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"


def test_timeout_never_integrates(git_repo, git):
    """A TIMEOUT sentinel is never integrated/verified even with a real branch+commit."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    wt = make_worktree(paths, "sess-task-001", "task-001", "main")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "w")
    sdir = paths.sessions_dir / "sess-task-001"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001",
                                                "branch": "autobuild/task-001"}))
    (sdir / "result.json").write_text(json.dumps({"task": "task-001", "status": "TIMEOUT",
                                                  "summary": "killed"}))
    # no retry budget -> terminal timeout, and never integrated even with a real branch+commit
    assert reap_session(sdir, Config(integration="auto-merge", timeout_max_retries=0), paths) is True
    assert "w" not in git(git_repo, "log", "--oneline", "main").stdout   # NOT merged
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"
    assert json.loads((sdir / "reaped.json").read_text())["status"] == "TIMEOUT"


def test_timeout_reap_is_idempotent(git_repo):
    """Second reap over a TIMEOUT'd session is a no-op (reaped.json guard)."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "TIMEOUT")
    cfg = Config(integration="branch", timeout_max_retries=0)   # exhausted -> terminal
    assert reap_session(sdir, cfg, paths) is True
    assert reap_session(sdir, cfg, paths) is False
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"


# ---- timeout auto-retry: re-queue while budget remains, then exhaust ---------

def test_timeout_requeues_and_discards_partial_branch(git_repo, git):
    """With retries remaining (default timeout_max_retries=2), a first timeout re-queues
    the task to `todo`, records the attempt in the ledger, and force-deletes the partial
    branch so the retry re-forks fresh from base."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    wt = make_worktree(paths, "sess-task-001", "task-001", "main")
    (wt / "partial.txt").write_text("half-done")               # an unmerged partial commit
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "partial work")
    sdir = make_session(paths, "task-001", "TIMEOUT")
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"   # re-queued
    assert not _branch_exists(git, git_repo, "task-001")                 # fresh base
    assert retry_count(paths.retries_ledger, "task-001") == 1
    reaped = json.loads((sdir / "reaped.json").read_text())
    assert reaped["status"] == "TIMEOUT" and reaped["requeued"] is True


def test_timeout_exhausts_after_budget_and_clears_ledger(git_repo):
    """Once a prior timeout has been recorded, the next timeout exhausts the budget
    (timeout_max_retries=1 -> 2 total): terminal `timeout`, ledger entry cleared so a
    later manual re-open starts fresh."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    record_timeout(paths.retries_ledger, "task-001", "sess-earlier")     # 1 attempt already
    sdir = make_session(paths, "task-001", "TIMEOUT")
    # Pin the budget to 1 so the arithmetic stays valid independent of the default:
    # one prior attempt + this one = 2 > 1 -> exhausted (matches "-> 2 total" above).
    assert reap_session(sdir, Config(integration="branch", timeout_max_retries=1), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"  # terminal
    assert retry_count(paths.retries_ledger, "task-001") == 0              # cleared


def test_timeout_zero_budget_blocks_on_first(git_repo):
    """timeout_max_retries=0 -> a single timeout is terminal immediately, no re-queue."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "TIMEOUT")
    reap_session(sdir, Config(integration="branch", timeout_max_retries=0), paths)
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"
    assert retry_count(paths.retries_ledger, "task-001") == 0


def test_timeout_requeue_is_idempotent(git_repo, git):
    """Re-reaping a re-queued session is a no-op: the reaped.json guard holds and the
    ledger count does not inflate (the session-id set is idempotent)."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    make_worktree(paths, "sess-task-001", "task-001", "main")
    sdir = make_session(paths, "task-001", "TIMEOUT")
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert reap_session(sdir, Config(integration="branch"), paths) is False
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"
    assert retry_count(paths.retries_ledger, "task-001") == 1


def test_timeout_reap_skips_transition_when_not_in_progress(git_repo):
    """Guard: if the task is no longer `in-progress` when a TIMEOUT session is reaped
    (a concurrent run already moved it on), the reap records the marker but does not
    clobber the task's status or touch the ledger."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="todo")           # already moved on
    sdir = make_session(paths, "task-001", "TIMEOUT")
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"   # unchanged
    assert retry_count(paths.retries_ledger, "task-001") == 0
    assert (sdir / "reaped.json").exists()


def test_reconcile_preserves_retry_budget(git_repo):
    """Crash recovery must NOT reset the budget: a startup reconcile leaves the ledger
    intact, so a task that already burned a retry before the crash can't refill it."""
    paths = setup(git_repo)
    record_timeout(paths.retries_ledger, "task-001", "sess-before-crash")
    reconcile(paths, sweep_in_progress=True)
    assert retry_count(paths.retries_ledger, "task-001") == 1


def test_clean_does_not_reset_retry_budget(git_repo):
    """`clean` removes reaped session dirs but must leave the ledger — the property that
    makes a ledger correct where session-derived counting silently refilled the budget."""
    paths = setup(git_repo)
    record_timeout(paths.retries_ledger, "task-001", "sess-old")
    reaped = paths.sessions_dir / "sess-old"
    reaped.mkdir(parents=True)
    (reaped / "reaped.json").write_text("{}")
    loop_mod.clean(paths)
    assert not reaped.exists()                                   # session dir gone
    assert retry_count(paths.retries_ledger, "task-001") == 1    # budget survives


def test_timeout_requeue_aborts_when_branch_undeletable(git_repo, monkeypatch):
    """If the partial branch can't be force-deleted (a degenerate worktree still pins it),
    the reaper must NOT re-queue onto surviving partial work — it gives up terminally so a
    human inspects, rather than silently resuming a half-done tree."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    make_worktree(paths, "sess-task-001", "task-001", "main")
    sdir = make_session(paths, "task-001", "TIMEOUT")
    monkeypatch.setattr(loop_mod, "delete_branch", lambda *a, **k: False)   # delete fails
    reap_session(sdir, Config(integration="branch"), paths)                 # default retries=1
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"   # terminal, not todo
    assert retry_count(paths.retries_ledger, "task-001") == 0               # ledger cleared


def test_timeout_for_missing_task_is_logged_not_silent(git_repo, capsys):
    """A TIMEOUT whose task file was deleted/renamed mid-flight is still reaped, but must
    not vanish silently — an operator watching the run needs a diagnostic line."""
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-task-001"          # no tasks/task-001.md exists
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    (sdir / "result.json").write_text(json.dumps({"task": "task-001", "status": "TIMEOUT",
                                                  "summary": "killed"}))
    assert reap_session(sdir, Config(integration="branch"), paths) is True
    assert (sdir / "reaped.json").exists()               # still reaped (idempotent)
    out = capsys.readouterr().out
    assert "task-001" in out and "TIMEOUT" in out        # not silent


def test_run_retries_timeout_then_settles(git_repo, stub_bin):
    """End-to-end through run(): a session that always times out is re-queued once
    (timeout_max_retries=1), respawned, times out again, then settles terminal `timeout`
    — proving the re-queue actually re-runs the task and the loop drains, not spins."""
    paths = setup(git_repo)
    stub_bin(STUB_SLEEP="30")                  # the session never finishes on its own
    add_task(paths, "task-001", status="todo")
    cfg = Config(claude_cmd="claude", integration="branch", max_iterations=10,
                 task_timeout_seconds=1, kill_grace_seconds=1, timeout_max_retries=1)
    loop_mod.run(paths, cfg, sleep_seconds=0.05)
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"   # exhausted -> terminal
    sessions = [d for d in paths.sessions_dir.iterdir() if d.is_dir()]
    assert len(sessions) >= 2                                                # respawned at least once
    assert retry_count(paths.retries_ledger, "task-001") == 0                # cleared on terminal


def test_one_dead_group_does_not_abort_harvest_of_others(git_repo):
    """A first session whose group is already dead must not strand a second session's
    harvest: the first is exited-without-result (-> BLOCKED), the second has a valid
    result (-> reaped done); both processed in one _harvest pass."""
    paths = setup(git_repo)
    s1 = paths.sessions_dir / "sess-a"
    s1.mkdir(parents=True)
    (s1 / "meta.json").write_text(json.dumps({"task": "task-001", "branch": "autobuild/task-001"}))
    add_task(paths, "task-001")
    rs1 = RunningSession("sess-a", "task-001", s1, paths.worktrees_dir / "sess-a", None,
                         _Exited(1), pgid=12345)            # group already gone
    add_task(paths, "task-002")
    s2 = make_session(paths, "task-002", "COMPLETE")
    rs2 = RunningSession("sess-task-002", "task-002", s2,
                         paths.worktrees_dir / "sess-task-002", None, _Exited(0))
    survivors = loop_mod._harvest([rs1, rs2], Config(integration="branch"), paths)
    assert survivors == []
    assert read_task(paths.tasks_dir / "task-001.md").status == "blocked"
    assert read_task(paths.tasks_dir / "task-002.md").status == "done"


def test_run_settles_with_parked_timeout_task(git_repo):
    """A terminal `timeout` task (retries exhausted) lets the loop settle cleanly, not
    spin to max_iterations."""
    paths = setup(git_repo)
    add_task(paths, "task-001", status="timeout")
    loop_mod.run(paths, Config(integration="branch"), sleep_seconds=0)  # must return
    assert read_task(paths.tasks_dir / "task-001.md").status == "timeout"


# ---- task-105: deadline-bounded wait ---------------------------------------

def test_next_wait_caps_at_nearest_deadline():
    rs = RunningSession("s", "t", None, None, None, None, deadline=100.0)
    assert loop_mod._next_wait([rs], sleep_seconds=2.0, now=99.5) == 0.5   # bounded
    assert loop_mod._next_wait([rs], sleep_seconds=2.0, now=101.0) == 0.0  # never negative


def test_next_wait_uses_sleep_when_no_deadlines():
    rs = RunningSession("s", "t", None, None, None, None, deadline=None)
    assert loop_mod._next_wait([rs], sleep_seconds=2.0, now=0.0) == 2.0


# ---- audit C-1: a non-dict result.json must never crash run/reap/status -----

NON_DICT_RESULTS = {"empty-list": "[]", "list-str": '["x"]', "string": '"s"', "number": "5"}


@pytest.mark.parametrize("shape", sorted(NON_DICT_RESULTS))
def test_read_json_non_dict_is_none(git_repo, shape):
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "s"
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text(NON_DICT_RESULTS[shape])
    assert loop_mod._read_json(sdir / "result.json") is None


@pytest.mark.parametrize("shape", sorted(NON_DICT_RESULTS))
def test_reap_session_tolerates_non_dict_result(git_repo, shape):
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-x"
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text(NON_DICT_RESULTS[shape])
    assert reap_session(sdir, Config(integration="branch"), paths) is False  # no crash


def test_collect_status_tolerates_non_dict_result(git_repo):
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-x"
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text('["x"]')
    report = collect_status(paths)                       # must not raise
    assert any(s["session"] == "sess-x" for s in report["sessions"])


def test_run_survives_poisoned_sentinel(git_repo):
    """A non-dict result.json must not crash run() at startup (reconcile -> reap_all)."""
    paths = setup(git_repo)
    sdir = paths.sessions_dir / "sess-poison"
    sdir.mkdir(parents=True)
    (sdir / "result.json").write_text('["x"]')
    loop_mod.run(paths, Config(integration="branch"), sleep_seconds=0)  # must return, not crash


# ---- audit I-4: pr mode must not mark done when push fails ------------------

def test_pr_mode_push_failure_keeps_done_with_clear_message(git_repo, monkeypatch):
    """pr mode without a reachable remote: the local branch is still the deliverable that
    downstream tasks merge, so the task stays `done` — but the detail must say no PR was
    opened (not the misleading 'PR creation failed', which implies the branch was pushed)."""
    paths = setup(git_repo)
    add_task(paths, "task-001")
    make_worktree(paths, "sess-task-001", "task-001", "main")
    monkeypatch.setattr(loop_mod, "which", lambda name: "/usr/bin/gh")   # pretend gh present
    # git_repo has no 'origin' remote -> push fails before gh is even invoked
    success, detail = loop_mod.integrate("task-001", Config(integration="pr"), paths)
    assert success is True                                   # branch is the deliverable
    assert "push failed" in detail and "no pr" in detail.lower()


# ---- audit I-5: clean must be lock-aware and keep unreaped results ----------

def test_clean_only_removes_reaped_sessions(git_repo):
    paths = setup(git_repo)
    reaped = paths.sessions_dir / "sess-done"
    reaped.mkdir(parents=True)
    (reaped / "reaped.json").write_text("{}")
    unreaped = paths.sessions_dir / "sess-live"
    unreaped.mkdir(parents=True)
    (unreaped / "result.json").write_text(json.dumps({"task": "t", "status": "COMPLETE"}))
    loop_mod.clean(paths)
    assert not reaped.exists()        # reaped -> removed
    assert unreaped.exists()          # unreaped COMPLETE -> preserved (used to be destroyed)


def test_clean_skips_when_run_active(git_repo):
    paths = setup(git_repo)
    s = paths.sessions_dir / "sess-done"
    s.mkdir(parents=True)
    (s / "reaped.json").write_text("{}")
    with loop_mod.run_lock(paths.run_lock):        # simulate an active run holding the lock
        loop_mod.clean(paths)
    assert s.exists()                 # clean refused under the lock


# ---- audit I-6: reaped.json is written atomically --------------------------

def test_reaped_json_written_atomically(git_repo, monkeypatch):
    paths = setup(git_repo)
    add_task(paths, "task-001")
    sdir = make_session(paths, "task-001", "BLOCKED")
    calls = []
    real = loop_mod._atomic_write_json
    monkeypatch.setattr(loop_mod, "_atomic_write_json",
                        lambda path, payload: (calls.append(str(path)), real(path, payload))[1])
    reap_session(sdir, Config(integration="branch"), paths)
    assert any(c.endswith("reaped.json") for c in calls)
    assert json.loads((sdir / "reaped.json").read_text())["status"] == "BLOCKED"


# ---- audit I-1: per-session reap lock (no double-integration) ---------------

def test_reap_skipped_while_another_holds_lock(git_repo):
    """A concurrent reaper holding the per-session lock makes reap_session skip (no-op):
    it returns False and touches nothing — status, result.json, reaped.json all unchanged."""
    import fcntl
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "COMPLETE")
    held = open(sdir / ".reap.lock", "w")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)            # stand in for a concurrent reaper
    try:
        assert reap_session(sdir, Config(integration="branch"), paths) is False
        assert read_task(paths.tasks_dir / "task-001.md").status == "in-progress"  # no-op
        assert not (sdir / "reaped.json").exists()
        assert (sdir / "result.json").exists()           # result untouched
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_reap_rechecks_reaped_json_under_lock(git_repo, monkeypatch):
    """If a concurrent reaper finished between our fast-path check and the lock, the
    UNDER-LOCK re-check catches it: no second integration / follow-up."""
    import contextlib as _ctx
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "COMPLETE",
                        followups=[{"title": "x", "priority": 2}])

    @_ctx.contextmanager
    def winner_finished(sd):
        (sd / "reaped.json").write_text("{}")            # a winner that finished in the gap
        yield

    monkeypatch.setattr(loop_mod, "_session_reap_lock", winner_finished)
    assert reap_session(sdir, Config(integration="branch"), paths) is False
    assert [p.name for p in paths.tasks_dir.glob("*.md")] == ["task-001.md"]  # no follow-up


def test_reap_recovers_after_lock_holder_is_killed(git_repo):
    """Crash safety: a SIGKILL'd lock holder auto-releases the flock, so the next reap
    succeeds — the headline property of using flock over a persistent marker."""
    import subprocess
    import sys
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = make_session(paths, "task-001", "COMPLETE")
    lock, ready = sdir / ".reap.lock", sdir / ".held"
    child = subprocess.Popen([sys.executable, "-c",
        "import fcntl,time;"
        f"f=open({str(lock)!r},'w');fcntl.flock(f.fileno(),fcntl.LOCK_EX);"
        f"open({str(ready)!r},'w').close();time.sleep(60)"])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert reap_session(sdir, Config(integration="branch"), paths) is False  # held -> skip
    finally:
        child.kill()
        child.wait()                                     # SIGKILL -> kernel releases the flock
    assert reap_session(sdir, Config(integration="branch"), paths) is True       # now reaps
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"


# ---- 107 slice: orphan-kill on reconcile -----------------------------------

def _orphan_session(paths, pgid_field):
    """An orphaned in-progress session: meta.json (with optional pgid), no result yet."""
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-x"
    sdir.mkdir(parents=True)
    meta = {"task": "task-001", "branch": "autobuild/task-001", **pgid_field}
    (sdir / "meta.json").write_text(json.dumps(meta))
    return sdir


def test_reconcile_kills_orphan_pgid_before_blocking(git_repo, monkeypatch):
    paths = setup(git_repo)
    pgid = os.getpgrp() + 100000                          # >1, not our group, not real
    sdir = _orphan_session(paths, {"pgid": pgid})
    events = []
    monkeypatch.setattr(loop_mod.os, "killpg", lambda p, s: events.append(("kill", p, s)))
    real = loop_mod.write_sentinel_if_absent
    monkeypatch.setattr(loop_mod, "write_sentinel_if_absent",
                        lambda *a, **k: (events.append(("block",)), real(*a, **k))[1])
    reconcile(paths, sweep_in_progress=True)
    assert ("kill", pgid, signal.SIGKILL) in events
    assert events.index(("kill", pgid, signal.SIGKILL)) < events.index(("block",))  # kill first
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"


@pytest.mark.parametrize("field", [{}, {"pgid": 0}, {"pgid": 1}, {"pgid": -5}, {"pgid": True}, {"pgid": "OWN"}])
def test_reconcile_never_kills_unsafe_pgid(git_repo, monkeypatch, field):
    paths = setup(git_repo)
    field = dict(field)
    if field.get("pgid") == "OWN":
        field["pgid"] = os.getpgrp()                      # never kill the harness's own group
    sdir = _orphan_session(paths, field)
    called = []
    monkeypatch.setattr(loop_mod.os, "killpg", lambda p, s: called.append((p, s)))
    reconcile(paths, sweep_in_progress=True)
    assert called == []                                   # never signalled
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"  # still blocked


@pytest.mark.parametrize("exc", [ProcessLookupError, PermissionError])
def test_reconcile_orphan_kill_suppresses_signal_errors(git_repo, monkeypatch, exc):
    paths = setup(git_repo)
    sdir = _orphan_session(paths, {"pgid": os.getpgrp() + 100000})

    def boom(p, s):
        raise exc()

    monkeypatch.setattr(loop_mod.os, "killpg", boom)
    reconcile(paths, sweep_in_progress=True)              # must not raise / abort the sweep
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"


def test_reconcile_kills_a_real_orphan_group(git_repo):
    """Confidence: a real setsid child group is actually killed. NOTE: here the test is the
    child's parent and must reap it; in production reconcile is NOT the parent (init reaps)."""
    import subprocess
    paths = setup(git_repo)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    assert pgid == proc.pid                               # own group leader
    sdir = _orphan_session(paths, {"pgid": pgid})
    try:
        reconcile(paths, sweep_in_progress=True)
        proc.wait(timeout=3)
        assert proc.poll() is not None                    # actually killed
        assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)               # fallback cleanup
        except ProcessLookupError:
            pass
        proc.wait()


def test_reconcile_without_sweep_never_kills(git_repo, monkeypatch):
    paths = setup(git_repo)
    sdir = _orphan_session(paths, {"pgid": os.getpgrp() + 100000})
    called = []
    monkeypatch.setattr(loop_mod.os, "killpg", lambda p, s: called.append(1))
    reconcile(paths)                                      # sweep gated off (default)
    assert called == []
    assert not (sdir / "result.json").exists()           # no BLOCK either
