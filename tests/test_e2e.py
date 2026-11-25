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
    # Commit the scaffolding init laid down (GOAL.md, .gitignore): `run` now
    # refuses a dirty base tree, and an uncommitted GOAL.md is exactly the kind of
    # stray edit a worktree-escaping session could sweep. tasks/ + .autobuild/ are exempt.
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "scaffold"], check=True)
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


def test_e2e_drain_with_blocked_task_is_not_reported_complete(git_repo, stub_bin, monkeypatch,
                                                              capsys):
    """Live run 2026-11-22: every task was terminal but one was blocked, and the run still
    printed "backlog drained — COMPLETE". The headline now names the failures."""
    stub_bin(STUB_STATUS_task_002="BLOCKED")
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002")  # independent: nothing is stuck, the backlog drains

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    out = capsys.readouterr().out
    assert "backlog drained — COMPLETE" not in out
    assert "backlog drained with unfinished work: blocked=1  done=1" in out
    assert "run ended: drained" in out  # the run-end reason is unchanged


def test_e2e_cli_run_exits_3_when_a_task_is_blocked(git_repo, stub_bin, monkeypatch, capsys):
    """`autobuild run && deploy` must not deploy after a failure: a run that ends with any
    task not `done` exits EXIT_UNFINISHED (3), not 0."""
    stub_bin(STUB_STATUS_task_002="BLOCKED")
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002")

    assert cli.main(["run"]) == cli.EXIT_UNFINISHED == 3
    assert statuses(paths) == {"task-001": "done", "task-002": "blocked"}
    assert "task-002 (blocked)" in capsys.readouterr().err


def test_e2e_cli_run_exits_3_when_a_task_times_out(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_SLEEP="30")  # outlives the 1s deadline -> killed -> synthetic TIMEOUT
    paths = init_project(git_repo, monkeypatch, integration="branch")
    _set_cfg(paths, "task_timeout_seconds: 3600", "task_timeout_seconds: 1")
    _set_cfg(paths, "kill_grace_seconds: 20", "kill_grace_seconds: 1")
    _set_cfg(paths, "timeout_max_retries: 2", "timeout_max_retries: 0")
    write_task(paths, "task-001")

    assert cli.main(["run"]) == 3
    assert statuses(paths) == {"task-001": "timeout"}


def test_e2e_cli_run_exits_3_with_invalid_task_file(git_repo, stub_bin, monkeypatch, capsys):
    """A settled backlog stuck on an unparseable task file is not complete."""
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    (paths.tasks_dir / "task-002.md").write_text("---\nid: task-002\nstatus: [todo\n---\n")

    assert cli.main(["run"]) == 3
    assert "task-002 (invalid)" in capsys.readouterr().err


def test_e2e_cli_run_exits_3_when_rate_limited_with_todo_left(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_RATE_LIMIT_task_001="event")
    paths = init_project(git_repo, monkeypatch, integration="branch", max_parallel=1)
    write_task(paths, "task-001")
    write_task(paths, "task-002")

    assert cli.main(["run"]) == 3
    assert set(statuses(paths).values()) == {"todo"}  # nothing blocked, but work remains
    assert json.loads(paths.run_summary.read_text())["reason"] == "rate_limited"


def test_e2e_cli_run_exits_0_on_clean_drain(git_repo, stub_bin, monkeypatch):
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])

    assert cli.main(["run"]) == 0
    assert statuses(paths) == {"task-001": "done", "task-002": "done"}


def test_e2e_cli_run_signal_after_supervise_exits_128_plus_signum(git_repo, stub_bin,
                                                                    monkeypatch):
    """A SIGTERM that lands after the loop ended (e.g. during the status print) is still
    honored: exit 143, not 0/3."""
    import signal

    from autobuild import interrupt, supervisor
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    real_status = supervisor.status

    def status_then_signal(*a, **kw):
        real_status(*a, **kw)
        interrupt._on_signal(signal.SIGTERM, None)
    monkeypatch.setattr(supervisor, "status", status_then_signal)

    assert cli.main(["run"]) == 128 + signal.SIGTERM
    assert statuses(paths) == {"task-001": "done"}


