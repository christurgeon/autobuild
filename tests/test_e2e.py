"""End-to-end: drive the real `run` loop with the stub `claude`, in a throwaway
git repo, spending no tokens. Proves dependency-ordered execution, gating on a
blocked dependency, stall recovery, and follow-up filing through spawn+reap."""

import json

from autobuild import cli
from autobuild import loop as loop_mod
from autobuild.config import load_config
from autobuild.paths import Paths
from autobuild.session import spawn_session
from autobuild.tasks import read_task
from autobuild.worktree import branch_name


def init_project(git_repo, monkeypatch, integration="auto-merge", max_parallel=5):
    monkeypatch.chdir(git_repo)
    cli.main(["init"])
    paths = Paths(git_repo)
    for f in paths.tasks_dir.glob("*.md"):
        f.unlink()  # drop the example task
    cfg = (paths.config_file.read_text()
           .replace("integration: pr", f"integration: {integration}")
           .replace("max_parallel: 3", f"max_parallel: {max_parallel}"))
    paths.config_file.write_text(cfg)
    return paths


def write_task(paths, tid, priority=1, depends_on=()):
    deps = "[" + ", ".join(depends_on) + "]"
    (paths.tasks_dir / f"{tid}.md").write_text(
        f"---\nid: {tid}\ntitle: {tid}\nstatus: todo\npriority: {priority}\n"
        f"depends_on: {deps}\n---\n\n## Goal\nx\n", encoding="utf-8")


def statuses(paths):
    return {read_task(p).id: read_task(p).status for p in paths.tasks_dir.glob("*.md")}


def test_e2e_runs_chain_in_dependency_order(git_repo, stub_bin, monkeypatch, git):
    stub_bin()  # default: every task COMPLETE
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    write_task(paths, "task-003", depends_on=["task-002"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths) == {"task-001": "done", "task-002": "done", "task-003": "done"}
    # auto-merge deletes each redundant branch after merging it (see the dedicated test);
    # the chain still completes because each dependent forks from base + skips the gone branch
    for tid in ("task-001", "task-002", "task-003"):
        assert git(git_repo, "show-ref", "--verify", "--quiet",
                   f"refs/heads/{branch_name(tid)}", check=False).returncode != 0

    # merge commits land on main in dependency order (newest first in the log)
    merges = git(git_repo, "log", "--merges", "--oneline", "main").stdout
    i1, i2, i3 = (merges.index(f"merge task-00{n}") for n in (1, 2, 3))
    assert i3 < i2 < i1


def test_e2e_auto_merge_deletes_the_redundant_branch(git_repo, stub_bin, monkeypatch, git):
    """After a successful auto-merge the autobuild/<tid> branch is deleted: its commits
    already live on base_branch via the merge commit, so the branch is redundant and would
    otherwise accumulate. A dependent still builds correctly — it forks from base (which has
    the dep) and the layering step skips the now-gone branch."""
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])  # proves the chain still works

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths) == {"task-001": "done", "task-002": "done"}
    for tid in ("task-001", "task-002"):
        # the merge landed on main...
        assert f"merge {tid}" in git(git_repo, "log", "--merges", "--oneline", "main").stdout
        # ...and the redundant branch is gone
        assert git(git_repo, "show-ref", "--verify", "--quiet",
                   f"refs/heads/{branch_name(tid)}", check=False).returncode != 0


def test_e2e_pr_mode_dependency_visible_in_downstream_worktree(git_repo, stub_bin, monkeypatch):
    """pr mode never lands a dep on base_branch, yet the dependent must still see the
    dep's committed files in its worktree at spawn (the core fix)."""
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="pr")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    config = load_config(paths.config_file)

    # run + reap task-001; in pr mode its branch is left, NOT merged into main
    rs1 = spawn_session(read_task(paths.tasks_dir / "task-001.md"), config, paths)
    rs1.proc.wait()
    loop_mod.reap_all(config, paths)
    assert statuses(paths)["task-001"] == "done"

    # spawning the dependent layers autobuild/task-001 onto its base at creation time
    rs2 = spawn_session(read_task(paths.tasks_dir / "task-002.md"), config, paths)
    assert (rs2.worktree / "task-001.txt").exists()  # visible before its own work runs
    rs2.proc.wait()


