"""Config loading. Ports cfg/cfg_list from common.sh, but parses YAML with PyYAML
instead of grep/sed. Flat schema, all keys optional with the bash defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    model: str = "claude-opus-4-8"
    max_parallel: int = 3
    base_branch: str = "main"
    max_iterations: int = 50
    integration: str = "pr"  # pr | auto-merge | branch
    checks: list[str] = field(default_factory=list)
    claude_cmd: str = "claude"


def load_config(path: Path) -> Config:
    """Read config.yml into a Config, applying defaults for missing keys."""
    data: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded

    defaults = Config()
    return Config(
        model=str(data.get("model", defaults.model)),
        max_parallel=int(data.get("max_parallel", defaults.max_parallel)),
        base_branch=str(data.get("base_branch", defaults.base_branch)),
        max_iterations=int(data.get("max_iterations", defaults.max_iterations)),
        integration=str(data.get("integration", defaults.integration)),
        checks=[str(c) for c in (data.get("checks") or [])],
        claude_cmd=str(data.get("claude_cmd", defaults.claude_cmd)),
    )