def test_e2e_malformed_task_file_settles_not_crashes(git_repo, stub_bin, monkeypatch, capsys):
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    bad = paths.tasks_dir / "task-002.md"
    bad.write_text("---\nid: task-002\ntitle: [unclosed\nstatus: todo\n---\n", encoding="utf-8")
    before = bad.read_bytes()
    (paths.tasks_dir / "README.md").write_text("# Backlog notes\n", encoding="utf-8")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert json.loads(paths.run_summary.read_text())["reason"] == "settled"
    assert read_task(paths.tasks_dir / "task-001.md").status == "done"
    assert bad.read_bytes() == before
    assert "invalid-task-file" in capsys.readouterr().out


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


def test_e2e_session_out_streams_parseable_json(git_repo, stub_bin, monkeypatch):
    """Issue #40: session.out is now stream-json (the default text format buffered it to
    0 bytes). After a run it must be non-empty and parse to per-session progress — the
    assistant-message count and the terminal result event's cost."""
    from autobuild.progress import read_progress
    stub_bin(STUB_MSGS=3, STUB_COST=0.07)
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths)["task-001"] == "done"
    sdir = next(d for d in paths.sessions_dir.iterdir() if d.is_dir())
    out = (sdir / "session.out").read_text()
    assert out.strip(), "session.out must not be empty (stream-json, not buffered text)"
    prog = read_progress(sdir / "session.out")
    assert prog.messages == 3
    assert prog.finished is True
    assert abs(prog.cost_usd - 0.07) < 1e-9


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


def _set_budget_usd(paths, value):
    paths.config_file.write_text(
        paths.config_file.read_text() + f"\nrun_budget_usd: {value}\n")


def test_e2e_cost_budget_stops_claiming_new_work(git_repo, stub_bin, monkeypatch, capsys):
    """Issue #41: once cumulative session cost crosses run_budget_usd, the loop stops
    claiming new work, drains what's in flight, and reports the cap. max_parallel=1 makes
    the accounting deterministic: $0.40/session vs a $1.00 budget -> the 4th task is never
    claimed (after 3 sessions = $1.20 >= $1.00)."""
    stub_bin(STUB_COST="0.40", STUB_MSGS="1")
    paths = init_project(git_repo, monkeypatch, integration="branch", max_parallel=1)
    _set_budget_usd(paths, "1.0")
    for n in range(1, 5):
        write_task(paths, f"task-00{n}")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)

    s = statuses(paths)
    done = sum(1 for v in s.values() if v == "done")
    assert done < 4                       # budget tripped before finishing all
    assert sum(1 for v in s.values() if v == "todo") >= 1   # leftover recoverable
    out = capsys.readouterr().out
    assert "hit run_budget_usd" in out
    summary = json.loads(paths.run_summary.read_text())
    assert summary["reason"] == "run_budget_usd"


def test_e2e_cost_budget_is_per_run_so_backlog_resumes(git_repo, stub_bin, monkeypatch):
    """Regression for the resumability trap: run_budget_usd is scoped to the sessions a run
    spawns, NOT every persisted session dir. So re-running a budget-capped backlog starts
    fresh (not pre-charged by the prior run's costs) and finishes the leftover work."""
    stub_bin(STUB_COST="0.40", STUB_MSGS="1")
    paths = init_project(git_repo, monkeypatch, integration="branch", max_parallel=1)
    _set_budget_usd(paths, "1.0")
    for n in range(1, 5):
        write_task(paths, f"task-00{n}")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    first = statuses(paths)
    assert sum(1 for v in first.values() if v == "todo") >= 1   # budget left work undone

    # A second run on the same project (no clean): the budget is NOT pre-charged by the
    # first run's persisted session dirs, so it claims and finishes the remaining task(s).
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    assert all(v == "done" for v in statuses(paths).values())


