"""Project paths. All commands run from the target project root; these resolve
relative to it. The one place every .autobuild/ location is defined."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def from_cwd(cls) -> "Paths":
        return cls(Path.cwd())

    @property
    def ab_dir(self) -> Path:
        return self.root / ".autobuild"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def sessions_dir(self) -> Path:
        return self.ab_dir / "sessions"

    @property
    def worktrees_dir(self) -> Path:
        return self.ab_dir / "worktrees"

    @property
    def lock_file(self) -> Path:
        return self.ab_dir / "backlog.lock"

    @property
    def run_lock(self) -> Path:
        return self.ab_dir / "run.lock"

    @property
    def retries_ledger(self) -> Path:
        return self.ab_dir / "retries.json"

    @property
    def config_file(self) -> Path:
        return self.ab_dir / "config.yml"

    @property
    def goal_file(self) -> Path:
        return self.root / "GOAL.md"

    @property
    def claude_md(self) -> Path:
        return self.root / "CLAUDE.md"

    def ensure_runtime_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
