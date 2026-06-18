---
id: task-006
title: README + OpenAPI examples
status: todo
priority: 4
depends_on: [task-003, task-004]   # a join: documents both endpoints
---

## Goal

Write the project `README.md` (install, run, example `curl` calls) and enrich the FastAPI
route definitions with summaries and example payloads so `/docs` reads well.

## Acceptance criteria

- [ ] `README.md` covers install, `uvicorn` run command, and a `curl` example for each endpoint.
- [ ] Each route has a `summary` and a request/response example surfaced at `/docs`.
- [ ] The documented `curl` examples actually work against a freshly started server.
- [ ] `ruff check .` and `pytest` both pass.

## Notes

A join task: it depends on both endpoint tasks (task-003 and task-004), so the scheduler
won't start it until both are `done`. Document the `?tag=` filter from task-005 if it has
landed by then.
