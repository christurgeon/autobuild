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
    sums. Present even for an errored result (e.g. `error_max_turns`);
  - `limit`: a usage/rate-limit reason iff the stream ENDS in a limit state (see
    `_LIMIT_ERRORS`), read by `detect_limit` so a limited session re-queues instead of
    becoming terminal `blocked`.

Total by construction: a torn trailing line (file mid-write), a non-JSON line, or an
unexpected shape is skipped, never raised — both `autobuild status` and the supervisor read
this while the file is still being written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _num(v) -> float | None:
    """`v` as a float, or None if it is not a real number (bool is rejected: JSON `true`
    must never be read as a cost of 1.0)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# Usage/rate-limit detection reads ONLY the CLI's own structured signals. VERIFIED against
# the Claude Code 2.1.330 binary (its stream-json schema AND the code that emits them):
#   {"type":"rate_limit_event","rate_limit_info":{"status":"allowed"|"allowed_warning"|
#     "rejected","resetsAt":<int epoch s>,"rateLimitType":"five_hour"|"seven_day"|...}}
#   {"type":"assistant",...,"error":"rate_limit"|"billing_error"|...}  (top-level `error`)
# NOT verified: no live rate-limited transcript has been captured, and whether `claude -p`
# exits (vs. waits for the reset) on a rejected limit is assumed. Free text (the `result`
# text, session.err) is deliberately NOT matched: it is not a stable shape, and a task that
# is ABOUT rate limiting would false-positive. `system/api_retry` alone is not a limit (the
# CLI retries and may succeed). A missed limit degrades to the old BLOCKED/TIMEOUT.
_LIMIT_ERRORS = frozenset({"rate_limit", "billing_error"})


def _epoch_iso(v) -> str | None:
    n = _num(v)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionProgress:
    messages: int = 0
    cost_usd: float | None = None
    finished: bool = False
    limit: str | None = None  # set iff the stream ENDS in a usage/rate-limit state

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
    # `limit` tracks the latest limit state in stream order, so only a stream that ENDS in
    # a limit counts: a later allowed event or a normal assistant message clears it.
    limit: str | None = None
    resets: str | None = None
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
            err = evt.get("error")
            if isinstance(err, str) and err in _LIMIT_ERRORS:
                limit = err + (f", resets {resets}" if resets else "")
            else:
                limit = None  # the session recovered
        elif etype == "rate_limit_event":
            info = evt.get("rate_limit_info")
            if not isinstance(info, dict):
                continue
            if info.get("status") == "rejected":
                kind = info.get("rateLimitType")
                resets = _epoch_iso(info.get("resetsAt"))
                limit = ("usage limit" + (f" ({kind})" if isinstance(kind, str) else "")
                         + (f", resets {resets}" if resets else ""))
            else:
                limit = resets = None
        elif etype == "result":
            # Any `result` event ends the session — success OR error (error_max_turns etc.),
            # which still carries the real cost. Key on type only, not subtype.
            finished = True
            cost = _num(evt.get("total_cost_usd"))
    return SessionProgress(messages=messages, cost_usd=cost, finished=finished, limit=limit)


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


def detect_limit(sdir: Path) -> str | None:
    """A human-readable usage/rate-limit reason if the session's stream-json output ends in
    a limit state, else None. Never raises."""
    return read_progress(sdir / "session.out").limit