def test_e2e_branch_mode_transitive_chain_layers_all_ancestors(git_repo, stub_bin, monkeypatch, git):
    """branch mode A<-B<-C through the real loop: C's branch must contain every
    ancestor's file even though nothing is ever merged into main."""
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    write_task(paths, "task-003", depends_on=["task-002"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths) == {"task-001": "done", "task-002": "done", "task-003": "done"}
    c = branch_name("task-003")
    for f in ("task-001.txt", "task-002.txt", "task-003.txt"):
        assert git(git_repo, "show", f"{c}:{f}", check=False).returncode == 0, f
    # main is untouched in branch mode (the layering happens only on the task branches)
    assert git(git_repo, "show", "main:task-001.txt", check=False).returncode != 0


def test_e2e_blocked_dependency_gates_downstream(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_STATUS_task_002="BLOCKED")
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    write_task(paths, "task-003", depends_on=["task-002"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    s = statuses(paths)
    assert s["task-001"] == "done"
    assert s["task-002"] == "blocked"
    assert s["task-003"] == "todo"  # never ran; its dep never completed


def test_e2e_stalled_session_becomes_blocked_and_loop_terminates(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_STALL="1")  # stub exits without writing result.json
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths)["task-001"] == "blocked"


def test_e2e_run_end_names_stuck_task(git_repo, stub_bin, monkeypatch, capsys):
    stub_bin(STUB_STATUS_task_002="BLOCKED")
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    write_task(paths, "task-003", depends_on=["task-002"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    out = capsys.readouterr().out
    # the run-end report names the stuck task and its reason, not the generic line
    assert "task-003" in out
    assert "blocked-dependency: task-002" in out
    assert "backlog drained — COMPLETE" not in out


def test_e2e_run_end_reports_complete_on_clean_drain(git_repo, stub_bin, monkeypatch, capsys):
    stub_bin()  # every task COMPLETE
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    out = capsys.readouterr().out
    assert "backlog drained — COMPLETE" in out
    assert "cannot proceed" not in out


def test_e2e_followup_filed_through_spawn_and_reap(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_FOLLOWUPS=json.dumps([{"title": "discovered work", "priority": 2}]))
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    config = load_config(paths.config_file)

    rs = spawn_session(read_task(paths.tasks_dir / "task-001.md"), config, paths)
    rs.proc.wait()
    loop_mod.reap_all(config, paths)

    assert statuses(paths)["task-001"] == "done"
    names = [p.name for p in paths.tasks_dir.glob("*.md")]
    assert any("discovered-work" in n for n in names)


def test_e2e_waiting_for_a_session_does_not_burn_max_iterations(git_repo, stub_bin, monkeypatch):
    """Regression: a session that spans many poll passes must NOT exhaust the
    max_iterations safety budget. The cap counts scheduling rounds (work started),
    not poll ticks — otherwise a single real (non-instant) session trips the cap and
    its finished work is stranded `in-progress`. The stub here sleeps ~1s while the
    loop polls every 0.05s with max_iterations=3: under the old (poll-tick) counting
    the loop would bail after ~0.15s, before the session ever finishes."""
    stub_bin(STUB_SLEEP="1.0")  # one session, far longer than max_iterations * sleep
    paths = init_project(git_repo, monkeypatch, integration="auto-merge", max_parallel=1)
    paths.config_file.write_text(
        paths.config_file.read_text().replace("max_iterations: 100", "max_iterations: 3"))
    write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)

    assert statuses(paths) == {"task-001": "done"}


def test_e2e_max_iterations_drains_inflight_and_reports_leftover(git_repo, stub_bin, monkeypatch, capsys):
    """When the max_iterations budget is genuinely spent, the loop stops launching NEW
    work but still drains the session already running (so its task reaches `done`, not
    stranded in-progress), leaves the unstarted task recoverable as `todo`, and reports
    the cap with the unfinished count instead of a false clean drain."""
    stub_bin()  # instant COMPLETE
    paths = init_project(git_repo, monkeypatch, integration="auto-merge", max_parallel=1)
    paths.config_file.write_text(
        paths.config_file.read_text().replace("max_iterations: 100", "max_iterations: 1"))
    write_task(paths, "task-001")
    write_task(paths, "task-002")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)

    s = statuses(paths)
    assert s["task-001"] == "done"     # the one we launched was drained, not stranded
    assert s["task-002"] == "todo"     # never claimed -> recoverable on the next run
    out = capsys.readouterr().out
    assert "hit max_iterations (1)" in out
    assert "backlog drained — COMPLETE" not in out
