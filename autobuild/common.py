"""Shared low-level helpers for the loop modules: console logging, timestamps, the
operator-notification choke point, the git/gh subprocess seams, and sentinel parsing."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .session import parse_leading_json

# --- logging -----------------------------------------------------------------

def _c(code: str) -> str:
    return f"\033[{code}m"


def log(msg: str) -> None:
    print(f"{_c('1;34')}[autobuild]{_c('0')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('1;32')}[ ok ]{_c('0')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33')}[warn]{_c('0')} {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- operator notifications --------------------------------------------------

_NOTIFY_TIMEOUT_SECONDS = 30  # bound a wedged notifier so it can never hang the run


def _notify(config: Config, event: str, message: str) -> None:
    """Best-effort operator notification — the single choke point for every event.

    When `config.notify_command` is non-empty, run it via the shell with the event type
    and message exposed as the environment variables `AUTOBUILD_EVENT` and
    `AUTOBUILD_MESSAGE` (the operator wires those to Telegram / push / email / whatever).
    Bounded by a timeout so a wedged notifier can't hang the run, and ALL failures
    (non-zero exit, timeout, OSError) are swallowed with a `warn(...)` — a notification
    problem must NEVER break or halt a run.

    The command is operator-controlled (≈ a shell), so this is not a security boundary —
    same posture as the permission allowlist (see the README security note)."""
    cmd = (config.notify_command or "").strip()
    if not cmd:
        return  # notifications disabled — no subprocess, no env churn
    env = {**os.environ, "AUTOBUILD_EVENT": event, "AUTOBUILD_MESSAGE": message}
    try:
        r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                           timeout=_NOTIFY_TIMEOUT_SECONDS)
        if r.returncode != 0:
            warn(f"notify_command exited {r.returncode} for event '{event}' (ignored)")
    except subprocess.TimeoutExpired:
        warn(f"notify_command timed out after {_NOTIFY_TIMEOUT_SECONDS}s for "
             f"event '{event}' (ignored)")
    except OSError as exc:
        warn(f"notify_command failed for event '{event}': {exc} (ignored)")


def _read_json(path: Path) -> dict | None:
    """Read `path` and parse it as a JSON object via parse_leading_json (tolerating
    stray trailing data after a valid leading object). Returns None if the file is
    unreadable, torn/partial, or parses to a non-object — returning a non-dict would
    crash callers that do `.get()` (reap_session, collect_status), wedging
    run/reap/status on a single poisoned file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_leading_json(text)


def _classify_sentinel(sdir: Path) -> str:
    """Classify a session's result.json:
      'reapable' — present and parses to a usable JSON object (a dict);
      'corrupt'  — present but unparseable (leading garbage / empty) or not an object
                   (`[]`, `"x"`) — shapes raw_decode cannot salvage;
      'absent'   — no result.json yet.

    Centralizing this is what lets every gate (`_harvest`, `reconcile`) treat a
    present-but-unparseable sentinel as a finished-but-failed session and BLOCK it,
    instead of skipping it on mere file existence and stranding the task forever."""
    result_file = sdir / "result.json"
    if not result_file.exists():
        return "absent"
    return "reapable" if isinstance(_read_json(result_file), dict) else "corrupt"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def current_branch(root: Path) -> str | None:
    """The branch checked out in `root`, or None on a detached HEAD or a git error."""
    r = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _gh(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `gh` in `root`. Mirrors `_git` so the gh seam is monkeypatchable in tests
    and the retry helpers have a single place to invoke gh."""
    return subprocess.run(["gh", *args], cwd=str(root), capture_output=True, text=True)
