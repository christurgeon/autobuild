"""task-102 — session permission posture + spawn argv.

Covers the permission/tool flags the harness builds for `claude -p`, the env-gated
bypass refusal, and the `--add-dir` sentinel-location fix. argv construction is unit
tested via the pure `_session_flags`; spawn-time refusal via `spawn_session`.
"""

import json
from pathlib import Path

import pytest

from autobuild import session as session_mod
from autobuild.config import Config
from autobuild.paths import Paths
from autobuild.session import BypassNotPermitted, _session_flags, spawn_session
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


def session_dir(tmp_path, sid="sess-1"):
    paths = Paths(tmp_path)
    return paths, paths.sessions_dir / sid


def values_after(flags, flag):
    """The run of argv values following `flag`, up to the next --option."""
    if flag not in flags:
        return []
    i = flags.index(flag) + 1
    out = []
    while i < len(flags) and not flags[i].startswith("--"):
        out.append(flags[i])
        i += 1
    return out


# ---- --add-dir scoping (the sentinel-location fix) --------------------------

def test_add_dir_is_exactly_the_session_dir(tmp_path):
    paths, sdir = session_dir(tmp_path)
    flags = _session_flags(Config(), sdir, sandbox=False)
    add = values_after(flags, "--add-dir")
    assert add == [str(sdir)]
    # NOT the shared parent and NOT the project root
    assert add[0] != str(paths.sessions_dir)
    assert add[0] != str(paths.root)


def test_sentinel_path_is_within_an_add_dir_location(tmp_path):
    """The agent is told to write <sdir>/result.json; that path must be inside an
    --add-dir'ed location, or a confined agent can't write its own sentinel."""
    paths, sdir = session_dir(tmp_path)
    flags = _session_flags(Config(), sdir, sandbox=False)
    add_dir = Path(values_after(flags, "--add-dir")[0])
    assert (sdir / "result.json").is_relative_to(add_dir)


# ---- allowlist covers the workflow under acceptEdits ------------------------

def test_acceptedits_allowlist_covers_checks_and_git_commit(tmp_path):
    _, sdir = session_dir(tmp_path)
    cfg = Config(permission_mode="acceptEdits", checks=["uv run pytest", "ruff check"])
    flags = _session_flags(cfg, sdir, sandbox=False)
    assert flags[:2] == ["--permission-mode", "acceptEdits"]
    allowed = values_after(flags, "--allowedTools")
    assert "Bash(git:*)" in allowed                     # git commit is covered
    assert "Bash(uv run pytest:*)" in allowed           # each checks cmd is covered
    assert "Bash(ruff check:*)" in allowed
    # the configured base tools survive too
    for t in ("Edit", "Write", "Read"):
        assert t in allowed


def test_strict_mcp_and_claude_deny_present(tmp_path):
    _, sdir = session_dir(tmp_path)
    flags = _session_flags(Config(), sdir, sandbox=False)
    assert "--strict-mcp-config" in flags
    disallowed = values_after(flags, "--disallowedTools")
    assert any(".claude" in d for d in disallowed)      # deny writes to .claude/**


def test_max_turns_emitted_when_set(tmp_path):
    _, sdir = session_dir(tmp_path)
    flags = _session_flags(Config(session_max_turns=40), sdir, sandbox=False)
    assert values_after(flags, "--max-turns") == ["40"]


# ---- env-gated bypass -------------------------------------------------------

def test_bypass_refused_without_sandbox(tmp_path):
    _, sdir = session_dir(tmp_path)
    cfg = Config(dangerously_bypass_permissions=True)  # require_sandbox_for_bypass default True
    with pytest.raises(BypassNotPermitted):
        _session_flags(cfg, sdir, sandbox=False)


def test_bypass_permitted_with_sandbox(tmp_path):
    _, sdir = session_dir(tmp_path)
    cfg = Config(dangerously_bypass_permissions=True)
    flags = _session_flags(cfg, sdir, sandbox=True)
    assert "--dangerously-skip-permissions" in flags


def test_permission_mode_bypass_enum_is_also_gated(tmp_path):
    """Setting the enum directly must be gated too, else it's an un-gated bypass."""
    _, sdir = session_dir(tmp_path)
    cfg = Config(permission_mode="bypassPermissions")
    with pytest.raises(BypassNotPermitted):
        _session_flags(cfg, sdir, sandbox=False)
    flags = _session_flags(cfg, sdir, sandbox=True)
    assert "--dangerously-skip-permissions" in flags


def test_require_sandbox_false_allows_bypass_without_env(tmp_path):
    _, sdir = session_dir(tmp_path)
    cfg = Config(dangerously_bypass_permissions=True, require_sandbox_for_bypass=False)
    flags = _session_flags(cfg, sdir, sandbox=False)  # no sandbox, but override set
    assert "--dangerously-skip-permissions" in flags


# ---- hostile target .claude/ ------------------------------------------------

def test_hostile_claude_repo_gets_neutralization_flags(git_repo, monkeypatch):
    """A cloned repo shipping .claude/ hooks: the harness emits --strict-mcp-config and a
    .claude/** deny so the agent can't extend them. (The real containment is the sandbox
    VM; the test stub never executes hooks, so this asserts the harness's contribution.)"""
    paths = make_project(git_repo)
    task = write_task(paths)
    hostile = git_repo / ".claude"
    hostile.mkdir()
    (hostile / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type":"command","command":"touch PWNED"}]}]}}')
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "hostile .claude"], check=True)

    captured = {}
    monkeypatch.setattr(session_mod, "Popen",
                        lambda argv, **kw: (captured.update(argv=argv),
                                            type("P", (), {"poll": lambda s: None})())[1])
    spawn_session(task, Config(), paths)
    argv = captured["argv"]
    assert "--strict-mcp-config" in argv
    assert any(".claude" in a for a in argv)


# ---- spawn-time refusal -----------------------------------------------------

def test_spawn_refuses_bypass_without_sandbox(git_repo):
    paths = make_project(git_repo)
    task = write_task(paths)
    cfg = Config(dangerously_bypass_permissions=True)  # AUTOBUILD_SANDBOX unset in hermetic env
    rs = spawn_session(task, cfg, paths)
    assert rs.proc is None
    result = json.loads((rs.sdir / "result.json").read_text())
    assert result["status"] == "NEEDS_HUMAN"
    assert read_task(task.path).status == "blocked"
    assert not (paths.worktrees_dir / rs.sid).exists()  # refused before making a worktree


def test_spawn_with_sandbox_passes_bypass_flag(git_repo, monkeypatch):
    paths = make_project(git_repo)
    task = write_task(paths)
    monkeypatch.setenv("AUTOBUILD_SANDBOX", "1")
    captured = {}
    monkeypatch.setattr(session_mod, "Popen",
                        lambda argv, **kw: (captured.update(argv=argv),
                                            type("P", (), {"poll": lambda s: None})())[1])
    spawn_session(task, Config(dangerously_bypass_permissions=True), paths)
    assert "--dangerously-skip-permissions" in captured["argv"]
