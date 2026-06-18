---
id: task-004
title: POST /quotes endpoint
status: todo
priority: 3
depends_on: [task-002]   # needs QuoteStore.add(); independent of task-003
---

## Goal

Expose `POST /quotes`, accepting a quote payload (`text`, `author`, optional `tags`),
persisting it via the store, and returning the created quote with its assigned `id`.

## Acceptance criteria

- [ ] `POST /quotes` validates the body and returns 201 with the created quote (including `id`).
- [ ] An invalid body (missing `text`) returns 422.
- [ ] The added quote survives a reload of the store (it was persisted to `data/quotes.json`).
- [ ] `tests/test_add.py` covers create, validation failure, and persistence.
- [ ] `ruff check .` and `pytest` both pass.

## Notes

Independent of task-003 — the scheduler can run both in parallel once task-002 is done.
Use the same `Quote` model; let the store assign the `id`.
