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
