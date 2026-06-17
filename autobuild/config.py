"""Config loading and validation. Ports cfg/cfg_list from common.sh, but parses
YAML with PyYAML instead of grep/sed. Flat schema, all keys optional with the bash
defaults. Unlike the bash version, values are validated at load time: every problem
is aggregated into a single ConfigError so a typo (integration: prr, max_parallel: 0)
fails fast with an actionable message instead of surfacing later inside the loop."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_INTEGRATIONS = ("pr", "auto-merge", "branch")
VALID_PERMISSION_MODES = ("plan", "default", "acceptEdits", "bypassPermissions")


@dataclass(frozen=True)
class Config:
    model: str = "claude-opus-4-8"
    max_parallel: int = 3
    base_branch: str = "main"
    max_iterations: int = 50
    integration: str = "pr"  # pr | auto-merge | branch
    checks: list[str] = field(default_factory=list)
    verify_checks: bool = True  # re-run checks in the reaper before integrating
    claude_cmd: str = "claude"
    # --- session permission posture (task-102) -------------------------------
    permission_mode: str = "acceptEdits"  # plan | default | acceptEdits | bypassPermissions
    allowed_tools: list[str] = field(default_factory=lambda: ["Edit", "Write", "Read"])
    session_max_turns: int = 40  # --max-turns; int >= 1
    dangerously_bypass_permissions: bool = False  # => --dangerously-skip-permissions ...
    require_sandbox_for_bypass: bool = True  # ... only if AUTOBUILD_SANDBOX=1, else refuse


# Top-level keys autobuild understands. Anything else is a likely typo and warned.
KNOWN_KEYS = frozenset(Config.__dataclass_fields__)


class ConfigError(ValueError):
    """Raised when config.yml fails validation. Aggregates *all* problems found in
    one pass so the user can fix everything at once rather than one error per run."""

    def __init__(self, problems: list[str], path: Path | None = None):
        self.problems = list(problems)
        self.path = path
        where = f"invalid config: {path}" if path is not None else "invalid config"
        super().__init__(where + "\n" + "\n".join(f"  - {p}" for p in self.problems))


def _warn(msg: str) -> None:
    # Printed to stderr (not added to the fatal problem list). Defined locally rather
    # than imported from loop.py, which already imports this module.
    print(f"\033[1;33m[warn]\033[0m {msg}", file=sys.stderr)


def load_config(path: Path) -> Config:
    """Read config.yml into a Config, applying defaults for missing keys and
    validating every present key. Raises ConfigError (aggregating all problems) on
    invalid input; warns on unknown top-level keys without aborting."""
    if not path.exists():
        return Config()

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return Config()
    if not isinstance(loaded, dict):
        raise ConfigError(
            [f"top-level config must be a mapping of key: value "
             f"(got {type(loaded).__name__})"],
            path,
        )
    data = loaded

    for key in sorted(k for k in data if k not in KNOWN_KEYS):
        _warn(f"unknown config key '{key}' in {path} (ignored)")

    defaults = Config()
    problems: list[str] = []

    def want_int(key: str, default: int) -> int:
        if key not in data:
            return default
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, int):
            problems.append(f"{key} must be an integer >= 1 (got {v!r})")
            return default
        if v < 1:
            problems.append(f"{key} must be >= 1 (got {v})")
            return default
        return v

    def want_str(key: str, default: str) -> str:
        if key not in data:
            return default
        v = data[key]
        if not isinstance(v, str) or not v.strip():
            problems.append(f"{key} must be a non-empty string (got {v!r})")
            return default
        return v

    def want_bool(key: str, default: bool) -> bool:
        if key not in data:
            return default
        v = data[key]
        if not isinstance(v, bool):
            problems.append(f"{key} must be a boolean true/false (got {v!r})")
            return default
        return v

    def want_enum(key: str, default: str, valid: tuple[str, ...]) -> str:
        if key not in data:
            return default
        v = data[key]
        if not isinstance(v, str) or v not in valid:
            problems.append(f"{key} must be one of {', '.join(valid)} (got {v!r})")
            return default
        return v

    def want_str_list(key: str, default: list[str]) -> list[str]:
        if data.get(key) is None:
            return list(default)
        v = data[key]
        if isinstance(v, str):
            problems.append(
                f"{key} must be a YAML list of strings, not a single string. "
                f"Write it as:\n      {key}:\n        - {v!r}"
            )
            return list(default)
        if not isinstance(v, list):
            problems.append(f"{key} must be a list of non-empty strings (got {type(v).__name__})")
            return list(default)
        cleaned: list[str] = []
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip():
                problems.append(f"{key}[{i}] must be a non-empty string (got {item!r})")
            else:
                cleaned.append(item)
        return cleaned

    model = want_str("model", defaults.model)
    base_branch = want_str("base_branch", defaults.base_branch)
    claude_cmd = want_str("claude_cmd", defaults.claude_cmd)
    max_parallel = want_int("max_parallel", defaults.max_parallel)
    max_iterations = want_int("max_iterations", defaults.max_iterations)

    integration = defaults.integration
    if "integration" in data:
        v = data["integration"]
        if not isinstance(v, str) or v not in VALID_INTEGRATIONS:
            problems.append(
                f"integration must be one of {', '.join(VALID_INTEGRATIONS)} "
                f"(got {v!r})"
            )
        else:
            integration = v

    checks = list(defaults.checks)
    if data.get("checks") is not None:
        v = data["checks"]
        if isinstance(v, str):
            problems.append(
                "checks must be a YAML list of commands, not a single string. "
                f"Write it as:\n      checks:\n        - {v!r}"
            )
        elif not isinstance(v, list):
            problems.append(
                f"checks must be a list of non-empty strings (got {type(v).__name__})"
            )
        else:
            cleaned: list[str] = []
            for i, c in enumerate(v):
                if not isinstance(c, str) or not c.strip():
                    problems.append(f"checks[{i}] must be a non-empty string (got {c!r})")
                else:
                    cleaned.append(c)
            checks = cleaned

    permission_mode = want_enum("permission_mode", defaults.permission_mode,
                                VALID_PERMISSION_MODES)
    allowed_tools = want_str_list("allowed_tools", defaults.allowed_tools)
    session_max_turns = want_int("session_max_turns", defaults.session_max_turns)
    dangerously_bypass_permissions = want_bool(
        "dangerously_bypass_permissions", defaults.dangerously_bypass_permissions)
    require_sandbox_for_bypass = want_bool(
        "require_sandbox_for_bypass", defaults.require_sandbox_for_bypass)

    if problems:
        raise ConfigError(problems, path)

    return Config(
        model=model,
        max_parallel=max_parallel,
        base_branch=base_branch,
        max_iterations=max_iterations,
        integration=integration,
        checks=checks,
        verify_checks=bool(data.get("verify_checks", defaults.verify_checks)),
        claude_cmd=claude_cmd,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        session_max_turns=session_max_turns,
        dangerously_bypass_permissions=dangerously_bypass_permissions,
        require_sandbox_for_bypass=require_sandbox_for_bypass,
    )
