import json
import os

from autobuild.config import Config
from autobuild.paths import Paths
from autobuild import loop as loop_mod
from autobuild import session as session_mod
from autobuild.session import build_prompt, new_session_id, spawn_session
from autobuild.tasks import read_task


def make_project(repo):
    paths = Paths(repo)
    paths.tasks_dir.mkdir(parents=True)
    paths.ensure_runtime_dirs()
    return paths


def write_task(paths, tid="task-001"):
    p = paths.tasks_dir / f"{tid}.md"
    p.write_text(f"---\nid: {tid}\ntitle: {tid}\nstatus: todo\npriority: 1\ndepends_on: []\n---\n\n## Goal\nx\n",
                 encoding="utf-8")
    return read_task(p)


# ---- prompt + id ------------------------------------------------------------

def test_build_prompt_has_contract_lines_the_stub_parses():
    prompt = build_prompt("/s/dir", "/p/task-001.md", "/w/tree", "task-001",
                          "/root/GOAL.md", "/root/CLAUDE.md")
    import re
    assert re.search(r"session directory is:\s*(\S+)", prompt).group(1) == "/s/dir"
    assert re.search(r"Work ONLY on task\s+(\S+)", prompt).group(1).rstrip(".") == "task-001"
    assert "/p/task-001.md" in prompt
    assert "/w/tree" in prompt


def test_build_prompt_points_at_root_contract_not_the_worktree():
    """The session must read GOAL.md and CLAUDE.md by their ROOT absolute paths, not
    "in this worktree": a worktree forked from the committed HEAD won't contain them if
    the user edited GOAL/tasks but didn't commit before running. Pointing at the root
    copies makes the session see the source-of-truth contract either way."""
    prompt = build_prompt("/s/dir", "/p/task-001.md", "/w/tree", "task-001",
                          "/root/GOAL.md", "/root/CLAUDE.md")
    assert "/root/GOAL.md" in prompt and "/root/CLAUDE.md" in prompt
    assert "in this worktree" not in prompt


def test_new_session_id_unique_and_prefixed():
    a, b = new_session_id(), new_session_id()
    assert a != b
    assert a.startswith("sess-")


# ---- spawn ------------------------------------------------------------------

def test_spawn_invokes_claude_with_expected_argv(git_repo, monkeypatch, stub_pgid):
    paths = make_project(git_repo)
    task = write_task(paths)
    captured = {}

    class FakeProc:
        def poll(self):
            return None  # pretend still running

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kw):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return FakeProc()

    monkeypatch.setattr(session_mod, "Popen", fake_popen)
    cfg = Config(model="test-model", claude_cmd="claude")
    rs = spawn_session(task, cfg, paths)

    # stable prefix: claude -p <prompt> --model <model>, then the permission posture flags
    assert captured["argv"][:5] == [
        "claude", "-p",
        build_prompt(str(rs.sdir), str(task.path), str(rs.worktree), "task-001",
                     str(paths.goal_file), str(paths.claude_md)),
        "--model", "test-model",
    ]
    assert "--add-dir" in captured["argv"]          # posture flags follow the prefix
    assert "--strict-mcp-config" in captured["argv"]
    assert str(captured["cwd"]) == str(rs.worktree)


def test_spawn_writes_meta_and_sets_in_progress(git_repo, monkeypatch, stub_pgid):
    paths = make_project(git_repo)
    task = write_task(paths)
    monkeypatch.setattr(session_mod, "Popen",
                        lambda *a, **k: type("P", (), {"poll": lambda s: None})())
    rs = spawn_session(task, Config(), paths)

    meta = json.loads((rs.sdir / "meta.json").read_text())
    assert meta["task"] == "task-001"
    assert meta["branch"] == "autobuild/task-001"
    assert meta["status"] == "in-progress"
    assert read_task(task.path).status == "in-progress"


