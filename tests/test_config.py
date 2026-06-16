from pathlib import Path

import pytest

from autobuild.config import Config, ConfigError, load_config
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

verify_checks: true

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
    assert cfg.verify_checks is True
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
    assert cfg.verify_checks is True
    assert cfg.claude_cmd == "claude"


def test_verify_checks_false_parses_to_bool(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("verify_checks: false\n")
    cfg = load_config(p)
    assert cfg.verify_checks is False
    assert cfg.checks == []  # other defaults preserved


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


# --- validation --------------------------------------------------------------

def _write(tmp_path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_invalid_integration_raises_listing_valid_values(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "integration: prr\n"))
    msg = str(e.value)
    assert "integration" in msg
    assert "pr" in msg and "auto-merge" in msg and "branch" in msg


def test_max_parallel_zero_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_parallel: 0\n"))
    assert "max_parallel" in str(e.value)


def test_max_parallel_negative_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_parallel: -1\n"))
    assert "max_parallel" in str(e.value)


def test_max_parallel_non_int_string_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_parallel: abc\n"))
    assert "max_parallel" in str(e.value)
    assert "integer" in str(e.value)


def test_max_parallel_float_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_parallel: 3.5\n"))
    assert "max_parallel" in str(e.value)
    assert "integer" in str(e.value)


def test_max_parallel_bool_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_parallel: true\n"))
    assert "max_parallel" in str(e.value)
    assert "integer" in str(e.value)


def test_max_iterations_zero_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "max_iterations: 0\n"))
    assert "max_iterations" in str(e.value)


def test_empty_base_branch_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, 'base_branch: ""\n'))
    assert "base_branch" in str(e.value)


def test_null_model_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "model:\n"))
    assert "model" in str(e.value)


def test_empty_claude_cmd_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, 'claude_cmd: ""\n'))
    assert "claude_cmd" in str(e.value)


def test_checks_as_bare_string_raises_with_list_hint(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, "checks: echo hi\n"))
    msg = str(e.value)
    assert "checks" in msg
    assert "list" in msg
    # the hint shows the YAML list form for the offending value
    assert "echo hi" in msg


def test_checks_with_empty_element_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, 'checks:\n  - "ok"\n  - ""\n'))
    assert "checks" in str(e.value)


def test_aggregates_all_problems(tmp_path):
    body = (
        "integration: prr\n"
        "max_parallel: 0\n"
        'base_branch: ""\n'
        "checks: echo hi\n"
    )
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, body))
    problems = e.value.problems
    # every bad field is reported, not just the first one
    joined = "\n".join(problems)
    assert len(problems) >= 4
    assert "integration" in joined
    assert "max_parallel" in joined
    assert "base_branch" in joined
    assert "checks" in joined


def test_config_error_names_path(tmp_path):
    p = _write(tmp_path, "integration: nope\n")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert e.value.path == p
    assert str(p) in str(e.value)


def test_non_mapping_top_level_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "- a\n- b\n"))


def test_unknown_key_warns_but_does_not_abort(tmp_path, capsys):
    cfg = load_config(_write(tmp_path, "intergration: pr\nmax_parallel: 2\n"))
    err = capsys.readouterr().err
    assert "intergration" in err
    # the typo'd key is ignored; everything else still loads with defaults
    assert cfg.max_parallel == 2
    assert cfg.integration == "pr"


def test_valid_config_loads_with_no_warnings(tmp_path, capsys):
    body = (
        "model: claude-opus-4-8\n"
        "max_parallel: 4\n"
        "base_branch: develop\n"
        "max_iterations: 10\n"
        "integration: auto-merge\n"
        "checks:\n  - 'pytest -q'\n"
        "claude_cmd: claude\n"
    )
    cfg = load_config(_write(tmp_path, body))
    captured = capsys.readouterr()
    assert cfg == Config(
        model="claude-opus-4-8",
        max_parallel=4,
        base_branch="develop",
        max_iterations=10,
        integration="auto-merge",
        checks=["pytest -q"],
        claude_cmd="claude",
    )
    assert captured.out == "" and captured.err == ""
