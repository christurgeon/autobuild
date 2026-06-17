import json

from autobuild.config import Config
from autobuild.paths import Paths
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
    prompt = build_prompt("/s/dir", "/p/task-001.md", "/w/tree", "task-001")
    import re
    assert re.search(r"session directory is:\s*(\S+)", prompt).group(1) == "/s/dir"
    assert re.search(r"Work ONLY on task\s+(\S+)", prompt).group(1).rstrip(".") == "task-001"
    assert "/p/task-001.md" in prompt
    assert "/w/tree" in prompt


def test_new_session_id_unique_and_prefixed():
    a, b = new_session_id(), new_session_id()
    assert a != b
    assert a.startswith("sess-")


# ---- spawn ------------------------------------------------------------------

def test_spawn_invokes_claude_with_expected_argv(git_repo, monkeypatch):
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
        build_prompt(str(rs.sdir), str(task.path), str(rs.worktree), "task-001"),
        "--model", "test-model",
    ]
    assert "--permission-mode" in captured["argv"]
    assert "--add-dir" in captured["argv"]
    assert str(captured["cwd"]) == str(rs.worktree)


def test_spawn_writes_meta_and_sets_in_progress(git_repo, monkeypatch):
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
