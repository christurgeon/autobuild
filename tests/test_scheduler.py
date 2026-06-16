from concurrent.futures import ThreadPoolExecutor

from autobuild.paths import Paths
from autobuild.scheduler import claim_tasks, deps_satisfied, runnable_tasks
from autobuild.tasks import iter_tasks, read_task, task_index


def make_task(tasks_dir, tid, status="todo", priority=1, depends_on=()):
    deps = "[" + ", ".join(depends_on) + "]"
    p = tasks_dir / f"{tid}.md"
    p.write_text(
        f"---\nid: {tid}\ntitle: {tid}\nstatus: {status}\n"
        f"priority: {priority}\ndepends_on: {deps}\n---\n\n## Goal\nx\n",
        encoding="utf-8",
    )
    return p


def setup_project(tmp_path):
    paths = Paths(tmp_path)
    paths.tasks_dir.mkdir(parents=True)
    paths.ab_dir.mkdir(parents=True, exist_ok=True)
    return paths


# ---- dependency gating + ordering ------------------------------------------

def test_runnable_excludes_unmet_deps(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", status="todo")
    make_task(paths.tasks_dir, "task-002", status="todo", depends_on=["task-001"])
    tasks = iter_tasks(paths.tasks_dir)
    idx = task_index(paths.tasks_dir)
    ids = [t.id for t in runnable_tasks(tasks, idx)]
    assert ids == ["task-001"]


def test_runnable_includes_when_deps_done(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", status="done")
    make_task(paths.tasks_dir, "task-002", status="todo", depends_on=["task-001"])
    tasks = iter_tasks(paths.tasks_dir)
    idx = task_index(paths.tasks_dir)
    assert [t.id for t in runnable_tasks(tasks, idx)] == ["task-002"]


def test_runnable_orders_by_priority_then_id(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", priority=3)
    make_task(paths.tasks_dir, "task-002", priority=1)
    make_task(paths.tasks_dir, "task-003", priority=1)
    tasks = iter_tasks(paths.tasks_dir)
    idx = task_index(paths.tasks_dir)
    # priority 1 before 3; ties broken by id (task-002 before task-003)
    assert [t.id for t in runnable_tasks(tasks, idx)] == ["task-002", "task-003", "task-001"]


def test_unknown_dependency_is_never_runnable(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-002", depends_on=["task-999"])
    tasks = iter_tasks(paths.tasks_dir)
    idx = task_index(paths.tasks_dir)
    assert runnable_tasks(tasks, idx) == []
    assert deps_satisfied(read_task(paths.tasks_dir / "task-002.md"), idx) is False


def test_dependency_cycle_yields_no_runnable(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", depends_on=["task-002"])
    make_task(paths.tasks_dir, "task-002", depends_on=["task-001"])
    tasks = iter_tasks(paths.tasks_dir)
    idx = task_index(paths.tasks_dir)
    assert runnable_tasks(tasks, idx) == []


# ---- atomic claiming --------------------------------------------------------

def test_claim_flips_todo_to_claimed(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001")
    claimed = claim_tasks(1, paths)
    assert [t.id for t in claimed] == ["task-001"]
    assert read_task(paths.tasks_dir / "task-001.md").status == "claimed"


def test_claim_respects_n_and_priority(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", priority=3)
    make_task(paths.tasks_dir, "task-002", priority=1)
    make_task(paths.tasks_dir, "task-003", priority=2)
    claimed = claim_tasks(2, paths)
    assert [t.id for t in claimed] == ["task-002", "task-003"]
    assert read_task(paths.tasks_dir / "task-001.md").status == "todo"  # untouched


def test_claim_skips_non_todo(tmp_path):
    paths = setup_project(tmp_path)
    make_task(paths.tasks_dir, "task-001", status="in-progress")
    assert claim_tasks(5, paths) == []


def test_concurrent_claims_never_double_claim(tmp_path):
    paths = setup_project(tmp_path)
    for i in range(1, 9):
        make_task(paths.tasks_dir, f"task-{i:03d}")
    with ThreadPoolExecutor(max_workers=2) as ex:
        a = ex.submit(claim_tasks, 4, paths)
        b = ex.submit(claim_tasks, 4, paths)
        got = [t.id for t in a.result()] + [t.id for t in b.result()]
    assert sorted(got) == [f"task-{i:03d}" for i in range(1, 9)]  # all 8, no dup
    assert len(got) == len(set(got))
    assert all(t.status == "claimed" for t in iter_tasks(paths.tasks_dir))
