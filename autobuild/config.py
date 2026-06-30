"""Config loading and validation. Parses .autobuild/config.yml with PyYAML into a
typed, frozen Config. Flat schema, all keys optional with sensible defaults. Values
are validated at load time: every problem is aggregated into a single ConfigError so
a typo (integration: prr, max_parallel: 0) fails fast with an actionable message
instead of surfacing later inside the loop."""

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
    max_iterations: int = 100
    run_budget_seconds: int = 0  # int >= 0; whole-run wall-clock ceiling (0 = unlimited).
                                 # Once spent the loop stops claiming new work, drains what's
                                 # in flight, and reports the cap. Monotonic + in-memory, so a
                                 # killed/resumed run does not resume the clock.
    integration: str = "pr"  # pr | auto-merge | branch
    integration_max_retries: int = 2  # int >= 0; extra attempts for transient remote ops
                                      # (git push / gh pr create) during integration
    checks: list[str] = field(default_factory=list)
    verify_checks: bool = True  # re-run checks in the reaper before integrating
    verify_after_merge: bool = True  # re-run checks on the COMBINED base tree after an
                                     # auto-merge; revert + block if they fail
    claude_cmd: str = "claude"
    # --- session permission posture ------------------------------------------
    # Default is maximally permissive: full bypass, no sandbox gate, so a session can do
    # whatever it needs unattended. The trade-off (the agent inherits this machine's git
    # credentials + network) is the operator's to accept — see the README security note.
    permission_mode: str = "acceptEdits"  # the fallback posture when bypass is turned off
    allowed_tools: list[str] = field(default_factory=lambda: ["Edit", "Write", "Read"])
    session_max_turns: int = 80  # --max-turns; int >= 1
    dangerously_bypass_permissions: bool = True  # => --dangerously-skip-permissions ...
    require_sandbox_for_bypass: bool = False  # ... and do NOT require AUTOBUILD_SANDBOX
    # --- per-session timeout plumbing ----------------------------------------
    task_timeout_seconds: int = 3600  # int >= 1; monotonic per-session deadline
    kill_grace_seconds: int = 20      # int >= 1; SIGTERM -> wait -> SIGKILL
    timeout_max_retries: int = 2      # int >= 0; auto-retries for a timed-out task
                                      # (0 = block on first timeout). Each retry
                                      # re-spends task_timeout_seconds.


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

    def want_int(key: str, default: int, *, minimum: int = 1) -> int:
        if key not in data:
            return default
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, int):
            problems.append(f"{key} must be an integer >= {minimum} (got {v!r})")
            return default
        if v < minimum:
            problems.append(f"{key} must be >= {minimum} (got {v})")
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
    run_budget_seconds = want_int("run_budget_seconds", defaults.run_budget_seconds,
                                  minimum=0)

    integration = want_enum("integration", defaults.integration, VALID_INTEGRATIONS)
    integration_max_retries = want_int("integration_max_retries",
                                       defaults.integration_max_retries, minimum=0)
    checks = want_str_list("checks", defaults.checks)
    verify_checks = want_bool("verify_checks", defaults.verify_checks)
    verify_after_merge = want_bool("verify_after_merge", defaults.verify_after_merge)

    permission_mode = want_enum("permission_mode", defaults.permission_mode,
                                VALID_PERMISSION_MODES)
    allowed_tools = want_str_list("allowed_tools", defaults.allowed_tools)
    session_max_turns = want_int("session_max_turns", defaults.session_max_turns)
    dangerously_bypass_permissions = want_bool(
        "dangerously_bypass_permissions", defaults.dangerously_bypass_permissions)
    require_sandbox_for_bypass = want_bool(
        "require_sandbox_for_bypass", defaults.require_sandbox_for_bypass)
    task_timeout_seconds = want_int("task_timeout_seconds", defaults.task_timeout_seconds)
    kill_grace_seconds = want_int("kill_grace_seconds", defaults.kill_grace_seconds)
    timeout_max_retries = want_int("timeout_max_retries", defaults.timeout_max_retries,
                                   minimum=0)

    if problems:
        raise ConfigError(problems, path)

    return Config(
        model=model,
        max_parallel=max_parallel,
        base_branch=base_branch,
        max_iterations=max_iterations,
        run_budget_seconds=run_budget_seconds,
        integration=integration,
        integration_max_retries=integration_max_retries,
        checks=checks,
        verify_checks=verify_checks,
        verify_after_merge=verify_after_merge,
        claude_cmd=claude_cmd,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        session_max_turns=session_max_turns,
        dangerously_bypass_permissions=dangerously_bypass_permissions,
        require_sandbox_for_bypass=require_sandbox_for_bypass,
        task_timeout_seconds=task_timeout_seconds,
        kill_grace_seconds=kill_grace_seconds,
        timeout_max_retries=timeout_max_retries,
    )
