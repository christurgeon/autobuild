"""Pure tests for autobuild/contract.py: legacy-contract detection and the Checks block."""

from autobuild.contract import checks_block, is_legacy_contract


def test_is_legacy_contract_markers():
    assert is_legacy_contract("# CLAUDE.md — the autobuild session contract\n")
    assert is_legacy_contract("intro\n\nYou are a single **autobuild** session. Hi.\n")
    assert not is_legacy_contract("# my project\n")
    # a project CLAUDE.md that quotes the contract further down is not the contract
    quoted = "# my project\n" + "line\n" * 28 + "the autobuild session contract\n"
    assert quoted.splitlines()[29] == "the autobuild session contract"
    assert not is_legacy_contract(quoted)


def test_checks_block_lists_each_command_stripped():
    block = checks_block(["  uv run pytest -q ", "", "make lint"])
    assert "  $ uv run pytest -q\n" in block
    assert "  $ make lint\n" in block
    assert "$ \n" not in block


def test_checks_block_empty_says_none_configured():
    assert "No checks are configured" in checks_block([])


def _packaged_contract() -> str:
    from importlib.resources import files
    return files("autobuild").joinpath("templates/CONTRACT.md").read_text(encoding="utf-8")


def test_contract_stages_by_path_not_add_all():
    """Live run 2026-11-22: `git add -A` after the checks committed `__pycache__/*.pyc`,
    and two parallel sessions' identical cache paths conflicted on auto-merge."""
    text = _packaged_contract()
    assert "git add -A &&" not in text
    assert "git add <path>" in text
    assert "Do not use `git add -A`" in text


def test_contract_makes_result_json_the_last_action():
    """Live run 2026-11-22: a session wrote COMPLETE early, was reaped, then rewrote
    result.json after the harness had removed its worktree."""
    text = _packaged_contract()
    assert "Writing `result.json` is your last action" in text