def test_e2e_run_halts_on_base_leak(git_repo, stub_bin, monkeypatch, git):
    """A session that escapes its worktree and commits onto base is caught end-to-end:
    `run` raises BaseBranchLeak (exit 2 at the CLI), blocks the task, and writes a
    leak.json marker — it does NOT integrate onto the corrupted base."""
    import glob

    import pytest

    stub_bin(STUB_LEAK_DIR=str(git_repo))  # the stub commits straight onto base
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")

    with pytest.raises(loop_mod.BaseBranchLeak):
        loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert statuses(paths)["task-001"] == "blocked"
    markers = glob.glob(str(paths.sessions_dir / "*" / "leak.json"))
    assert markers, "expected a leak.json forensic marker"
    leak = json.loads(open(markers[0]).read())
    assert leak["base_branch"] == "main" and leak["commits"]
    # no integration merge landed on base (the leak commit is base's tip, not a merge)
    assert "Merge" not in git(git_repo, "log", "-1", "--format=%s", "main").stdout


# ---- graceful interrupt (plan D1): Ctrl-C / SIGTERM ---------------------------

def _set_cfg(paths, old, new):
    cfg = paths.config_file.read_text()
    assert old in cfg
    paths.config_file.write_text(cfg.replace(old, new))


def _signal_when_ready(paths, n, sigs, gap=0.3, timeout=15.0):
    """Background thread: once `n` stub sessions are ready (signal handlers installed),
    send `sigs` to THIS process, `gap` seconds apart. Sends nothing if they never get
    ready, so a broken run can't deliver a stray SIGINT to pytest itself."""
    import os
    import threading
    import time

    def go():
        deadline = time.monotonic() + timeout
        while len(list(paths.sessions_dir.glob("*/stub-ready"))) < n:
            if time.monotonic() > deadline:
                return
            time.sleep(0.02)
        for i, sig in enumerate(sigs):
            if i:
                time.sleep(gap)
            os.kill(os.getpid(), sig)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _session_pgids(paths):
    return [json.loads(m.read_text())["pgid"] for m in paths.sessions_dir.glob("*/meta.json")]


def _interrupted_run(git_repo, stub_bin, monkeypatch, sigs, *, n=2, grace=5, **stub_env):
    """Start a run of `n` sleeping tasks, interrupt it with `sigs`; return
    (paths, RunInterrupted, elapsed seconds)."""
    import time

    import pytest

    stub_bin(STUB_SLEEP="30", **stub_env)
    paths = init_project(git_repo, monkeypatch, integration="auto-merge", max_parallel=n)
    _set_cfg(paths, "kill_grace_seconds: 20", f"kill_grace_seconds: {grace}")
    for i in range(1, n + 1):
        write_task(paths, f"task-00{i}")
    _signal_when_ready(paths, n, sigs)
    t0 = time.monotonic()
    with pytest.raises(loop_mod.RunInterrupted) as ei:
        loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.2)
    return paths, ei.value, time.monotonic() - t0


def test_e2e_sigint_kills_sessions_and_requeues(git_repo, stub_bin, monkeypatch):
    import os
    import signal

    import pytest

    from autobuild.retries import retry_count

    before = signal.getsignal(signal.SIGINT)
    paths, exc, elapsed = _interrupted_run(git_repo, stub_bin, monkeypatch, [signal.SIGINT])
    assert exc.signum == signal.SIGINT
    assert elapsed < 15
    for pgid in _session_pgids(paths):
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
    assert statuses(paths) == {"task-001": "todo", "task-002": "todo"}
    assert retry_count(paths.retries_ledger, "task-001") == 0
    assert json.loads(paths.run_summary.read_text())["reason"] == "interrupted"
    with loop_mod.run_lock(paths.run_lock):
        pass                                            # lock released
    assert signal.getsignal(signal.SIGINT) is before   # handler restored


def test_e2e_second_sigint_forces_kill(git_repo, stub_bin, monkeypatch):
    import os
    import signal

    import pytest

    paths, exc, elapsed = _interrupted_run(
        git_repo, stub_bin, monkeypatch, [signal.SIGINT, signal.SIGINT], grace=10,
        STUB_IGNORE_SIGTERM="1")
    assert exc.signum == signal.SIGINT
    assert elapsed < 8   # well under the 10s grace (first signal waits for stub-ready)
    for pgid in _session_pgids(paths):
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)


