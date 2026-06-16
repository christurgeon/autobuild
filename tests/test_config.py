from pathlib import Path

from autobuild.config import Config, load_config
from autobuild.paths import Paths

TEMPLATE_CONFIG = """\
# autobuild configuration. Safe to edit; the loop re-reads it each iteration.

model: claude-opus-4-8        # passed to `claude --model`
max_parallel: 3               # WIP limit / number of concurrent worktrees
base_branch: main             # feature branches fork from and merge into this
max_iterations: 50            # global safety stop for the outer loop

integration: pr

checks:
  - "echo 'replace me with real checks, e.g. npm test'"

claude_cmd: claude            # override if your Claude CLI binary differs
"""


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "nope.yml")
    assert cfg == Config()
    assert cfg.model == "claude-opus-4-8"
    assert cfg.max_parallel == 3
    assert cfg.base_branch == "main"
    assert cfg.max_iterations == 50
    assert cfg.integration == "pr"
    assert cfg.checks == []
    assert cfg.claude_cmd == "claude"


def test_empty_file_uses_defaults(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("\n# only comments\n")
    assert load_config(p) == Config()


def test_parses_template_config(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(TEMPLATE_CONFIG)
    cfg = load_config(p)
    assert cfg.model == "claude-opus-4-8"
    assert cfg.max_parallel == 3 and isinstance(cfg.max_parallel, int)
    assert cfg.max_iterations == 50 and isinstance(cfg.max_iterations, int)
    assert cfg.integration == "pr"
    assert cfg.claude_cmd == "claude"


def test_checks_block_list_keeps_inner_quotes(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(TEMPLATE_CONFIG)
    cfg = load_config(p)
    assert cfg.checks == ["echo 'replace me with real checks, e.g. npm test'"]


def test_overrides_partial_keep_other_defaults(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("integration: auto-merge\nmax_parallel: 7\n")
    cfg = load_config(p)
    assert cfg.integration == "auto-merge"
    assert cfg.max_parallel == 7
    assert cfg.base_branch == "main"  # default preserved


def test_paths_resolve_from_root(tmp_path):
    paths = Paths(tmp_path)
    assert paths.ab_dir == tmp_path / ".autobuild"
    assert paths.tasks_dir == tmp_path / "tasks"
    assert paths.sessions_dir == tmp_path / ".autobuild" / "sessions"
    assert paths.worktrees_dir == tmp_path / ".autobuild" / "worktrees"
    assert paths.config_file == tmp_path / ".autobuild" / "config.yml"
    assert paths.lock_file == tmp_path / ".autobuild" / "backlog.lock"


def test_paths_ensure_runtime_dirs(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure_runtime_dirs()
    assert paths.sessions_dir.is_dir()
    assert paths.worktrees_dir.is_dir()
