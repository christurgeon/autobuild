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
