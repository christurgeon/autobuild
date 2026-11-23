"""Tests for `autobuild doctor` (autobuild/preflight.py) and the critical-check
wiring into `run`. All token-free: the only external tool a check touches is the
stub `claude` on PATH (via stub_bin) and local git."""

import subprocess
import types

import pytest

from autobuild import cli
from autobuild import preflight
from autobuild.config import Config
from autobuild.paths import Paths


# --- helpers -----------------------------------------------------------------

def init_committed(git_repo, monkeypatch, **cfg_overrides):
    """`autobuild init` in a throwaway repo, then commit the scaffold so the base tree
    is clean (run refuses a dirty base). Returns Paths. cfg_overrides patch config.yml."""
    monkeypatch.chdir(git_repo)
    cli.main(["init"])
    paths = Paths(git_repo)
    if cfg_overrides:
        lines = paths.config_file.read_text().splitlines()
        for key, val in cfg_overrides.items():
            lines = [ln for ln in lines if not ln.startswith(f"{key}:")]
            lines.append(f"{key}: {val}")
        paths.config_file.write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "scaffold"], check=True)
    return paths


def unset_identity(git, git_repo):
    git(git_repo, "config", "--unset", "user.name")
    git(git_repo, "config", "--unset", "user.email")


# --- doctor: the happy path --------------------------------------------------

def test_doctor_passes_on_wellformed_repo(git_repo, stub_bin, capsys):
    stub_bin()  # claude on PATH
    # branch mode so the gh check doesn't apply; git_repo has identity + a clean `main`.
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch"))
    assert rc == 0
    assert "all checks passed" in capsys.readouterr().out


# --- doctor: FAIL-level checks exit non-zero ---------------------------------

def test_doctor_fails_when_git_identity_unset(git_repo, stub_bin, git, capsys):
    stub_bin()  # claude resolvable, so identity is the only FAIL
    unset_identity(git, git_repo)
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "git identity" in out and "FAILED" in out


