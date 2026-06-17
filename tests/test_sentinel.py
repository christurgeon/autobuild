"""task-101 — sentinel write discipline (conditional + atomic).

Covers the new `write_sentinel_if_absent` guard and the atomicity of every harness
sentinel write. Loop-path regressions (reap_stalled / _harvest / reconcile still
produce BLOCKED sentinels, now atomically) live alongside these.
"""

import json
import os

import pytest

from autobuild import session as session_mod
from autobuild.paths import Paths
from autobuild.session import write_sentinel, write_sentinel_if_absent


def setup(repo):
    paths = Paths(repo)
    paths.tasks_dir.mkdir(parents=True)
    paths.ensure_runtime_dirs()
    return paths


def make_sdir(paths, sid="sess-task-001"):
    sdir = paths.sessions_dir / sid
    sdir.mkdir(parents=True)
    return sdir


# A valid object plus stray trailing output an agent appended is still a *real*
# result the guard must protect (matches loop._classify_sentinel's "reapable").
SALVAGEABLE = '{"task": "task-001", "status": "COMPLETE", "summary": "s"}\n</content>\n'

# Shapes loop._classify_sentinel treats as "corrupt" — no usable leading object.
CORRUPT_SENTINELS = {
    "leading-garbage": 'oops a preamble\n{"task": "task-001", "status": "COMPLETE"}\n',
    "empty": "",
    "whitespace": "   \n\t\n",
    "json-list": "[]",
    "json-string": '"x"',
    "torn": '{"task": "task-001"',
}


# ---- write_sentinel_if_absent: the guard ------------------------------------

def test_writes_when_result_absent(git_repo):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "no result") is True
    result = json.loads((sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"
    assert result["summary"] == "no result"


def test_refuses_to_overwrite_parseable_result(git_repo):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    write_sentinel(sdir, "task-001", "COMPLETE", "real agent result")
    # the harness must NOT clobber a real result
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "late block") is False
    result = json.loads((sdir / "result.json").read_text())
    assert result["status"] == "COMPLETE"
    assert result["summary"] == "real agent result"


def test_refuses_to_overwrite_parseable_result_with_trailing_junk(git_repo):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    (sdir / "result.json").write_text(SALVAGEABLE, encoding="utf-8")
    # a salvageable result (valid leading object) is a real result -> refuse
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "late block") is False
    assert (sdir / "result.json").read_text() == SALVAGEABLE


def test_refuses_when_reaped_marker_exists(git_repo):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    (sdir / "reaped.json").write_text("{}", encoding="utf-8")
    # even with no result.json, an already-reaped session is off limits
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "x") is False
    assert not (sdir / "result.json").exists()


def test_refuses_when_reaped_even_with_corrupt_result(git_repo):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    (sdir / "result.json").write_text("garbage", encoding="utf-8")
    (sdir / "reaped.json").write_text("{}", encoding="utf-8")
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "x") is False
    assert (sdir / "result.json").read_text() == "garbage"


@pytest.mark.parametrize("shape", sorted(CORRUPT_SENTINELS))
def test_overwrites_corrupt_result(git_repo, shape):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    (sdir / "result.json").write_text(CORRUPT_SENTINELS[shape], encoding="utf-8")
    # a present-but-corrupt result is not a real result -> the guard writes over it
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "recovered") is True
    result = json.loads((sdir / "result.json").read_text())
    assert result["status"] == "BLOCKED"


# ---- atomicity --------------------------------------------------------------

def test_write_sentinel_uses_temp_then_replace(git_repo, monkeypatch):
    """The final swap goes through os.replace, and the temp source is already
    complete JSON at swap time — so a concurrent reader never sees a partial file."""
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    swaps = []
    real_replace = os.replace

    def spy_replace(src, dst):
        # at the atomic swap, the temp source holds the COMPLETE object
        assert json.loads(open(src).read())["status"] == "BLOCKED"
        swaps.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(session_mod.os, "replace", spy_replace)
    write_sentinel(sdir, "task-001", "BLOCKED", "x")

    assert swaps and swaps[0][1].endswith("result.json")
    assert json.loads((sdir / "result.json").read_text())["status"] == "BLOCKED"
    # no partial temp left behind (catch dot-prefixed temps too)
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []


def test_atomic_overwrite_reader_sees_old_complete_never_partial(git_repo, monkeypatch):
    """Overwriting an existing sentinel is atomic: mid-swap, the destination still
    holds the OLD complete object — never a half-written new one."""
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    write_sentinel(sdir, "task-001", "COMPLETE", "v1")
    real_replace = os.replace

    def spy_replace(src, dst):
        # a reader during the swap sees the old, complete file (not partial new)
        assert json.loads(open(dst).read())["summary"] == "v1"
        real_replace(src, dst)

    monkeypatch.setattr(session_mod.os, "replace", spy_replace)
    write_sentinel(sdir, "task-001", "COMPLETE", "v2")
    assert json.loads((sdir / "result.json").read_text())["summary"] == "v2"


def test_atomic_write_cleans_up_temp_on_replace_failure(git_repo, monkeypatch):
    """If os.replace fails after the temp is written, the swap is reported as a failure
    AND no temp file is left behind — the atomic write never leaks scratch files."""
    paths = setup(git_repo)
    sdir = make_sdir(paths)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(session_mod.os, "replace", boom)
    with pytest.raises(OSError):
        write_sentinel(sdir, "task-001", "BLOCKED", "x")
    assert not (sdir / "result.json").exists()        # the write did not land
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []  # no residue


def test_write_sentinel_if_absent_is_atomic(git_repo, monkeypatch):
    paths = setup(git_repo)
    sdir = make_sdir(paths)
    used = []
    real_replace = os.replace
    monkeypatch.setattr(session_mod.os, "replace",
                        lambda s, d: (used.append(str(d)), real_replace(s, d))[1])
    assert write_sentinel_if_absent(sdir, "task-001", "BLOCKED", "x") is True
    assert any(d.endswith("result.json") for d in used)
    assert [p for p in sdir.iterdir() if p.name.endswith(".tmp")] == []
