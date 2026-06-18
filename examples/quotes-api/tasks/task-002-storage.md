---
id: task-002
title: Quote store + seed data
status: todo
priority: 2
depends_on: [task-001]   # needs the package skeleton from the scaffold
---

## Goal

Add a small persistence layer: a `Quote` model (`id`, `text`, `author`, `tags: list[str]`)
and a `QuoteStore` that loads from and saves to a single JSON file under `data/quotes.json`.
Seed the store with ~10 quotes covering a few distinct tags.

## Acceptance criteria

- [ ] `quotes_api/store.py` defines `Quote` and a `QuoteStore` with `all()`, `add(quote)`,
      and `random(tag=None)` methods.
- [ ] `data/quotes.json` ships with ~10 seed quotes spanning at least 3 tags.
- [ ] `tests/test_store.py` covers add, random, and tag-filtered random.
- [ ] `ruff check .` and `pytest` both pass.

## Notes

Persistence is a JSON file by design (see GOAL constraints) — do not introduce a database.
This task defines the store interface that the two endpoint tasks build on.
