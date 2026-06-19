import importlib.resources as ir

import pytest
import yaml

from autobuild import cli
from autobuild import loop as loop_mod
from autobuild.paths import Paths


def template(*parts):
    t = ir.files("autobuild") / "templates"
    for p in parts:
        t = t / p
    return t.read_text(encoding="utf-8")


def packaged_skills():
    root = ir.files("autobuild") / "templates" / "skills"
    return [s for s in root.iterdir() if s.is_dir()]


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


def test_init_installs_skills_byte_identical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    skills_dir = Paths(tmp_path).skills_dir
    expected = {s.name for s in packaged_skills()}
    assert expected, "no skills are packaged under templates/skills/"
    installed = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert expected <= installed
    for s in packaged_skills():
        got = (skills_dir / s.name / "SKILL.md").read_text()
        assert got == (s / "SKILL.md").read_text(encoding="utf-8")


def test_init_skills_idempotent_and_preserves_edits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    skill_md = Paths(tmp_path).skills_dir / packaged_skills()[0].name / "SKILL.md"
    skill_md.write_text("EDITED SKILL\n")
    cli.main(["init"])  # second init must not clobber an edited skill
    assert skill_md.read_text() == "EDITED SKILL\n"


def test_packaged_skills_have_valid_frontmatter():
    skills = packaged_skills()
    assert skills, "no skills are packaged under templates/skills/"
    for s in skills:
        text = (s / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{s.name} missing frontmatter"
        _, frontmatter, _ = text.split("---\n", 2)
        meta = yaml.safe_load(frontmatter)
        assert meta["name"] == s.name, f"{s.name} name/dir mismatch: {meta.get('name')}"
        assert s.name.startswith("autobuild-"), f"{s.name} not namespaced 'autobuild-'"
        assert meta.get("description"), f"{s.name} missing description"


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


def test_run_refused_with_nonzero_exit_when_run_lock_held(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    paths = Paths(tmp_path)
    with loop_mod.run_lock(paths.run_lock):  # simulate another active run
        rc = cli.main(["run"])
    assert rc == 1
    assert "run.lock" in capsys.readouterr().err


def test_invalid_config_exits_2_without_spawning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    paths = Paths(tmp_path)
    paths.config_file.write_text("integration: prr\nmax_parallel: 0\n", encoding="utf-8")

    assert cli.main(["run"]) == 2

    err = capsys.readouterr().err
    assert str(paths.config_file) in err
    assert "integration" in err and "max_parallel" in err
    # the loop never ran: no session directories were created
    assert not any(paths.sessions_dir.iterdir())
