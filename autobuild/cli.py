"""CLI entry point. Ports bin/autobuild: init, run, status, reap, clean."""

from __future__ import annotations

import argparse
import importlib.resources as ir
import sys

from . import loop as loop_mod
from .config import load_config
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

    ok("ready. Edit GOAL.md and tasks/, then run: autobuild run")
    return 0


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
    config = load_config(paths.config_file)

    if args.command == "run":
        try:
            loop_mod.run(paths, config)
        except loop_mod.RunLockHeld as e:
            _err(f"another 'autobuild run' is active (holds {e}); refusing to start a "
                 f"second run. The lock releases automatically when that run exits.")
            return 1
    elif args.command == "status":
        loop_mod.status(paths, config)
    elif args.command == "reap":
        loop_mod.reap(paths, config)
    elif args.command == "clean":
        loop_mod.clean(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
