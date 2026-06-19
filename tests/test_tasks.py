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
