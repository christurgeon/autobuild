"""Task model + frontmatter I/O. Ports fm/set_status/count_status and the
follow-up id allocation from common.sh + loop.sh.

Reads use PyYAML (so `depends_on` is a real list, not a literal string). Writes
of the `status:` field are surgical — a single-line regex rewrite that preserves
comments and formatting — and atomic (same-dir temp + os.replace). Human task
files are NEVER reserialized through yaml.dump, which would strip their comments.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PRIORITY = 999  # missing/invalid priority sorts last; never silently promoted
TERMINAL = frozenset({"done", "blocked"})

# frontmatter is the leading `---\n ... \n---\n` block; group(1) is its body.
_FM_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
# a `status:` line within the frontmatter, capturing an optional trailing comment.
_STATUS_LINE_RE = re.compile(
    r"^(?P<pre>status:[ \t]*)(?P<val>[^\n#]*?)(?P<comment>[ \t]*#.*)?$",
    re.MULTILINE,
)
_ID_NUM_RE = re.compile(r"task-0*(\d+)")


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: int
    depends_on: list[str]
    path: Path


# ---- reading ----------------------------------------------------------------

def parse_frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else {}


def _coerce_priority(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def _coerce_deps(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(d).strip() for d in value if str(d).strip()]
    if isinstance(value, str):  # tolerate a scalar "task-001" or "a, b"
        return [d.strip() for d in value.strip("[]").split(",") if d.strip()]
    return [str(value)]


def read_task(path: Path) -> Task:
    data = parse_frontmatter(path.read_text(encoding="utf-8"))
    return Task(
        id=str(data.get("id") or path.stem),
        title=str(data.get("title", "")),
        status=str(data.get("status", "todo")),
        priority=_coerce_priority(data.get("priority")),
        depends_on=_coerce_deps(data.get("depends_on")),
        path=path,
    )


def iter_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.is_dir():
        return []
    return [read_task(p) for p in sorted(tasks_dir.glob("*.md"))]


def task_index(tasks_dir: Path) -> dict[str, Task]:
    return {t.id: t for t in iter_tasks(tasks_dir)}


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def count_by_status(tasks: list[Task]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    return counts


# ---- writing status (surgical + atomic) ------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)  # atomic same-filesystem rename
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_status(path: Path, new_status: str, *, preserve_comment: bool = True) -> None:
    """Rewrite only the `status:` value in the frontmatter, preserving everything
    else (comments, key order, body) byte-for-byte."""
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        raise ValueError(f"{path}: no frontmatter block to set status in")
    fm = m.group(1)

    def repl(mm: re.Match) -> str:
        comment = mm.group("comment") if (preserve_comment and mm.group("comment")) else ""
        return f"{mm.group('pre')}{new_status}{comment}"

    new_fm, n = _STATUS_LINE_RE.subn(repl, fm, count=1)
    if n != 1:
        raise ValueError(f"{path}: no status: line in frontmatter")
    _atomic_write(path, text[: m.start(1)] + new_fm + text[m.end(1):])


# ---- follow-up task creation -----------------------------------------------

def next_task_id(tasks_dir: Path) -> str:
    """Next free task-NNN, derived from the max frontmatter id (the scheduler's
    key), not from filenames. Call under the backlog lock for collision safety."""
    max_n = 0
    for t in iter_tasks(tasks_dir):
        m = _ID_NUM_RE.match(t.id)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"task-{max_n + 1:03d}"


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")


def render_task(task_id: str, title: str, priority: int, notes: str) -> str:
    # frontmatter via yaml so a hostile title/notes can't break parsing; ordered.
    fm = yaml.safe_dump(
        {"id": task_id, "title": title, "status": "todo",
         "priority": priority, "depends_on": []},
        sort_keys=False, allow_unicode=True,
    ).strip()
    notes = notes or "Auto-generated from a session follow-up."
    return (
        f"---\n{fm}\n---\n\n"
        f"## Goal\n{title}\n\n"
        f"## Acceptance criteria\n- [ ] (filed automatically by autobuild as a follow-up)\n\n"
        f"## Notes\n{notes}\n"
    )


def create_task_file(tasks_dir: Path, task_id: str, title: str,
                     priority: int = 3, notes: str = "") -> Path:
    slug = slugify(title)
    name = f"{task_id}-{slug}.md" if slug else f"{task_id}.md"
    path = tasks_dir / name
    path.write_text(render_task(task_id, title, priority, notes), encoding="utf-8")
    return path
