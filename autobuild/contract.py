"""The session contract: which text a spawned session obeys, and how it is staged.

The harness owns the contract. It resolves, in order: the operator override
`.autobuild/CONTRACT.md`, a legacy root `CLAUDE.md` that is the old contract (back-compat),
then the packaged `templates/CONTRACT.md`. The project's own root CLAUDE.md is never the
contract unless it is the old one; it still loads from the worktree as project conventions.
Leaf module: it imports no loop module, so preflight can use it without a cycle."""

from __future__ import annotations

import importlib.resources as ir
import shutil
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from .paths import Paths
from .tasks import Task

# Phrases from the old template's heading (line 1) and opening line (line 3).
_LEGACY_MARKERS = ("autobuild session contract", "You are a single **autobuild** session")
# Only the head of the file counts, so a project CLAUDE.md that quotes the contract further
# down is not taken as the contract.
_LEGACY_WINDOW = 10


def is_legacy_contract(text: str) -> bool:
    head = "\n".join(text.splitlines()[:_LEGACY_WINDOW])
    return any(m in head for m in _LEGACY_MARKERS)


def _packaged_contract() -> str:
    # Must always succeed; a failure here is a broken install and is raised.
    return (ir.files("autobuild") / "templates" / "CONTRACT.md").read_text(encoding="utf-8")


def resolve_contract(paths: Paths) -> tuple[str, str]:
    """Return `(text, source)`, source in {"override", "legacy", "packaged"}. An unreadable
    override or root file falls through to the next candidate."""
    with suppress(OSError):
        if paths.contract_override.is_file():
            return paths.contract_override.read_text(encoding="utf-8"), "override"
    with suppress(OSError):
        if paths.claude_md.is_file():
            text = paths.claude_md.read_text(encoding="utf-8")
            if is_legacy_contract(text):
                return text, "legacy"
    return _packaged_contract(), "packaged"


def checks_block(checks: Sequence[str]) -> str:
    """The prompt's Checks block. `.autobuild/` is gitignored, so the prompt is the only
    place a session can learn the commands the reaper will re-run."""
    cmds = [c.strip() for c in checks if c.strip()]
    if not cmds:
        return "No checks are configured; verify your change by the task's acceptance criteria.\n"
    lines = "".join(f"  $ {c}\n" for c in cmds)
    return ("Checks (the harness re-runs these verbatim in your worktree before integrating; "
            "run each from the worktree root, all must pass before you write COMPLETE):\n"
            + lines)


def stage_contract(sdir: Path, paths: Paths, task: Task) -> Path:
    """Write the resolved contract (CONTRACT.md) and copy the north star (GOAL.md) and the
    task file into the session dir, so the prompt points the agent there instead of at the
    main checkout. Returns the staged task-file path. A missing GOAL.md is just not staged.
    Everything lands in `sdir` (already `--add-dir`'d and OUTSIDE the worktree tree), so it
    is never swept into the agent's feature commit."""
    text, _source = resolve_contract(paths)
    (sdir / "CONTRACT.md").write_text(text, encoding="utf-8")
    with suppress(OSError):
        if paths.goal_file.is_file():
            shutil.copyfile(paths.goal_file, sdir / "GOAL.md")
    staged_task = sdir / task.path.name
    shutil.copyfile(task.path, staged_task)
    return staged_task
