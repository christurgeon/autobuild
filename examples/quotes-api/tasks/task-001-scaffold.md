---
id: task-001
title: Project scaffold + health endpoint
status: todo          # todo | claimed | in-progress | review | done | blocked
priority: 1           # lower number = higher priority
depends_on: []        # the root task — nothing must land first
---

## Goal

Stand up the project skeleton so every later task has a place to land: a `pyproject.toml`
declaring `fastapi` and `uvicorn` (and `pytest` + `ruff` as dev deps), a `quotes_api/`
package with a FastAPI `app` in `quotes_api/main.py`, and a `tests/` directory.

## Acceptance criteria

- [ ] `pyproject.toml` declares the runtime + dev dependencies and is installable.
- [ ] `quotes_api/main.py` exposes `app` with `GET /health` returning `{"status": "ok"}`.
- [ ] `tests/test_health.py` asserts the health endpoint returns 200 and the expected body.
- [ ] `ruff check .` and `pytest` both pass.

## Notes

Keep the package import path `quotes_api` — later tasks and the GOAL's done-criteria assume it.
