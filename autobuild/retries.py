"""The timeout-retry ledger — a disposable record of which sessions timed out for
each task, used to bound automatic retries.

Lives at `.autobuild/retries.json` (`Paths.retries_ledger`): `{ "<tid>": ["<sid>", ...] }`.
It tracks the *set* of timed-out session ids per task, so re-recording the same session
(a crash-driven re-reap) is idempotent and can never inflate the count. The durable
*outcome* of an exhausted task lives in its terminal status under `tasks/` (in git); this
ledger only holds in-flight retry budget, so it is safe to delete with the rest of
`.autobuild/`. `clean` removes only reaped session dirs, so the ledger survives it.

All mutating ops assume the caller already holds the backlog lock (the same lock
`claim_tasks` / `next_task_id` use) — they do NOT re-acquire it, so they compose inside a
single locked critical section in the reaper without self-deadlocking on the advisory lock.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def _load(path: Path) -> dict[str, list[str]]:
    """Read the ledger, tolerating an absent, torn, or non-object file by returning an
    empty mapping — a corrupt ledger must never crash a reap or strand a budget."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    # Coerce each value to a list[str]; drop anything malformed rather than trust it.
    out: dict[str, list[str]] = {}
    for tid, sids in obj.items():
        if isinstance(sids, list):
            out[str(tid)] = [str(s) for s in sids]
    return out


def _save(path: Path, data: dict[str, list[str]]) -> None:
    """Atomically replace the ledger (temp in the same dir + os.replace), so a concurrent
    reader only ever sees the old complete file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def retry_count(path: Path, tid: str) -> int:
    """How many distinct sessions have timed out for `tid` so far (0 if none)."""
    return len(_load(path).get(tid, []))


def record_timeout(path: Path, tid: str, sid: str) -> int:
    """Record that session `sid` timed out for task `tid`; return the new distinct count.
    Idempotent in `sid` — re-recording the same session does not increase the count.
    Caller must hold the backlog lock."""
    data = _load(path)
    sids = data.setdefault(tid, [])
    if sid not in sids:
        sids.append(sid)
        _save(path, data)
    return len(sids)


def clear_retries(path: Path, tid: str) -> None:
    """Drop `tid`'s retry history (called when the task reaches a terminal state, so a
    later manual re-open starts with a fresh budget). No-op if absent. Caller must hold
    the backlog lock."""
    data = _load(path)
    if data.pop(tid, None) is not None:
        _save(path, data)
