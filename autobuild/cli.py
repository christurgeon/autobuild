"""CLI entry point: the `autobuild` command (init, run, status, reap, clean)."""

from __future__ import annotations

import argparse
import importlib.resources as ir
import sys

from . import loop as loop_mod
from . import preflight as preflight_mod
from .config import ConfigError, load_config
from .loop import log, ok
from .paths import Paths


def _err(msg: str) -> None:
    print(f"\033[1;31m[fail]\033[0m {msg}", file=sys.stderr)


def ab_init(paths: Paths) -> int:
    log(f"initializing autobuild in {paths.root}")
    tpl = ir.files("autobuild") / "templates"

    if not paths.goal_file.exists():
        paths.goal_file.write_text((tpl / "GOAL.md").read_text(encoding="utf-8"), encoding="utf-8")
    if not paths.claude_md.exists():
        paths.claude_md.write_text((tpl / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")

    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    if not any(paths.tasks_dir.glob("*.md")):
        for f in (tpl / "tasks").iterdir():
            if f.name.endswith(".md"):
                (paths.tasks_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    paths.ab_dir.mkdir(parents=True, exist_ok=True)
    paths.ensure_runtime_dirs()
    if not paths.config_file.exists():
        paths.config_file.write_text((tpl / "config.yml").read_text(encoding="utf-8"), encoding="utf-8")

    _install_skills(tpl / "skills", paths.skills_dir)
    _ensure_gitignore(paths)

    ok("ready. Edit GOAL.md and tasks/, commit them (autobuild run refuses a dirty "
       "base tree), then: autobuild run")
    return 0


def _ensure_gitignore(paths: Paths) -> None:
    """Make sure `.autobuild/` is gitignored: it is disposable harness state (sessions,
    worktrees, locks) that must never be committed, and ignoring it also keeps a session's
    `git add -A` from sweeping in worktree noise. Idempotent: append the entry only if no
    line already names `.autobuild`, creating .gitignore if absent — never clobbering it."""
    gi = paths.root / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    except OSError:
        return

    def _names_autobuild(line: str) -> bool:
        # match `.autobuild`, `.autobuild/`, `/.autobuild`, `.autobuild/**`, etc.
        s = line.strip().lstrip("/").rstrip("/")
        return s == ".autobuild" or s.removesuffix("/**").rstrip("/") == ".autobuild"

    if any(_names_autobuild(line) for line in lines):
        return
    block = ("" if not lines or lines[-1].strip() == "" else "\n") + \
            "# autobuild: disposable harness state (rebuilt from tasks/ + git)\n.autobuild/\n"
    with open(gi, "a", encoding="utf-8") as fh:
        fh.write(block)


def _install_skills(src, skills_dir) -> None:
    """Copy the packaged authoring/operating skills into the project's .claude/skills/.

    Each skill dir is copied only if it does not already exist, so re-running init is a
    no-op and a user's edited/installed skill is never clobbered (mirrors how GOAL.md and
    CLAUDE.md are guarded)."""
    if not src.is_dir():
        return
    for skill in src.iterdir():
        if not skill.is_dir():
            continue
        dest = skills_dir / skill.name
        if dest.exists():
            continue
        _copy_resource_tree(skill, dest)


def _copy_resource_tree(src, dest) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            _copy_resource_tree(child, target)
        else:
            target.write_text(child.read_text(encoding="utf-8"), encoding="utf-8")


def require_init(paths: Paths) -> None:
    if not paths.config_file.exists():
        _err("no .autobuild/config.yml — run 'autobuild init' first")
        raise SystemExit(1)
    if not paths.tasks_dir.is_dir():
        _err("no tasks/ directory — run 'autobuild init' first")
        raise SystemExit(1)
    paths.ensure_runtime_dirs()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autobuild",
        description="Drain a backlog toward a GOAL with parallel Claude sessions. "
                    "State lives under .autobuild/ and is rebuilt from tasks/ + git.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="copy GOAL.md, CLAUDE.md, tasks/, .autobuild/config.yml into this project")
    sub.add_parser("doctor", help="preflight: check the environment before a run spends tokens")
    sub.add_parser("run", help="schedule -> spawn sessions in worktrees -> reap, until drained")
    sub.add_parser("status", help="print every task's status and any in-flight sessions")
    sub.add_parser("reap", help="one-shot: collect finished sessions, update tasks, integrate")
    sub.add_parser("clean", help="remove finished worktrees and reaped session dirs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = Paths.from_cwd()

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        return ab_init(paths)

    require_init(paths)
    try:
        config = load_config(paths.config_file)
    except ConfigError as e:
        _err(f"invalid configuration in {e.path}:")
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return preflight_mod.doctor(paths, config)
    if args.command == "run":
        try:
            loop_mod.run(paths, config)
        except loop_mod.RunLockHeld as e:
            _err(f"another 'autobuild run' is active (holds {e}); refusing to start a "
                 f"second run. The lock releases automatically when that run exits.")
            return 1
        except loop_mod.DirtyBaseTree as e:
            _err(str(e))
            return 2
        except preflight_mod.PreflightError as e:
            _err(str(e))
            return 2
        except loop_mod.BaseBranchLeak as e:
            _err(str(e))
            return 2
    elif args.command == "status":
        loop_mod.status(paths, config)
    elif args.command == "reap":
        try:
            loop_mod.reap(paths, config)
        except loop_mod.BaseBranchLeak as e:
            _err(str(e))
            return 2
    elif args.command == "clean":
        loop_mod.clean(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
