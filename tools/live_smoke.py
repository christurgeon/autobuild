#!/usr/bin/env python3
"""Opt-in, ad-hoc smoke test against a REAL `claude -p` session (issue #43).

Every test under tests/ drives the loop with the STUB `claude` (tests/fixtures/claude) —
token-free and deterministic, but hand-coded to match what the harness expects, so it can
never catch drift between `autobuild/templates/CLAUDE.md` (the session contract) and how a
live agent actually behaves. This script closes that gap: it scaffolds a throwaway project
with ONE trivial task, drives the real outer loop end-to-end against the real CLI, and
asserts the whole contract held — the task reached `done`, the deliverable landed on the
base branch, `result.json` is COMPLETE, and the real stream-json `session.out` parses
through `progress.read_progress` (the #40 path) with a captured cost (the #41 path).

This is DELIBERATELY NOT a pytest test and lives OUTSIDE tests/ (pyproject `testpaths =
["tests"]`), so `uv run pytest` / CI never collect it. It spends real tokens and needs a
working `claude` login + network. Run it by hand when you touch the contract, the spawn, or
the reaper:

    uv run python tools/live_smoke.py

Environment knobs (all optional):
    AUTOBUILD_SMOKE_MODEL   override the model (default: the template default, opus-4-8)
    AUTOBUILD_SMOKE_DIR     build the throwaway project here (default: a fresh mkdtemp)
    AUTOBUILD_SMOKE_KEEP    "1" -> keep the project dir even on success (default: remove on
                            success, always keep on failure for inspection)

Exit code 0 = every assertion passed; 1 = a failure (the project dir is left for debugging);
2 = a setup/precondition error (claude not found, not logged in, etc.).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Import the package the same way the tests do — the script runs from the repo root via
# `uv run`, so autobuild is importable.
from autobuild import cli
from autobuild import loop as loop_mod
from autobuild.config import load_config
from autobuild.paths import Paths
from autobuild.progress import read_progress
from autobuild.tasks import read_task

DELIVERABLE = "hello.txt"
EXPECTED_TEXT = "hello from autobuild"
TASK_ID = "task-001"


def _c(code: str) -> str:
    return f"\033[{code}m"


def info(msg: str) -> None:
    print(f"{_c('1;34')}[smoke]{_c('0')} {msg}")


def good(msg: str) -> None:
    print(f"{_c('1;32')}[ ok ]{_c('0')} {msg}")


def bad(msg: str) -> None:
    print(f"{_c('1;31')}[fail]{_c('0')} {msg}")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _scaffold(project: Path) -> Paths:
    """Build a real one-task autobuild project in `project`: git init, `autobuild init`,
    then tailor the config + write a single trivial task and commit the scaffold (the loop
    refuses a dirty base tree)."""
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-q", "-b", "main")
    # Local identity so commits (and the harness's git-identity preflight) work regardless
    # of the developer's global gitconfig. Worktrees share this repo config, so the spawned
    # agent's commits in its worktree inherit it too.
    _git(project, "config", "user.name", "autobuild-smoke")
    _git(project, "config", "user.email", "smoke@autobuild.invalid")

    # `autobuild init` resolves paths from cwd, so run it there.
    cwd = Path.cwd()
    os.chdir(project)
    try:
        cli.main(["init"])
    finally:
        os.chdir(cwd)

    paths = Paths(project)

    # Tailor the scaffolded config: auto-merge so the deliverable lands on `main` (a local
    # merge — no `gh`/remote needed), a real check that proves the agent did the work, and
    # tight turn/time bounds so a fumbling run can't burn tokens unbounded.
    cfg = paths.config_file.read_text()
    cfg = cfg.replace("integration: pr", "integration: auto-merge")
    cfg = cfg.replace(
        "checks:\n  - \"echo 'replace me with real checks, e.g. npm test'\"",
        f"checks:\n  - \"grep -q '{EXPECTED_TEXT}' {DELIVERABLE}\"")
    cfg = cfg.replace("session_max_turns: 80", "session_max_turns: 25")
    cfg = cfg.replace("task_timeout_seconds: 3600", "task_timeout_seconds: 300")
    cfg = cfg.replace("max_parallel: 3", "max_parallel: 1")
    model = os.environ.get("AUTOBUILD_SMOKE_MODEL")
    if model:
        cfg = cfg.replace("model: claude-opus-4-8", f"model: {model}")
    paths.config_file.write_text(cfg)

    # Replace the example task with our one trivial, unambiguous task.
    for f in paths.tasks_dir.glob("*.md"):
        f.unlink()
    (paths.tasks_dir / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\ntitle: create the {DELIVERABLE} greeting file\n"
        f"status: todo\npriority: 1\ndepends_on: []\n---\n\n## Goal\n"
        f"Create a file named `{DELIVERABLE}` in the repository root whose contents are "
        f"exactly the single line:\n\n    {EXPECTED_TEXT}\n\n"
        f"That is the entire task. Do not modify any other file.\n",
        encoding="utf-8")

    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "scaffold smoke project")
    return paths


def _await_result_event(out_file: Path, timeout: float = 90.0) -> bool:
    """Poll `out_file` until its stream-json `result` event has landed (or `timeout`).

    Real `claude` flushes its terminal `result` event (which carries total_cost_usd) a
    moment AFTER the agent writes result.json — and the harness reaps on result.json, so by
    the time `loop.run` returns the cost line may not be in session.out yet. The session
    process keeps running to completion (a clean drain doesn't kill it), so a bounded poll
    reliably catches the flush. This is the live counterpart to what the stub fakes
    synchronously, and it's exactly the behavior the stub-based tests can't model."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_progress(out_file).finished:
            return True
        time.sleep(1.0)
    return read_progress(out_file).finished


def _assertions(paths: Paths) -> list[tuple[bool, str]]:
    """Check the real run satisfied the whole contract end-to-end. Returns (passed, label)
    for each check so the caller can report all of them, not just the first failure."""
    results: list[tuple[bool, str]] = []

    task = read_task(paths.tasks_dir / f"{TASK_ID}.md")
    results.append((task.status == "done",
                    f"task {TASK_ID} reached `done` (got `{task.status}`)"))

    # auto-merge lands the deliverable on `main`, so it is readable in the project root.
    deliverable = paths.root / DELIVERABLE
    exists = deliverable.is_file()
    results.append((exists, f"{DELIVERABLE} exists on the base branch"))
    if exists:
        body = deliverable.read_text(encoding="utf-8", errors="replace")
        results.append((EXPECTED_TEXT in body,
                        f"{DELIVERABLE} contains '{EXPECTED_TEXT}'"))

    # Exactly one session ran; inspect its sentinel + real stream-json output.
    sdirs = sorted(d for d in paths.sessions_dir.iterdir() if d.is_dir()) \
        if paths.sessions_dir.is_dir() else []
    results.append((len(sdirs) >= 1, "a session directory was created"))
    if sdirs:
        sdir = sdirs[-1]
        from autobuild.loop import _read_json  # reuse the harness's tolerant JSON reader
        sentinel = _read_json(sdir / "result.json") or {}
        results.append((sentinel.get("status") == "COMPLETE",
                        f"result.json status is COMPLETE (got {sentinel.get('status')!r})"))

        # The #40/#41 payoff: real stream-json output parses, and a real cost was captured.
        # Wait for the result event first — it flushes just after result.json (see
        # _await_result_event), so reading immediately after the run would race it.
        out = sdir / "session.out"
        results.append((out.is_file() and out.stat().st_size > 0,
                        "session.out streamed (non-empty)"))
        _await_result_event(out)
        prog = read_progress(out)
        results.append((prog.messages >= 1,
                        f"session.out parsed >=1 assistant message (got {prog.messages})"))
        results.append((prog.finished, "session.out has a terminal result event"))
        results.append((prog.cost_usd is not None,
                        f"a real cost was captured (got {prog.cost_usd})"))

    # A clean end-to-end run drains the backlog.
    summary = None
    if paths.run_summary.is_file():
        from autobuild.loop import _read_json
        summary = _read_json(paths.run_summary)
    results.append((bool(summary) and summary.get("reason") == "drained",
                    f"run ended `drained` (got {summary.get('reason') if summary else None})"))
    return results


def main() -> int:
    if shutil.which("claude") is None:
        bad("`claude` not found on PATH — log in to the CLI first.")
        return 2

    # A developer may invoke this from inside an autobuild-spawned session; the loop refuses
    # to nest (AUTOBUILD_IN_SESSION=1). This standalone runner is the deliberate top level,
    # so scrub the marker (mirrors tests/conftest.py's hermetic_env) before driving `run`.
    os.environ.pop("AUTOBUILD_IN_SESSION", None)

    keep = os.environ.get("AUTOBUILD_SMOKE_KEEP") == "1"
    base = os.environ.get("AUTOBUILD_SMOKE_DIR")
    project = Path(base) if base else Path(tempfile.mkdtemp(prefix="autobuild-smoke-"))

    info("Real-`claude` smoke test — this spends tokens and needs a working login.")
    info(f"project: {project}")
    info(f"model:   {os.environ.get('AUTOBUILD_SMOKE_MODEL', '(template default)')}")

    failed = False
    try:
        paths = _scaffold(project)
        config = load_config(paths.config_file)
        info("driving the real loop (one task) — this may take a minute...")
        loop_mod.run(paths, config)

        print()
        info("contract assertions:")
        all_pass = True
        for passed, label in _assertions(paths):
            (good if passed else bad)(label)
            all_pass = all_pass and passed
        failed = not all_pass
    except Exception as exc:  # noqa: BLE001 — a smoke runner reports, never tracebacks raw
        bad(f"run raised {type(exc).__name__}: {exc}")
        failed = True

    print()
    if failed:
        bad(f"SMOKE TEST FAILED — project kept for inspection: {project}")
        return 1
    good("SMOKE TEST PASSED — the real session honored the contract.")
    if keep:
        info(f"project kept (AUTOBUILD_SMOKE_KEEP=1): {project}")
    elif not base:
        shutil.rmtree(project, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
