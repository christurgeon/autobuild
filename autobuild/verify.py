"""The verification gate: re-run config.checks against a session's worktree before
integrating, and persist a checks log on failure."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config
from .paths import Paths

def _write_checks_log(sdir: Path, cmd: str, code: int, stdout: str, stderr: str,
                      name: str = "checks.log") -> None:
    """Persist why verification failed: the command, its exit code, and the tail of
    its combined output (last 50 lines) so a human can inspect without re-running.
    `name` selects the log file — the pre-merge gate writes `checks.log`, the post-merge
    combined-tree gate writes `post-merge-checks.log`."""
    tail = "\n".join(((stdout or "") + (stderr or "")).splitlines()[-50:])
    (sdir / name).write_text(
        f"command: {cmd}\nexit: {code}\n\n--- output (tail) ---\n{tail}\n",
        encoding="utf-8",
    )


def verify_checks(config: Config, paths: Paths, sdir: Path, sid: str) -> tuple[bool, str]:
    """Re-run config.checks against the session's worktree before integrating —
    the harness's own verification of a COMPLETE, not the agent's self-report.

    Returns (passed, outcome) where outcome is one of "skipped" (verification
    disabled or no checks), "passed", or "failed: <cmd>". Stops at the first failing
    command and writes <sdir>/checks.log. A missing worktree is treated as a failure:
    we cannot verify the tree, so we refuse to trust it."""
    if not config.verify_checks or not config.checks:
        return True, "skipped"

    worktree = paths.worktrees_dir / sid
    if not worktree.is_dir():
        _write_checks_log(sdir, "(pre-flight)", -1, "",
                          f"worktree {worktree} is missing; cannot verify checks")
        return False, "failed: worktree missing"

    for cmd in config.checks:
        r = subprocess.run(cmd, shell=True, cwd=str(worktree),
                           capture_output=True, text=True)
        if r.returncode != 0:
            _write_checks_log(sdir, cmd, r.returncode, r.stdout, r.stderr)
            return False, f"failed: {cmd}"
    return True, "passed"
