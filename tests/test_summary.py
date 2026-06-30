"""The end-of-run summary: `write_run_summary` + the `run-summary.json` a run leaves.

Drives the real `run` loop with the stub `claude` (token-free) and asserts the digest
file records the terminal reason, the per-task terminal status + integration outcome,
a blocked task's reason, and that even a halted run (BaseBranchLeak) leaves a summary.
"""

import json

import pytest

from autobuild import cli
from autobuild import loop as loop_mod
from autobuild.config import Config, load_config
from autobuild.loop import write_run_summary
from autobuild.paths import Paths


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
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "scaffold"], check=True)
    return paths


def write_task(paths, tid, priority=1, depends_on=()):
    deps = "[" + ", ".join(depends_on) + "]"
    (paths.tasks_dir / f"{tid}.md").write_text(
        f"---\nid: {tid}\ntitle: {tid}\nstatus: todo\npriority: {priority}\n"
        f"depends_on: {deps}\n---\n\n## Goal\nx\n", encoding="utf-8")


def _summary(paths):
    return json.loads(paths.run_summary.read_text())


def _row(summary, tid):
    return next(r for r in summary["tasks"] if r["id"] == tid)


def test_run_summary_written_on_clean_drain(git_repo, stub_bin, monkeypatch):
    stub_bin()  # every task COMPLETE
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert paths.run_summary.exists()
    summary = _summary(paths)
    assert summary["reason"] == "drained"
    assert summary["counts"].get("done") == 2
    assert summary["stuck"] == []
    for tid in ("task-001", "task-002"):
        row = _row(summary, tid)
        assert row["status"] == "done"
        assert row["integration"]["integrated"] is True
        assert row["integration"]["result"] == "COMPLETE"


def test_run_summary_lists_blocked_task_with_reason(git_repo, stub_bin, monkeypatch):
    # task-002 BLOCKED -> task-003 can never run (stuck behind a blocked dep).
    stub_bin(STUB_STATUS_task_002="BLOCKED")
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")
    write_task(paths, "task-002", depends_on=["task-001"])
    write_task(paths, "task-003", depends_on=["task-002"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    summary = _summary(paths)
    assert summary["reason"] == "settled"
    assert _row(summary, "task-001")["status"] == "done"

    blocked = _row(summary, "task-002")
    assert blocked["status"] == "blocked"
    assert blocked["integration"]["result"] == "BLOCKED"

    # the unsatisfiable dependent is surfaced in `stuck` with its one-line reason
    stuck = {s["task"]: s["reason"] for s in summary["stuck"]}
    assert "task-003" in stuck
    assert "blocked-dependency: task-002" in stuck["task-003"]


def test_run_summary_records_followups_filed(git_repo, stub_bin, monkeypatch):
    stub_bin(STUB_FOLLOWUPS=json.dumps([{"title": "discovered work", "priority": 2}]))
    paths = init_project(git_repo, monkeypatch, integration="branch")
    write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    summary = _summary(paths)
    row = _row(summary, "task-001")
    assert len(row["followups"]) == 1  # the filed follow-up's task id


def test_run_summary_written_on_halt(git_repo, stub_bin, monkeypatch):
    """A BaseBranchLeak halt still leaves a summary with reason `halted`, so even an
    aborted run is legible. The halt itself is re-raised (the summary write must not
    swallow it)."""
    stub_bin(STUB_LEAK_DIR=str(git_repo))  # the stub commits straight onto base
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")

    with pytest.raises(loop_mod.BaseBranchLeak):
        loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert paths.run_summary.exists()
    summary = _summary(paths)
    assert summary["reason"] == "halted"
    blocked = _row(summary, "task-001")
    assert blocked["status"] == "blocked"
    assert blocked["integration"]["leak"]  # the leaking commit shas


def test_run_summary_digest_is_printed(git_repo, stub_bin, monkeypatch, capsys):
    stub_bin()
    paths = init_project(git_repo, monkeypatch, integration="auto-merge")
    write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    out = capsys.readouterr().out
    assert "run ended: drained" in out
    assert "done=1" in out


def test_write_run_summary_survives_missing_sessions_dir(git_repo, monkeypatch):
    """write_run_summary is best-effort and must not require any sessions on disk —
    an empty backlog still yields a well-formed summary."""
    paths = init_project(git_repo, monkeypatch)
    summary = write_run_summary(paths, load_config(paths.config_file), "drained")
    assert summary["reason"] == "drained"
    assert summary["tasks"] == []
    assert paths.run_summary.exists()
