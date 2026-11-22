"""Scheduler — choose runnable tasks and claim them atomically.

A task is runnable when status==todo and every depends_on id is `done`. The queue
is ordered by (priority, id). Claiming flips todo->claimed under an exclusive lock
so parallel runs never grab the same task. The lock is fcntl.flock on
.autobuild/backlog.lock, auto-released if the holder dies so a crash strands no
stale lock.
"""

from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

from .paths import Paths
from .tasks import (
    INVALID,
    Task,
    duplicate_ids,
    index_tasks,
    is_terminal,
    iter_tasks,
    set_status,
)


@contextlib.contextmanager
def backlog_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def deps_satisfied(task: Task, index: dict[str, Task]) -> bool:
    for dep in task.depends_on:
        d = index.get(dep)
        if d is None or d.status != "done":
            return False
    return True


def runnable_tasks(tasks: list[Task], index: dict[str, Task]) -> list[Task]:
    # A duplicated id is never schedulable, and neither is a dependent of one (even if
    # every copy is `done`): which copy is real is a human decision, so don't guess.
    dups = duplicate_ids(tasks)
    runnable = [t for t in tasks
                if t.status == "todo" and t.id not in dups
                and not any(d in dups for d in t.depends_on) and deps_satisfied(t, index)]
    return sorted(runnable, key=lambda t: (t.priority, t.id))


def stuck_tasks(tasks: list[Task], index: dict[str, Task]) -> dict[str, str]:
    """Classify every non-terminal task that can *never* become runnable, mapping
    its id to a human-readable reason. A task with no entry is either terminal or
    still on a path to runnable (its deps will eventually be `done`). Reasons:

    - ``missing-dependency: <id>`` — a (transitive) `depends_on` id absent from
      the backlog; it can never reach `done`, so the dependent is stuck.
    - ``blocked-dependency: <id>`` — a (transitive) dependency whose status is
      `blocked` (a terminal, non-`done` state).
    - ``dependency-cycle: <id -> ... -> id>`` — the task depends, directly or
      transitively, on a cycle; no member can complete.
    - ``duplicate-id: <id> (<a.md>, <b.md>)`` — more than one file declares the task's
      own id; no copy is ever scheduled.
    - ``invalid-task-file: <name.md> (<error>)`` — the task's own file is INVALID.
    - ``duplicate-dependency: <id>`` / ``invalid-dependency: <id>`` — a (transitive)
      dependency id is duplicated, or resolves to an INVALID file.

    A blocked dep is reported by its root id even when reached transitively, so a
    long todo chain behind one blocked task all names that task. Cycle detection
    uses the DFS recursion stack and so always terminates.
    """
    def blocker(tid: str, path: list[str]) -> str | None:
        """Why can the task `tid` never reach status `done`? None if it can.
        `path` is the active DFS recursion stack of task ids (ancestors)."""
        if tid in path:  # a back-edge into the current stack — a cycle
            cycle = path[path.index(tid):] + [tid]
            return f"dependency-cycle: {' -> '.join(cycle)}"
        if tid in dups:
            return f"duplicate-dependency: {tid}"
        task = index.get(tid)
        if task is None:
            return f"missing-dependency: {tid}"
        if task.status == INVALID:
            return f"invalid-dependency: {tid}"
        if task.status == "done":
            return None
        if task.status == "blocked":
            return f"blocked-dependency: {tid}"
        if task.status == "timeout":
            # terminal (retries exhausted) and never `done` — a dependency that strands
            # its dependents, reported distinctly from a `blocked` dep. (Only reached
            # transitively: a terminal timeout task is filtered out at the top level.)
            return f"timed-out-dependency: {tid}"
        path.append(tid)
        try:
            for dep in task.depends_on:
                reason = blocker(dep, path)
                if reason is not None:
                    return reason
            return None
        finally:
            path.pop()

    dups = duplicate_ids(tasks)
    stuck: dict[str, str] = {}
    for t in tasks:
        if is_terminal(t.status):
            continue  # terminal: a cause of stuckness, never itself stuck
        if t.id in dups:
            names = ", ".join(p.name for p in dups[t.id])
            stuck[t.id] = f"duplicate-id: {t.id} ({names})"
            continue
        if t.status == INVALID:
            stuck[t.id] = f"invalid-task-file: {t.path.name} ({t.error})"
            continue
        reason = blocker(t.id, [])
        if reason is not None:
            stuck[t.id] = reason
    return stuck


def claim_tasks(n: int, paths: Paths) -> list[Task]:
    """Atomically flip up to n runnable tasks todo->claimed; return them."""
    if n <= 0:
        return []
    claimed: list[Task] = []
    with backlog_lock(paths.lock_file):
        tasks = iter_tasks(paths.tasks_dir)
        index = index_tasks(tasks)
        for t in runnable_tasks(tasks, index):
            if len(claimed) >= n:
                break
            set_status(t.path, "claimed")
            t.status = "claimed"
            claimed.append(t)
    return claimed