def test_spawn_worktree_failure_blocks_task(git_repo):
    paths = make_project(git_repo)
    task = write_task(paths)
    cfg = Config(base_branch="nonexistent-base")  # make_worktree will fail
    rs = spawn_session(task, cfg, paths)

    assert rs.proc is None
    result = json.loads((rs.sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"
    assert read_task(task.path).status == "blocked"


def test_missing_claude_binary_writes_needs_human(git_repo):
    paths = make_project(git_repo)
    task = write_task(paths)
    cfg = Config(claude_cmd="definitely-not-a-real-binary-xyz")
    rs = spawn_session(task, cfg, paths)

    assert rs.proc is None
    result = json.loads((rs.sdir / "result.json").read_text())
    assert result["status"] == "NEEDS_HUMAN"


def test_spawn_real_stub_produces_result_and_commit(git_repo, stub_bin):
    """End-to-end of one spawn against the stub claude (no monkeypatching)."""
    paths = make_project(git_repo)
    task = write_task(paths)
    rs = spawn_session(task, Config(claude_cmd="claude"), paths)
    rs.proc.wait()
    result = json.loads((rs.sdir / "result.json").read_text())
    assert result["status"] == "COMPLETE"
    assert result["task"] == "task-001"
    assert result["commit"]  # the stub committed in the worktree


# ---- task-104: process group + pgid -----------------------------------------

def test_spawn_passes_start_new_session(git_repo, monkeypatch, stub_pgid):
    paths = make_project(git_repo)
    task = write_task(paths)
    captured = {}

    def fake_popen(argv, **kw):
        captured.update(kw)
        return type("P", (), {"poll": lambda s: None})()

    monkeypatch.setattr(session_mod, "Popen", fake_popen)
    spawn_session(task, Config(), paths)
    assert captured.get("start_new_session") is True


def test_spawn_child_is_group_leader_and_pgid_persisted(git_repo, monkeypatch):
    """A REAL long-lived child launched via the real spawn path is its own group
    leader; its pgid is captured on the handle and atomically in meta.json. This is
    the ONLY test that exercises the real os.getpgid."""
    paths = make_project(git_repo)
    task = write_task(paths)
    real_popen = session_mod.Popen
    spawned = {}

    def slow_popen(argv, **kw):
        p = real_popen(["sleep", "5"], **kw)  # honors start_new_session in kw
        spawned["p"] = p
        return p

    monkeypatch.setattr(session_mod, "Popen", slow_popen)
    rs = spawn_session(task, Config(), paths)
    try:
        assert rs.pgid == rs.proc.pid                   # leader: pgid == pid
        assert os.getpgid(rs.proc.pid) == rs.proc.pid   # confirmed live
        assert json.loads((rs.sdir / "meta.json").read_text())["pgid"] == rs.proc.pid
    finally:
        p = spawned.get("p")                            # guard; don't mask a real error
        if p is not None:
            p.terminate()
            p.wait()


def test_process_group_id_none_when_child_gone(monkeypatch):
    """ESRCH (child already exited/reaped) -> None, not a stale pid."""
    def raise_esrch(pid):
        raise ProcessLookupError

    monkeypatch.setattr(session_mod.os, "getpgid", raise_esrch)
    fake = type("P", (), {"pid": 999})()
    assert session_mod._process_group_id(fake) is None


def test_missing_claude_returns_no_pgid_or_deadline(git_repo):
    """FileNotFoundError path: no live child, so pgid and deadline stay None."""
    paths = make_project(git_repo)
    task = write_task(paths)
    rs = spawn_session(task, Config(claude_cmd="definitely-not-real-xyz"), paths)
    assert rs.proc is None and rs.pgid is None and rs.deadline is None


def test_reconcile_handles_meta_with_pgid(git_repo):
    """Regression: meta.json now carries pgid; reconcile still BLOCKs an orphan."""
    from tests.test_loop import setup, add_task
    paths = setup(git_repo)
    add_task(paths, "task-001", status="in-progress")
    sdir = paths.sessions_dir / "sess-x"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps(
        {"task": "task-001", "branch": "autobuild/task-001", "pgid": 4242}))
    loop_mod.reconcile(paths, sweep_in_progress=True)
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"


# ---- task-104: monotonic deadline -------------------------------------------

def test_deadline_is_monotonic_based(git_repo, monkeypatch, stub_pgid):
    paths = make_project(git_repo)
    task = write_task(paths)
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(session_mod, "Popen",
                        lambda argv, **kw: type("P", (), {"poll": lambda s: None})())
    rs = spawn_session(task, Config(task_timeout_seconds=1800), paths)
    assert rs.deadline == 1000.0 + 1800


def test_deadline_separate_from_wall_clock(git_repo, monkeypatch, stub_pgid):
    """A wall-clock jump shifts the display-only `started`, NOT the monotonic deadline."""
    paths = make_project(git_repo)
    task = write_task(paths)
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: 500.0)

    class Frozen(session_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return session_mod.datetime(2999, 1, 1, tzinfo=tz)

    monkeypatch.setattr(session_mod, "datetime", Frozen)
    monkeypatch.setattr(session_mod, "Popen",
                        lambda argv, **kw: type("P", (), {"poll": lambda s: None})())
    rs = spawn_session(task, Config(task_timeout_seconds=60), paths)
    assert rs.deadline == 500.0 + 60                      # from monotonic
    meta = json.loads((rs.sdir / "meta.json").read_text())
    assert meta["started"].startswith("2999")            # wall clock is display-only


def test_spawn_conflicting_dependency_blocks_with_dep_named(git_repo, diverging_dep):
    """A dependency that cannot be merged into the new worktree base blocks the task,
    and the BLOCKED sentinel names the conflicting dependency."""
    paths = make_project(git_repo)
    diverging_dep(git_repo, "task-001")  # main and autobuild/task-001 conflict on shared.txt

    # task-001 is done; task-002 depends on it
    (paths.tasks_dir / "task-001.md").write_text(
        "---\nid: task-001\ntitle: t\nstatus: done\npriority: 1\ndepends_on: []\n---\n\nx\n",
        encoding="utf-8")
    dep_task = write_task(paths, "task-002")
    dep_task.depends_on = ["task-001"]

    rs = spawn_session(dep_task, Config(), paths)

    assert rs.proc is None
    result = json.loads((rs.sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"
    assert "task-001" in result["summary"]
    assert read_task(dep_task.path).status == "blocked"


# ---- audit I-2: scrub push/transport credentials from the child env ---------

def test_session_env_drops_push_credentials(monkeypatch):
    for k in ("GH_TOKEN", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND"):
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ab")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "keep-me")
    env = session_mod._session_env()
    assert "GH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert env["GIT_AUTHOR_NAME"] == "ab"          # commit identity preserved
    assert env["ANTHROPIC_API_KEY"] == "keep-me"   # the agent's own auth preserved
    assert "PATH" in env                            # a complete env, not a fragment


def test_spawn_scrubs_credentials_from_child_env(git_repo, monkeypatch, stub_pgid):
    paths = make_project(git_repo)
    task = write_task(paths)
    monkeypatch.setenv("GH_TOKEN", "secret")
    captured = {}
    monkeypatch.setattr(session_mod, "Popen",
                        lambda argv, **kw: (captured.update(kw),
                                            type("P", (), {"poll": lambda s: None})())[1])
    spawn_session(task, Config(), paths)
    assert "env" in captured
    assert "GH_TOKEN" not in captured["env"]
    assert "PATH" in captured["env"]                # the child still gets a full env


def test_session_env_drops_git_config_injection(monkeypatch):
    """GIT_CONFIG_COUNT/KEY_*/VALUE_*/PARAMETERS inject arbitrary git config (credential
    helpers, core.sshCommand) into every child git call — deny them. GIT_CONFIG_GLOBAL/
    SYSTEM (file pointers) and the AUTHOR/COMMITTER identity vars stay."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.sshCommand=evil'")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/gc")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "ab")
    env = session_mod._session_env()
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/tmp/gc"      # file pointer, benign -> kept
    assert env["GIT_COMMITTER_NAME"] == "ab"          # commit identity -> kept
