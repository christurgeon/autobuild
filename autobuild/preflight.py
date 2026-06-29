"""Preflight checks: `autobuild doctor` validates the operating environment *before*
a run spends tokens, so a misconfigured host fails fast with one clear report instead
of late, scattered errors (or N wasted sessions).

Each check is cheap and side-effect-free, returning a `(level, name, detail)` triple
where level is PASS / WARN / FAIL. `doctor` prints the report and exits non-zero if any
check FAILs (a WARN never fails). The critical (FAIL-level) subset — `claude` on PATH and
a git commit identity — is also wired into `run()` via `assert_run_preflight`, so a real
run aborts early with the same message before claiming or spawning.

`doctor` only *reports*; `run` *enforces* (it keeps its own hard `_assert_base_clean`
gate). The two never duplicate enforcement — the base-tree-clean check here is a WARN.
"""

from __future__ import annotations

import shutil
import subprocess
from shutil import which

from .config import Config
from .loop import _git, dirty_base_paths, log, ok, warn
from .paths import Paths

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# WARN threshold for free space on the filesystem holding `.autobuild/`: parallel
# worktrees of a large repo can exhaust a small disk.
MIN_FREE_DISK_BYTES = 2 * 1024 ** 3  # ~2 GiB

CheckResult = tuple[str, str, str]  # (level, name, detail)


def _fail_line(msg: str) -> None:
    print(f"\033[1;31m[fail]\033[0m {msg}")


# --- individual checks -------------------------------------------------------
# Each takes (paths, config) and returns a CheckResult, or None when the check
# does not apply to this configuration (filtered out before reporting).

def _check_config(paths: Paths, config: Config) -> CheckResult:
    # Config already loaded cleanly to get here (the CLI would have exited 2 otherwise);
    # report it as PASS for completeness so the report covers the whole environment.
    return (PASS, "config", f"loaded {paths.config_file.name} cleanly")


def _check_claude(paths: Paths, config: Config) -> CheckResult:
    cmd = config.claude_cmd
    resolved = which(cmd)
    if resolved:
        return (PASS, "claude on PATH", f"{cmd} -> {resolved}")
    return (FAIL, "claude on PATH",
            f"'{cmd}' not found on PATH; sessions cannot spawn "
            f"(set claude_cmd or install the Claude CLI)")


def _check_git_identity(paths: Paths, config: Config) -> CheckResult:
    name = _git(paths.root, "config", "user.name").stdout.strip()
    email = _git(paths.root, "config", "user.email").stdout.strip()
    if name and email:
        return (PASS, "git identity", f"{name} <{email}>")
    missing = " and ".join(
        n for n, v in (("user.name", name), ("user.email", email)) if not v)
    return (FAIL, "git identity",
            f"git {missing} not set; a session cannot commit its work "
            f"(set it with `git config user.name`/`user.email`)")


def _check_base_branch(paths: Paths, config: Config) -> CheckResult:
    r = _git(paths.root, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{config.base_branch}")
    if r.returncode == 0:
        return (PASS, "base branch", f"{config.base_branch} exists")
    return (FAIL, "base branch",
            f"base_branch '{config.base_branch}' is not a local ref; "
            f"worktrees fork from it")


def _check_base_clean(paths: Paths, config: Config) -> CheckResult:
    # WARN only: `run` hard-enforces this via `_assert_base_clean`. doctor just surfaces it.
    dirty = dirty_base_paths(paths)
    if not dirty:
        return (PASS, "base tree clean", "no uncommitted source in the base tree")
    shown = ", ".join(dirty[:5]) + (f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else "")
    return (WARN, "base tree clean",
            f"{len(dirty)} uncommitted change(s): {shown} — `run` refuses a dirty base "
            f"(override AUTOBUILD_ALLOW_DIRTY_BASE=1)")


def _check_gh(paths: Paths, config: Config) -> CheckResult | None:
    # Only relevant under integration: pr. The harness degrades gracefully (it leaves
    # the branch for a manual PR), so a gh problem is a WARN, never fatal.
    if config.integration != "pr":
        return None
    if not which("gh"):
        return (WARN, "gh CLI",
                "gh not on PATH; PRs can't be opened (branch left for manual PR)")
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode == 0:
        return (PASS, "gh auth", "gh is authenticated")
    return (WARN, "gh auth",
            "gh auth status failed; PRs can't be opened (branch left for manual PR)")


def _check_disk(paths: Paths, config: Config) -> CheckResult:
    target = paths.ab_dir if paths.ab_dir.exists() else paths.root
    try:
        usage = shutil.disk_usage(target)
    except OSError as e:
        return (WARN, "free disk", f"could not determine free space: {e}")
    free_gib = usage.free / 1024 ** 3
    if usage.free >= MIN_FREE_DISK_BYTES:
        return (PASS, "free disk", f"{free_gib:.1f} GiB free")
    return (WARN, "free disk",
            f"only {free_gib:.1f} GiB free (< {MIN_FREE_DISK_BYTES / 1024 ** 3:.0f} GiB); "
            f"parallel worktrees of a large repo can exhaust it")


# The full check suite, in report order. Critical (FAIL-level) checks are also the
# subset wired into `run()` — see CRITICAL_CHECKS.
_CHECKS = (
    _check_config,
    _check_claude,
    _check_git_identity,
    _check_base_branch,
    _check_base_clean,
    _check_gh,
    _check_disk,
)

# The subset `run()` enforces early (before claiming/spawning): a session genuinely
# cannot do its job without these, so a real run should abort rather than burn tokens.
CRITICAL_CHECKS = (_check_claude, _check_git_identity)


# --- orchestration -----------------------------------------------------------

def run_checks(paths: Paths, config: Config) -> list[CheckResult]:
    """Run every doctor check and return the applicable `(level, name, detail)` results
    (checks that don't apply to this config — e.g. `gh` outside pr mode — are dropped)."""
    results: list[CheckResult] = []
    for check in _CHECKS:
        r = check(paths, config)
        if r is not None:
            results.append(r)
    return results


def doctor(paths: Paths, config: Config) -> int:
    """Run the preflight checks, print a readable report, and return an exit code:
    non-zero if any check FAILs (a WARN never fails the run)."""
    log("doctor: preflight checks")
    failed = 0
    for level, name, detail in run_checks(paths, config):
        line = f"{name}: {detail}"
        if level == PASS:
            ok(line)
        elif level == WARN:
            warn(line)
        else:
            _fail_line(line)
            failed += 1
    if failed:
        warn(f"doctor: {failed} check(s) FAILED — fix before running")
        return 1
    ok("doctor: all checks passed")
    return 0


class PreflightError(RuntimeError):
    """A critical preflight check failed, so `run` refuses to start (before claiming or
    spawning). str() is the operator message naming every failure."""


def assert_run_preflight(paths: Paths, config: Config) -> None:
    """Run only the critical (FAIL-level) checks before a run claims/spawns. Raises
    PreflightError naming every failure if any FAILs — the same checks `doctor` reports,
    so the operator can run `autobuild doctor` for the full picture."""
    failures = [
        f"{name}: {detail}"
        for level, name, detail in (c(paths, config) for c in CRITICAL_CHECKS)
        if level == FAIL
    ]
    if failures:
        raise PreflightError(
            "preflight failed; refusing to start a run:\n  - "
            + "\n  - ".join(failures)
            + "\n  run 'autobuild doctor' for the full report")