def test_e2e_sigterm_behaves_like_sigint(git_repo, stub_bin, monkeypatch):
    import signal

    paths, exc, _ = _interrupted_run(git_repo, stub_bin, monkeypatch, [signal.SIGTERM])
    assert exc.signum == signal.SIGTERM
    assert statuses(paths) == {"task-001": "todo", "task-002": "todo"}


def test_e2e_interrupt_then_rerun_drains(git_repo, stub_bin, monkeypatch):
    import signal

    from autobuild.retries import retry_count

    paths, _, _ = _interrupted_run(git_repo, stub_bin, monkeypatch, [signal.SIGINT])
    stub_bin(STUB_SLEEP="0")
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    assert statuses(paths) == {"task-001": "done", "task-002": "done"}
    assert retry_count(paths.retries_ledger, "task-001") == 0


def test_e2e_interrupt_keeps_result_written_during_grace(git_repo, stub_bin, monkeypatch):
    """A session that writes a real COMPLETE during the shutdown grace keeps it: it is not
    overwritten with INTERRUPTED, is left reapable, and the next run integrates it."""
    import signal

    paths, _, _ = _interrupted_run(git_repo, stub_bin, monkeypatch, [signal.SIGINT], n=1,
                                   STUB_COMPLETE_ON_SIGTERM="1")
    results = [json.loads(p.read_text()) for p in paths.sessions_dir.glob("*/result.json")]
    assert [r["status"] for r in results] == ["COMPLETE"]
    assert not list(paths.sessions_dir.glob("*/reaped.json"))
    assert statuses(paths) == {"task-001": "in-progress"}
    stub_bin(STUB_SLEEP="0", STUB_COMPLETE_ON_SIGTERM="0")
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    assert statuses(paths) == {"task-001": "done"}


# ---- usage / rate limits (plan D2) ------------------------------------------

def _rate_limited_run(git_repo, stub_bin, monkeypatch, max_parallel=1, **stub_env):
    stub_bin(**stub_env)
    paths = init_project(git_repo, monkeypatch, integration="auto-merge",
                         max_parallel=max_parallel)
    for i in (1, 2, 3):
        write_task(paths, f"task-00{i}")
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    return paths


def test_e2e_rate_limit_requeues_and_stops_claiming(git_repo, stub_bin, monkeypatch):
    paths = _rate_limited_run(git_repo, stub_bin, monkeypatch, STUB_RATE_LIMIT_task_001="event")
    assert statuses(paths) == {"task-001": "todo", "task-002": "todo", "task-003": "todo"}
    assert len(list(paths.sessions_dir.iterdir())) == 1       # 002/003 never claimed
    summary = json.loads(paths.run_summary.read_text())
    assert summary["reason"] == "rate_limited"
    assert summary["rate_limited"][0]["task"] == "task-001"
    assert "five_hour" in summary["rate_limited"][0]["detail"]


def test_e2e_rate_limit_resume_next_run_completes(git_repo, stub_bin, monkeypatch):
    from autobuild.retries import retry_count

    paths = _rate_limited_run(git_repo, stub_bin, monkeypatch,
                              STUB_RATE_LIMIT_task_001="assistant")
    stub_bin(STUB_RATE_LIMIT_task_001="")
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    assert set(statuses(paths).values()) == {"done"}
    assert retry_count(paths.retries_ledger, "task-001") == 0
    assert json.loads(paths.run_summary.read_text())["reason"] == "drained"


def test_e2e_rate_limit_notifies_once(git_repo, stub_bin, monkeypatch, tmp_path):
    log = tmp_path / "events.log"
    stub_bin(STUB_RATE_LIMIT="event")
    paths = init_project(git_repo, monkeypatch, integration="auto-merge", max_parallel=2)
    cfg = paths.config_file.read_text().replace(
        'notify_command: ""', f"notify_command: 'echo $AUTOBUILD_EVENT >> {log}'")
    paths.config_file.write_text(cfg)
    for i in (1, 2, 3):
        write_task(paths, f"task-00{i}")
    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=0.05)
    events = log.read_text().split()
    assert events.count("rate_limited") == 1
    assert "blocked" not in statuses(paths).values()
