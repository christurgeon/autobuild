"""Shared pytest fixtures: hermetic git env, throwaway repos, the stub `claude`."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

STUB_CLAUDE = Path(__file__).parent / "fixtures" / "claude"


@pytest.fixture(autouse=True)
def hermetic_env(tmp_path, monkeypatch):
    """Isolate git + tooling from the developer's machine: tmp HOME, fixed identity,
    no global gitconfig, no network prompts, no inherited GH token."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ab")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ab@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "ab")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ab@test.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def run_git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


@pytest.fixture
def git():
    """The git helper as a fixture: git(repo, *args, check=True)."""
    return run_git


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway repo on branch `main` with one empty commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "ab")
    run_git(repo, "config", "user.email", "ab@test.invalid")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


@pytest.fixture
def diverging_dep(tmp_path):
    """Set up `main` and autobuild/<dep> diverging on the same line of a shared file so
    that merging the dep into a fresh worktree off main conflicts. Commits on main land
    in the repo's own working tree; the dep's edit is made via a throwaway worktree
    (main can't be checked out twice). Returns the dep tid."""
    def _make(repo, dep_tid="task-001", filename="shared.txt"):
        run_git(repo, "checkout", "-q", "main")
        (repo / filename).write_text("v1\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "seed shared file")
        run_git(repo, "branch", f"autobuild/{dep_tid}")          # dep forks from here
        depwt = tmp_path / f"depwt-{dep_tid}"
        run_git(repo, "worktree", "add", "-q", str(depwt), f"autobuild/{dep_tid}")
        (depwt / filename).write_text("from-dep\n")
        run_git(depwt, "add", "-A")
        run_git(depwt, "commit", "-q", "-m", f"{dep_tid}: edit shared file")
        run_git(repo, "worktree", "remove", "--force", str(depwt))
        (repo / filename).write_text("from-main\n")              # base diverges
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "main: edit shared file")
        return dep_tid
    return _make


@pytest.fixture
def stub_bin(tmp_path, monkeypatch):
    """A tmp bin/ on PATH holding the stub `claude`. Returns a setter for the env
    knobs the stub reads (STUB_STATUS, STUB_FOLLOWUPS, STUB_STALL, ...)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dst = bindir / "claude"
    dst.write_text(STUB_CLAUDE.read_text())
    dst.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def set_mode(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))

    return set_mode
