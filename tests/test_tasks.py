import hashlib

import pytest

from autobuild import tasks
from autobuild.tasks import (
    DEFAULT_PRIORITY,
    Task,
    create_task_file,
    is_terminal,
    iter_tasks,
    next_task_id,
    parse_frontmatter,
    read_task,
    set_status,
    slugify,
    task_index,
)


@pytest.mark.parametrize("status,terminal", [
    ("done", True),
    ("blocked", True),
    ("timeout", True),       # retries exhausted -> a terminal resting state
    ("todo", False),
    ("claimed", False),
    ("in-progress", False),
])
def test_is_terminal(status, terminal):
    assert is_terminal(status) is terminal

TEMPLATE_TASK = """\
---
id: task-001
title: Example task — replace me
status: todo          # todo | claimed | in-progress | review | done | blocked
priority: 1           # lower number = higher priority
depends_on: []        # list of task ids that must be `done` first, e.g. [task-000]
---

## Goal

A status: line in the body must NOT be rewritten.
"""


def write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---- reading frontmatter ----------------------------------------------------

def test_reads_scalars_and_ignores_comments(tmp_path):
    t = read_task(write(tmp_path, "task-001.md", TEMPLATE_TASK))
    assert t.id == "task-001"
    assert t.title == "Example task — replace me"  # unicode em-dash preserved
    assert t.status == "todo"  # trailing comment stripped
    assert t.priority == 1 and isinstance(t.priority, int)
    assert t.depends_on == []


@pytest.mark.parametrize("literal,expected", [
    ("[]", []),
    ("[task-000]", ["task-000"]),
    ("[task-000, task-001]", ["task-000", "task-001"]),
])
def test_depends_on_parsed_as_real_list(tmp_path, literal, expected):
    body = f"---\nid: t\nstatus: todo\npriority: 1\ndepends_on: {literal}\n---\n"
    t = read_task(write(tmp_path, "t.md", body))
    assert t.depends_on == expected


def test_missing_fields_get_defaults(tmp_path):
    t = read_task(write(tmp_path, "task-009.md", "---\nid: task-009\n---\nbody\n"))
    assert t.status == "todo"
    assert t.priority == DEFAULT_PRIORITY
    assert t.depends_on == []


def test_non_numeric_priority_defaults(tmp_path):
    body = "---\nid: t\nstatus: todo\npriority: high\n---\n"
    assert read_task(write(tmp_path, "t.md", body)).priority == DEFAULT_PRIORITY


def test_parse_frontmatter_ignores_body(tmp_path):
    data = parse_frontmatter(TEMPLATE_TASK)
    assert data["status"] == "todo"
    assert "Goal" not in data


# ---- surgical, atomic status write -----------------------------------------

def test_set_status_changes_only_status_line(tmp_path):
    p = write(tmp_path, "task-001.md", TEMPLATE_TASK)
    set_status(p, "in-progress")

    lines = p.read_text(encoding="utf-8").splitlines()
    status_lines = [ln for ln in lines if ln.startswith("status:")]
    assert status_lines == ["status: in-progress          # todo | claimed | in-progress | review | done | blocked"]

    # everything except the status line is byte-identical to the original
    orig = [ln for ln in TEMPLATE_TASK.splitlines() if not ln.startswith("status:")]
    now = [ln for ln in lines if not ln.startswith("status:")]
    assert orig == now


def test_set_status_roundtrips_and_is_idempotent(tmp_path):
    p = write(tmp_path, "task-001.md", TEMPLATE_TASK)
    for s in ("claimed", "in-progress", "done"):
        set_status(p, s)
        assert read_task(p).status == s


def test_set_status_leaves_no_temp_files(tmp_path):
    p = write(tmp_path, "task-001.md", TEMPLATE_TASK)
    set_status(p, "done")
    temp_artifacts = [x.name for x in p.parent.iterdir() if x.name.startswith(".tmp-")]
    assert temp_artifacts == []


def test_set_status_does_not_touch_body_status_text(tmp_path):
    p = write(tmp_path, "task-001.md", TEMPLATE_TASK)
    set_status(p, "blocked")
    assert "A status: line in the body must NOT be rewritten." in p.read_text(encoding="utf-8")


def test_set_status_without_comment(tmp_path):
    p = write(tmp_path, "t.md", "---\nid: t\nstatus: todo\n---\nbody\n")
    set_status(p, "done")
    assert "status: done\n" in p.read_text(encoding="utf-8")


def test_set_status_raises_without_frontmatter(tmp_path):
    p = write(tmp_path, "t.md", "no frontmatter here\nstatus: todo\n")
    with pytest.raises(ValueError):
        set_status(p, "done")


# ---- index, ordering, id allocation ----------------------------------------

