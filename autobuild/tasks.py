"""Task model + frontmatter I/O, plus follow-up id allocation.

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
# Terminal resting states. `timeout` is terminal too: it means a timed-out task that
# has exhausted its automatic retries (timeout_max_retries) — a settled failure, distinct
# from `blocked` so a human can tell "ran out of time" from "agent hit a wall".
TERMINAL = frozenset({"done", "blocked", "timeout"})
# In-memory pseudo-status for a task file that starts with `---` but cannot be safely
# scheduled (YAML error, unclosed block, non-mapping, no `status:` line, undecodable).
# Never written to disk; non-terminal, so the backlog settles instead of draining.
INVALID = "invalid"

# frontmatter is the leading `---\n ... \n---\n` block; group(1) is its body.
_FM_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
# a `status:` line within the frontmatter, capturing an optional trailing comment.
_STATUS_LINE_RE = re.compile(
    r"^(?P<pre>status:[ \t]*)(?P<val>[^\n#]*?)(?P<comment>[ \t]*#.*)?$",
    re.MULTILINE,
)
_ID_NUM_RE = re.compile(r"task-0*(\d+)")
# best-effort id recovery from an unparseable file, so a broken task keeps its identity.
_ID_LINE_RE = re.compile(r"""^id:[ \t]*["']?([^\s"'#]+)""", re.MULTILINE)


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: int
    depends_on: list[str]
    path: Path
    model: str | None = None  # per-task `model:` override of config.model
    error: str | None = None  # why the file is INVALID; None for a valid task


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
    return _task_from(parse_frontmatter(path.read_text(encoding="utf-8")), path)


def _task_from(data: dict, path: Path) -> Task:
    return Task(
        id=str(data.get("id") or path.stem),
        title=str(data.get("title", "")),
        status=str(data.get("status", "todo")),
        priority=_coerce_priority(data.get("priority")),
        depends_on=_coerce_deps(data.get("depends_on")),
        path=path,
        model=_coerce_model(data.get("model")),
    )


def _coerce_model(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _recover_id(text: str, path: Path) -> str:
    m = _FM_RE.match(text)
    found = _ID_LINE_RE.search(m.group(1) if m else text)
    return found.group(1) if found else path.stem


def _describe_yaml_error(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        # +2: the mark is 0-based within the block, which starts after the `---` line
        problem = getattr(exc, "problem", None) or "YAML error"
        return f"{problem} at line {mark.line + 2}, column {mark.column + 1}"
    return (str(exc).splitlines() or ["YAML error"])[0]


def load_task(path: Path) -> Task | None:
    """Read a task file without ever raising for a malformed or vanished file.

    Returns None for a file that vanished mid-scan or does not start with `---` (not a
    task, e.g. a tasks/README.md). A file that starts with `---` but cannot be safely
    scheduled becomes an INVALID pseudo-task (id recovered from its `id:` line, else the
    file stem) carrying the reason in `error`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        try:
            raw = path.read_bytes().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        return _invalid(raw, path, f"not valid UTF-8 (byte {e.start})")
    if not text.startswith("---"):
        return None
    m = _FM_RE.match(text)
    if not m:
        return _invalid(text, path, "frontmatter block does not close with ---")
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return _invalid(text, path, _describe_yaml_error(e))
    if not isinstance(data, dict):
        return _invalid(text, path, "frontmatter is not a mapping")
    if not _STATUS_LINE_RE.search(m.group(1)):
        return _invalid(text, path, "no status: line in frontmatter")
    model = data.get("model")
    if model is not None and not (isinstance(model, str) and model.strip()):
        return _invalid(text, path, "model: must be a non-empty string (or omitted)")
    return _task_from(data, path)


def _invalid(text: str, path: Path, error: str) -> Task:
    return Task(id=_recover_id(text, path), title="", status=INVALID,
                priority=DEFAULT_PRIORITY, depends_on=[], path=path, error=error)


def iter_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.is_dir():
        return []
    loaded = (load_task(p) for p in sorted(tasks_dir.glob("*.md")))
    return [t for t in loaded if t is not None]


def index_tasks(tasks: list[Task]) -> dict[str, Task]:
    """id -> Task. On a duplicate id the FIRST file in sorted-path order wins; callers
    that act on a task must check `duplicate_ids` first (the scheduler and reaper do)."""
    index: dict[str, Task] = {}
    for t in tasks:
        index.setdefault(t.id, t)
    return index


def task_index(tasks_dir: Path) -> dict[str, Task]:
    return index_tasks(iter_tasks(tasks_dir))


def duplicate_ids(tasks: list[Task]) -> dict[str, list[Path]]:
    """Every id declared by more than one file, mapped to all of its paths."""
    by_id: dict[str, list[Path]] = {}
    for t in tasks:
        by_id.setdefault(t.id, []).append(t.path)
    return {tid: ps for tid, ps in by_id.items() if len(ps) > 1}


def is_terminal(status: str) -> bool:
    return status in TERMINAL


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


def set_status(path: Path, new_status: str) -> None:
    """Rewrite only the `status:` value in the frontmatter, preserving everything
    else (comments, key order, body) byte-for-byte."""
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        raise ValueError(f"{path}: no frontmatter block to set status in")
    fm = m.group(1)

    def repl(mm: re.Match) -> str:
        comment = mm.group("comment") or ""
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
