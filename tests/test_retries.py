"""The timeout-retry ledger: a disposable .autobuild/ record of which sessions
timed out for each task. All mutating ops assume the caller holds the backlog lock
(mirroring next_task_id); the count is a set of session-ids so re-recording the same
session is idempotent."""

from autobuild.paths import Paths
from autobuild.retries import clear_retries, record_timeout, retry_count


def test_count_is_zero_when_ledger_absent(tmp_path):
    assert retry_count(tmp_path / "retries.json", "task-001") == 0


def test_record_timeout_counts_distinct_sessions(tmp_path):
    ledger = tmp_path / "retries.json"
    assert record_timeout(ledger, "task-001", "sess-a") == 1
    assert record_timeout(ledger, "task-001", "sess-b") == 2
    assert retry_count(ledger, "task-001") == 2


def test_record_timeout_is_idempotent_per_session(tmp_path):
    ledger = tmp_path / "retries.json"
    assert record_timeout(ledger, "task-001", "sess-a") == 1
    # the SAME session re-recorded (a crash-driven re-reap) must not double-count
    assert record_timeout(ledger, "task-001", "sess-a") == 1
    assert retry_count(ledger, "task-001") == 1


def test_tasks_are_independent(tmp_path):
    ledger = tmp_path / "retries.json"
    record_timeout(ledger, "task-001", "sess-a")
    record_timeout(ledger, "task-002", "sess-b")
    assert retry_count(ledger, "task-001") == 1
    assert retry_count(ledger, "task-002") == 1


def test_clear_resets_one_task_only(tmp_path):
    ledger = tmp_path / "retries.json"
    record_timeout(ledger, "task-001", "sess-a")
    record_timeout(ledger, "task-002", "sess-b")
    clear_retries(ledger, "task-001")
    assert retry_count(ledger, "task-001") == 0
    assert retry_count(ledger, "task-002") == 1  # untouched


def test_clear_missing_task_is_noop(tmp_path):
    ledger = tmp_path / "retries.json"
    clear_retries(ledger, "task-001")  # must not raise
    assert retry_count(ledger, "task-001") == 0


def test_corrupt_ledger_reads_as_empty(tmp_path):
    ledger = tmp_path / "retries.json"
    ledger.write_text("not json at all{", encoding="utf-8")
    assert retry_count(ledger, "task-001") == 0
    # and a write recovers cleanly rather than propagating the corruption
    assert record_timeout(ledger, "task-001", "sess-a") == 1


def test_write_leaves_no_temp_residue(tmp_path):
    ledger = tmp_path / "retries.json"
    record_timeout(ledger, "task-001", "sess-a")
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_paths_exposes_retries_ledger(tmp_path):
    paths = Paths(tmp_path)
    assert paths.retries_ledger == paths.ab_dir / "retries.json"
