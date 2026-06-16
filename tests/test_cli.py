import importlib.resources as ir

import pytest

from autobuild import cli
from autobuild.paths import Paths


def template(*parts):
    t = ir.files("autobuild") / "templates"
    for p in parts:
        t = t / p
    return t.read_text(encoding="utf-8")


def test_init_lays_down_templates_byte_identical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    paths = Paths(tmp_path)
    assert paths.goal_file.read_text() == template("GOAL.md")
    assert paths.claude_md.read_text() == template("CLAUDE.md")
    assert paths.config_file.read_text() == template("config.yml")
    assert (paths.tasks_dir / "task-001-example.md").read_text() == template("tasks", "task-001-example.md")
    assert paths.sessions_dir.is_dir() and paths.worktrees_dir.is_dir()


def test_init_is_idempotent_and_preserves_edits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    Paths(tmp_path).goal_file.write_text("MY EDITED GOAL\n")
    cli.main(["init"])  # second init must not clobber
    assert Paths(tmp_path).goal_file.read_text() == "MY EDITED GOAL\n"


def test_status_runs_from_fresh_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "TASKS BY STATE" in out
    assert "task-001" in out


def test_status_requires_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["status"])
    assert e.value.code == 1


def test_no_command_prints_help(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert "autobuild" in capsys.readouterr().out


def test_unknown_command_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["frobnicate"])
    assert e.value.code == 2
