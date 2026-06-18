---
id: task-005
title: Tag filter on /quotes/random
status: todo
priority: 3
depends_on: [task-003]   # extends the random endpoint
---

## Goal

Add a `?tag=` query parameter to `GET /quotes/random` so a client can get a random quote
restricted to a single tag.

## Acceptance criteria

- [ ] `GET /quotes/random?tag=wisdom` returns a random quote whose `tags` include `wisdom`.
- [ ] An unknown tag returns 404 with a clear message.
- [ ] `GET /quotes/random` with no tag keeps its existing behavior (regression covered by a test).
- [ ] `ruff check .` and `pytest` both pass.

## Notes

Builds directly on task-003's endpoint and `QuoteStore.random(tag=...)` from task-002. Keep
the no-tag behavior unchanged.
