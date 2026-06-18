# GOAL

> The north star for this project. autobuild sessions read this every iteration.
> Keep it stable — this is the *why* and the *definition of done for the whole
> project*, not a task list. Tasks live in `tasks/`.

## Mission

Build **quotes-api**, a small, well-tested FastAPI service that serves random quotes and
lets a client add new ones. It should be something a developer could clone and run in under
a minute, with clear OpenAPI docs.

## Definition of done (project level)

- [ ] `uvicorn quotes_api.main:app` starts and `GET /health` returns `{"status": "ok"}`.
- [ ] `GET /quotes/random` returns a random quote; `?tag=` filters by tag.
- [ ] `POST /quotes` adds a quote and persists it to the JSON store.
- [ ] OpenAPI docs at `/docs` describe every endpoint with example payloads.
- [ ] `ruff check .` and `pytest` both pass with meaningful test coverage of each endpoint.

## Constraints

- Agents MAY change: anything under `quotes_api/`, `tests/`, `pyproject.toml`, and `README.md`.
- Agents MUST NOT change: the public route shapes once defined (`/health`, `/quotes/random`,
  `/quotes`), or introduce a database server — persistence is a single JSON file under `data/`.

## Non-goals

- No authentication, rate limiting, or user accounts.
- No web UI — JSON API only.
- No external quote provider; quotes live in the local JSON store.
