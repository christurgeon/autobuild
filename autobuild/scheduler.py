"""Scheduler — choose runnable tasks and claim them atomically. Ports scheduler.sh.

A task is runnable when status==todo and every depends_on id is `done`. The queue
is ordered by (priority, id). Claiming flips todo->claimed under an exclusive lock
so parallel runs never grab the same task. The lock is fcntl.flock on
.autobuild/backlog.lock (auto-released if the holder dies) rather than the bash
mkdir lockdir, which could strand a stale lock after a crash.
"""

from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

from .paths import Paths
from .tasks import Task, iter_tasks, set_status


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
    runnable = [t for t in tasks if t.status == "todo" and deps_satisfied(t, index)]
    return sorted(runnable, key=lambda t: (t.priority, t.id))


def claim_tasks(n: int, paths: Paths) -> list[Task]:
    """Atomically flip up to n runnable tasks todo->claimed; return them."""
    if n <= 0:
        return []
    claimed: list[Task] = []
    with backlog_lock(paths.lock_file):
        tasks = iter_tasks(paths.tasks_dir)
        index = {t.id: t for t in tasks}
        for t in runnable_tasks(tasks, index):
            if len(claimed) >= n:
                break
            set_status(t.path, "claimed")
            t.status = "claimed"
            claimed.append(t)
    return claimed
