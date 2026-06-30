"""Parse a session's stream-json output into a small progress snapshot.

The harness spawns `claude -p --output-format stream-json --verbose` (issue #40), so
`session.out` is newline-delimited JSON events that flush live during the run — the default
text format buffers to 0 bytes, which was the whole observability problem. This turns that
stream into:
  - `messages`: how many `assistant` events the agent has emitted so far — a liveness proxy
    for `autobuild status` (this is the count of API messages, NOT claude's own `num_turns`,
    which counts differently; a tool-using turn produces several `assistant` events);
  - `cost_usd`: the session's total cost, known only once the terminal `result` event lands
    (None while still running) — the authoritative per-session figure issue #41's budget
    sums. Present even for an errored result (e.g. `error_max_turns`).

Total by construction: a torn trailing line (file mid-write), a non-JSON line, or an
unexpected shape is skipped, never raised — both `autobuild status` and the supervisor read
this while the file is still being written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def _num(v) -> float | None:
    """`v` as a float, or None if it is not a real number (bool is rejected: JSON `true`
    must never be read as a cost of 1.0)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@dataclass(frozen=True)
class SessionProgress:
    messages: int = 0
    cost_usd: float | None = None
    finished: bool = False

    @property
    def running(self) -> bool:
        return not self.finished


def parse_progress(text: str) -> SessionProgress:
    """Parse JSONL stream-json `text`. `assistant` events are counted as messages; the
    terminal `result` event supplies cost and marks the session finished. Unparseable,
    partial, or non-object lines are ignored (never raises)."""
    messages = 0
    cost = None
    finished = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue  # torn trailing line / non-JSON noise
        if not isinstance(evt, dict):
            continue
        etype = evt.get("type")
        if etype == "assistant":
            messages += 1
        elif etype == "result":
            # Any `result` event ends the session — success OR error (error_max_turns etc.),
            # which still carries the real cost. Key on type only, not subtype.
            finished = True
            cost = _num(evt.get("total_cost_usd"))
    return SessionProgress(messages=messages, cost_usd=cost, finished=finished)


def read_progress(out_file: Path) -> SessionProgress:
    """`parse_progress` over `out_file`'s bytes. Empty `SessionProgress()` if the file is
    missing or unreadable (a session that hasn't emitted yet). `errors="replace"` keeps a
    multibyte char torn at the in-flight write boundary from raising — that line then fails
    `json.loads` and is skipped like any other partial line."""
    try:
        text = out_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return SessionProgress()
    return parse_progress(text)
