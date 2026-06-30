"""The generic `notify_command` operator hook.

Two layers:
  - unit tests of the `_notify` choke point: the env-var contract, that an empty command
    fires nothing, and that every failure mode (non-zero exit, timeout, OSError) is
    swallowed with a warn and never raises;
  - integration tests driving the real `run` loop with the token-free stub `claude`,
    asserting the `done` / `halt` / `needs_human` events fire (and that an empty
    `notify_command` stays silent end-to-end).
"""

import subprocess

import pytest

from autobuild import cli
from autobuild import loop as loop_mod
from autobuild.config import Config, load_config
from autobuild.loop import _notify, _run_end_message
from autobuild.paths import Paths


# --- unit: the _notify choke point -------------------------------------------

def test_empty_notify_command_runs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(loop_mod.subprocess, "run",
                        lambda *a, **k: called.append((a, k)))
    _notify(Config(notify_command=""), "done", "msg")
    assert called == []  # disabled: no subprocess at all


def test_notify_passes_event_and_message_via_env(tmp_path):
    out = tmp_path / "event.txt"
    cfg = Config(notify_command=f'printf "%s|%s" "$AUTOBUILD_EVENT" "$AUTOBUILD_MESSAGE" > {out}')
    _notify(cfg, "needs_human", "task-007 needs a human")
    assert out.read_text() == "needs_human|task-007 needs a human"


def test_notify_swallows_nonzero_exit(capsys):
    _notify(Config(notify_command="exit 3"), "done", "msg")  # must NOT raise
    err = capsys.readouterr().out
    assert "notify_command exited 3" in err
    assert "done" in err


def test_notify_swallows_timeout(monkeypatch, capsys):
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=loop_mod._NOTIFY_TIMEOUT_SECONDS)
    monkeypatch.setattr(loop_mod.subprocess, "run", _raise_timeout)
    _notify(Config(notify_command="sleep 99"), "halt", "msg")  # must NOT raise / sleep
    assert "timed out" in capsys.readouterr().out


def test_notify_swallows_oserror(monkeypatch, capsys):
    def _raise_oserror(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(loop_mod.subprocess, "run", _raise_oserror)
    _notify(Config(notify_command="whatever"), "done", "msg")  # must NOT raise
    assert "notify_command failed" in capsys.readouterr().out


def test_notify_timeout_is_bounded(monkeypatch):
    seen = {}
    def _capture(*a, **k):
        seen.update(k)
        class R:  # minimal CompletedProcess stand-in
            returncode = 0
        return R()
    monkeypatch.setattr(loop_mod.subprocess, "run", _capture)
    _notify(Config(notify_command="true"), "done", "msg")
    assert seen.get("timeout") == loop_mod._NOTIFY_TIMEOUT_SECONDS  # wedged notifier can't hang


def test_run_end_message_includes_reason_and_counts():
    msg = _run_end_message({"reason": "drained", "counts": {"done": 2, "blocked": 1}})
    assert "drained" in msg and "done=2" in msg and "blocked=1" in msg


# --- integration: events fire from the real run loop -------------------------

def _init_project(git_repo, monkeypatch, *, integration, notify_log=None, max_parallel=5):
    monkeypatch.chdir(git_repo)
    cli.main(["init"])
    paths = Paths(git_repo)
    for f in paths.tasks_dir.glob("*.md"):
        f.unlink()  # drop the example task
    cfg = (paths.config_file.read_text()
           .replace("integration: pr", f"integration: {integration}")
           .replace("max_parallel: 3", f"max_parallel: {max_parallel}"))
    if notify_log is not None:
        # Append "<event>|<message>" lines to a log OUTSIDE the repo working tree.
        cmd = (f'printf "%s|%s\\n" "$AUTOBUILD_EVENT" "$AUTOBUILD_MESSAGE" >> {notify_log}')
        cfg = cfg.replace('notify_command: ""', f"notify_command: '{cmd}'")
    paths.config_file.write_text(cfg)
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "scaffold"], check=True)
    return paths


def _write_task(paths, tid, priority=1, depends_on=()):
    deps = "[" + ", ".join(depends_on) + "]"
    (paths.tasks_dir / f"{tid}.md").write_text(
        f"---\nid: {tid}\ntitle: {tid}\nstatus: todo\npriority: {priority}\n"
        f"depends_on: {deps}\n---\n\n## Goal\nx\n", encoding="utf-8")


def _events(notify_log):
    if not notify_log.exists():
        return []
    return [ln.split("|", 1) for ln in notify_log.read_text().splitlines() if ln]


def test_fires_done_on_clean_drain(git_repo, stub_bin, monkeypatch, tmp_path):
    stub_bin()  # every task COMPLETE
    log = tmp_path / "events.log"
    paths = _init_project(git_repo, monkeypatch, integration="auto-merge", notify_log=log)
    _write_task(paths, "task-001")
    _write_task(paths, "task-002", depends_on=["task-001"])

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    events = _events(log)
    done = [m for e, m in events if e == "done"]
    assert len(done) == 1                       # run-level, fired exactly once
    assert "drained" in done[0] and "done=2" in done[0]  # message says what merged


def test_fires_needs_human(git_repo, stub_bin, monkeypatch, tmp_path):
    stub_bin(STUB_STATUS="NEEDS_HUMAN")
    log = tmp_path / "events.log"
    paths = _init_project(git_repo, monkeypatch, integration="branch", notify_log=log)
    _write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    events = _events(log)
    nh = [m for e, m in events if e == "needs_human"]
    assert len(nh) == 1 and "task-001" in nh[0]
    assert any(e == "done" for e, _ in events)  # the run still ends -> done also fires


def test_fires_halt_on_base_leak(git_repo, stub_bin, monkeypatch, tmp_path):
    stub_bin(STUB_LEAK_DIR=str(git_repo))  # stub commits straight onto base -> BaseBranchLeak
    log = tmp_path / "events.log"
    paths = _init_project(git_repo, monkeypatch, integration="auto-merge", notify_log=log)
    _write_task(paths, "task-001")

    with pytest.raises(loop_mod.BaseBranchLeak):
        loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    events = _events(log)
    halt = [m for e, m in events if e == "halt"]
    assert len(halt) == 1 and "task-001" in halt[0]
    assert not any(e == "done" for e, _ in events)  # a halt does NOT also fire `done`


def test_empty_notify_command_fires_nothing_end_to_end(git_repo, stub_bin, monkeypatch, tmp_path):
    stub_bin()
    log = tmp_path / "events.log"
    paths = _init_project(git_repo, monkeypatch, integration="auto-merge", notify_log=None)
    _write_task(paths, "task-001")

    loop_mod.run(paths, load_config(paths.config_file), sleep_seconds=5)

    assert not log.exists()  # default "" -> never invoked
