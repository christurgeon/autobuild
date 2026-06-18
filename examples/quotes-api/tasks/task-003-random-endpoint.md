---
id: task-003
title: GET /quotes/random endpoint
status: todo
priority: 2
depends_on: [task-002]   # needs QuoteStore.random()
---

## Goal

Expose `GET /quotes/random`, returning a random quote from the store as JSON. Runs in
parallel with task-004 — both depend only on the store, not on each other.

## Acceptance criteria

- [ ] `GET /quotes/random` returns 200 with a single quote payload (`id`, `text`, `author`, `tags`).
- [ ] Returns 404 with a clear message when the store is empty.
- [ ] `tests/test_random.py` covers the happy path and the empty-store case.
- [ ] `ruff check .` and `pytest` both pass.

## Notes

Wire the endpoint to `QuoteStore.random()` from task-002. The `?tag=` filter is a separate
task (task-005) — keep this one to the unfiltered random case.
