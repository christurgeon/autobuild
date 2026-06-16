import json

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
from autobuild.session import RunningSession
from autobuild.tasks import read_task
from autobuild.worktree import make_worktree


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