def test_doctor_fails_when_claude_not_resolvable(git_repo, capsys):
    # A bogus claude_cmd is deterministic regardless of what's on the host PATH.
    rc = preflight.doctor(Paths(git_repo),
                          Config(integration="branch", claude_cmd="claude-not-a-real-binary"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "claude on PATH" in out and "FAILED" in out


def test_doctor_fails_when_base_branch_missing(git_repo, stub_bin, capsys):
    stub_bin()
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch", base_branch="nope"))
    assert rc == 1
    assert "base branch" in capsys.readouterr().out


# --- doctor: WARN does not fail ----------------------------------------------

def test_doctor_warn_low_disk_does_not_fail(git_repo, stub_bin, monkeypatch, capsys):
    stub_bin()
    monkeypatch.setattr(
        preflight.shutil, "disk_usage",
        lambda _p: types.SimpleNamespace(total=10 * 1024 ** 3, used=10 * 1024 ** 3,
                                         free=100 * 1024 ** 2))  # 100 MiB free
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch"))
    assert rc == 0  # WARN must not fail the run
    out = capsys.readouterr().out
    assert "free disk" in out and "all checks passed" in out


def test_doctor_warn_dirty_base_does_not_fail(git_repo, stub_bin, capsys):
    stub_bin()
    (git_repo / "stray.py").write_text("x = 1\n")  # uncommitted source
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch"))
    assert rc == 0  # run enforces a clean base; doctor only WARNs
    assert "base tree clean" in capsys.readouterr().out


def _tasks_dir(git_repo):
    d = Paths(git_repo).tasks_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_doctor_warns_on_malformed_task_file_but_passes(git_repo, stub_bin, capsys):
    stub_bin()
    d = _tasks_dir(git_repo)
    (d / "task-002.md").write_text("---\nid: task-002\ntitle: [unclosed\nstatus: todo\n---\n")
    (d / "README.md").write_text("# Backlog notes\n")
    rc = preflight.doctor(Paths(git_repo), Config(integration="branch"))
    assert rc == 0  # run tolerates the file, so doctor only WARNs
    out = capsys.readouterr().out
    assert "task-002.md" in out and "README.md" in out


def test_doctor_warns_on_duplicate_task_ids(git_repo, stub_bin):
    stub_bin()
    d = _tasks_dir(git_repo)
    for name in ("task-001-a.md", "task-001-b.md"):
        (d / name).write_text("---\nid: task-001\nstatus: todo\n---\n")
    results = {name: (level, detail) for level, name, detail in
               preflight.run_checks(Paths(git_repo), Config(integration="branch"))}
    level, detail = results["task files"]
    assert level == preflight.WARN and "task-001" in detail


def test_doctor_gh_check_only_applies_in_pr_mode(git_repo, stub_bin):
    stub_bin()
    names = {name for _l, name, _d in preflight.run_checks(Paths(git_repo),
                                                           Config(integration="branch"))}
    assert not any("gh" in n for n in names)
    pr_names = {name for _l, name, _d in preflight.run_checks(Paths(git_repo),
                                                              Config(integration="pr"))}
    assert any("gh" in n for n in pr_names)


# --- the doctor CLI subcommand ----------------------------------------------

def test_doctor_cli_exit_zero_when_healthy(git_repo, stub_bin, monkeypatch):
    init_committed(git_repo, monkeypatch, integration="branch")
    assert cli.main(["doctor"]) == 0


def test_doctor_cli_exit_nonzero_when_identity_unset(git_repo, stub_bin, monkeypatch, git):
    init_committed(git_repo, monkeypatch, integration="branch")
    unset_identity(git, git_repo)
    assert cli.main(["doctor"]) == 1


def test_doctor_cli_requires_init(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor"])  # no .autobuild/config.yml yet
    assert e.value.code == 1


# --- the run() critical-check wiring -----------------------------------------

def test_run_aborts_early_when_identity_unset(git_repo, stub_bin, monkeypatch, git, capsys):
    paths = init_committed(git_repo, monkeypatch, integration="branch")
    unset_identity(git, git_repo)  # critical FAIL after the scaffold commit
    rc = cli.main(["run"])
    assert rc == 2
    assert "preflight failed" in capsys.readouterr().err
    # aborted BEFORE claiming or spawning: no session directories were created.
    assert not any(paths.sessions_dir.iterdir())


def test_run_aborts_early_when_claude_unresolvable(git_repo, monkeypatch, capsys):
    # No stub_bin; claude_cmd is bogus so the check fails on any host.
    paths = init_committed(git_repo, monkeypatch, integration="branch",
                           claude_cmd="claude-not-a-real-binary")
    rc = cli.main(["run"])
    assert rc == 2
    assert "preflight failed" in capsys.readouterr().err
    assert not any(paths.sessions_dir.iterdir())


def test_assert_run_preflight_passes_when_healthy(git_repo, stub_bin):
    stub_bin()
    # Should not raise: claude on PATH + git_repo identity present.
    preflight.assert_run_preflight(Paths(git_repo), Config(integration="branch"))


def test_assert_run_preflight_ignores_warn_level_problems(git_repo, stub_bin):
    stub_bin()
    # A missing base_branch is a FAIL in doctor but NOT one of the critical run checks,
    # so the run-wiring must not abort on it (run handles base resolution itself).
    preflight.assert_run_preflight(Paths(git_repo),
                                   Config(integration="branch", base_branch="nope"))


# --- auto-merge requires base_branch checked out -----------------------------

def test_doctor_fails_when_auto_merge_and_head_not_base(git_repo, stub_bin, monkeypatch,
                                                        git, capsys):
    init_committed(git_repo, monkeypatch, integration="auto-merge")
    git(git_repo, "checkout", "-q", "-b", "other")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "other" in out and "main" in out


def test_doctor_base_checkout_check_absent_outside_auto_merge(git_repo, stub_bin, git):
    git(git_repo, "checkout", "-q", "-b", "other")
    names = {name for _l, name, _d in preflight.run_checks(Paths(git_repo),
                                                           Config(integration="branch"))}
    assert "base checkout" not in names


def test_run_refuses_auto_merge_when_head_not_base(git_repo, stub_bin, monkeypatch, git,
                                                   capsys):
    paths = init_committed(git_repo, monkeypatch, integration="auto-merge")
    git(git_repo, "checkout", "-q", "-b", "other")
    before = {p.name: p.read_text() for p in paths.tasks_dir.glob("*.md")}
    rc = cli.main(["run"])
    assert rc == 2
    assert "preflight failed" in capsys.readouterr().err
    assert not any(paths.sessions_dir.iterdir())
    assert {p.name: p.read_text() for p in paths.tasks_dir.glob("*.md")} == before


# --- contract source ---------------------------------------------------------

def test_doctor_warns_on_legacy_root_contract(tmp_path):
    paths = Paths(tmp_path)
    paths.claude_md.write_text("# CLAUDE.md — the autobuild session contract\n")
    level, _name, detail = preflight._check_contract(paths, Config())
    assert level == preflight.WARN
    assert ".autobuild/CONTRACT.md" in detail


def test_doctor_contract_pass_when_packaged(tmp_path):
    paths = Paths(tmp_path)
    paths.claude_md.write_text("# my project\n")
    level, _name, detail = preflight._check_contract(paths, Config())
    assert level == preflight.PASS
    assert "packaged" in detail


# --- .gitignore coverage -----------------------------------------------------

def test_doctor_warns_when_gitignore_absent(tmp_path):
    level, _name, detail = preflight._check_gitignore(Paths(tmp_path), Config())
    assert level == preflight.WARN
    assert "build output" in detail


def test_doctor_warns_when_gitignore_only_ignores_autobuild(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "# autobuild: disposable harness state (rebuilt from tasks/ + git)\n.autobuild/\n")
    level, _name, _detail = preflight._check_gitignore(Paths(tmp_path), Config())
    assert level == preflight.WARN


def test_doctor_gitignore_pass_with_project_rules(tmp_path):
    (tmp_path / ".gitignore").write_text(".autobuild/\n__pycache__/\n\n# c\n")
    level, _name, detail = preflight._check_gitignore(Paths(tmp_path), Config())
    assert level == preflight.PASS
    assert "1 project rule" in detail