def test_iter_tasks_sorted_and_indexed(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for tid in ("task-003", "task-001", "task-002"):
        write(tasks_dir, f"{tid}.md", f"---\nid: {tid}\nstatus: todo\npriority: 1\n---\n")
    ids = [t.id for t in iter_tasks(tasks_dir)]
    assert ids == ["task-001", "task-002", "task-003"]
    assert set(task_index(tasks_dir)) == {"task-001", "task-002", "task-003"}


def test_next_task_id_from_max_frontmatter_id(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # filename deliberately disagrees with frontmatter id; allocator keys off id
    write(tasks_dir, "renamed.md", "---\nid: task-007\nstatus: todo\n---\n")
    write(tasks_dir, "task-002-foo.md", "---\nid: task-002\nstatus: todo\n---\n")
    assert next_task_id(tasks_dir) == "task-008"


def test_next_task_id_empty_dir(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    assert next_task_id(tasks_dir) == "task-001"


@pytest.mark.parametrize("title,slug", [
    ("Wire up CI", "wire-up-ci"),
    ("Fix: the THING!! (now)", "fix-the-thing-now"),
    ("   ", ""),
])
def test_slugify(title, slug):
    assert slugify(title) == slug


def test_create_task_file_safe_frontmatter(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # a title with YAML-hostile characters must still produce parseable frontmatter
    p = create_task_file(tasks_dir, "task-005", "danger: # title with: colons", priority=2, notes="see x")
    t = read_task(p)
    assert t.id == "task-005"
    assert t.title == "danger: # title with: colons"
    assert t.status == "todo"
    assert t.priority == 2
    assert t.depends_on == []


def test_create_task_file_empty_slug_falls_back_to_id(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    p = create_task_file(tasks_dir, "task-005", "！！！", priority=3)
    assert p.name == "task-005.md"


# ---- malformed task files never crash the backlog read ----------------------

MALFORMED = "---\nid: task-002\ntitle: [unclosed\nstatus: todo\n---\n"


def test_load_task_yaml_error_yields_invalid_pseudo_task(tmp_path):
    t = tasks.load_task(write(tmp_path, "task-002.md", MALFORMED))
    assert t.status == "invalid"
    assert t.id == "task-002"  # recovered from the id: line
    assert "line" in t.error


@pytest.mark.parametrize("body", [
    "---\nid: task-003\nstatus: todo\n",                 # block never closes
    "---\nid: task-003\ntitle: t\n---\n",                # no status: line
    "---\n- a\n- b\n---\n",                              # frontmatter is a list
    b"---\nid: task-003\nstatus: todo\n---\n\xff\xfe\n",  # non-UTF-8 bytes
])
def test_load_task_unclaimable_file_is_invalid(tmp_path, body):
    p = tmp_path / "task-003.md"
    if isinstance(body, bytes):
        p.write_bytes(body)
    else:
        p.write_text(body, encoding="utf-8")
    t = tasks.load_task(p)
    assert t is not None and t.status == "invalid"
    assert t.id == "task-003"


def test_load_task_ignores_file_without_frontmatter(tmp_path):
    p = write(tmp_path, "README.md", "# Backlog notes\n")
    assert tasks.load_task(p) is None
    write(tmp_path, "task-001.md", "---\nid: task-001\nstatus: todo\n---\n")
    assert [t.id for t in iter_tasks(tmp_path)] == ["task-001"]


def test_load_task_unrecoverable_id_falls_back_to_stem(tmp_path):
    t = tasks.load_task(write(tmp_path, "task-077-x.md", "---\ntitle: [bad\nstatus: todo\n---\n"))
    assert t.status == "invalid"
    assert t.id == "task-077-x"


def test_iter_tasks_tolerates_malformed_and_vanished_files(tmp_path, monkeypatch):
    write(tmp_path, "task-001.md", "---\nid: task-001\nstatus: todo\n---\n")
    write(tmp_path, "task-002.md", MALFORMED)
    gone = write(tmp_path, "task-003.md", "---\nid: task-003\nstatus: todo\n---\n")
    real = type(gone).read_text

    def flaky(self, *a, **kw):
        if self.name == "task-003.md":
            raise FileNotFoundError(self)
        return real(self, *a, **kw)

    monkeypatch.setattr(type(gone), "read_text", flaky)
    assert [t.status for t in iter_tasks(tmp_path)] == ["todo", "invalid"]


def test_index_tasks_first_file_wins_and_duplicate_ids_lists_all_paths(tmp_path):
    a = write(tmp_path, "a.md", "---\nid: task-001\ntitle: A\nstatus: todo\n---\n")
    b = write(tmp_path, "b.md", "---\nid: task-001\ntitle: B\nstatus: done\n---\n")
    write(tmp_path, "c.md", "---\nid: task-002\nstatus: todo\n---\n")
    ts = iter_tasks(tmp_path)
    assert tasks.index_tasks(ts)["task-001"].title == "A"
    assert task_index(tmp_path)["task-001"].path == a
    assert tasks.duplicate_ids(ts) == {"task-001": [a, b]}


def test_next_task_id_reserves_invalid_file_id(tmp_path):
    write(tmp_path, "task-001.md", "---\nid: task-001\nstatus: done\n---\n")
    write(tmp_path, "task-009.md", "---\nid: task-009\ntitle: [bad\nstatus: todo\n---\n")
    assert next_task_id(tmp_path) == "task-010"


# ---- per-task model override -------------------------------------------------

def test_task_model_override_parsed_and_stripped(tmp_path):
    t = tasks.load_task(write(tmp_path, "task-001.md",
                              "---\nid: task-001\nstatus: todo\nmodel: ' claude-sonnet-5-5 '\n---\n"))
    assert t.model == "claude-sonnet-5-5"


@pytest.mark.parametrize("extra", ["", "model:\n", "model: null\n"])
def test_task_without_model_has_none(tmp_path, extra):
    t = tasks.load_task(write(tmp_path, "task-001.md",
                              f"---\nid: task-001\nstatus: todo\n{extra}---\n"))
    assert t.status == "todo"
    assert t.model is None


@pytest.mark.parametrize("value", ["''", "'  '", "42", "[a, b]"])
def test_task_with_bad_model_is_invalid(tmp_path, value):
    """A typo'd model would otherwise fail only when `claude` starts, as a BLOCKED task."""
    t = tasks.load_task(write(tmp_path, "task-001.md",
                              f"---\nid: task-001\nstatus: todo\nmodel: {value}\n---\n"))
    assert t.status == "invalid"
    assert "model" in t.error
